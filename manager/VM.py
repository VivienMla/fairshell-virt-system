# -*- coding: utf-8 -*-
#
# Copyright 2020 - 2022 Vivien Malerba <vmalerba@gmail.com>
#
# This file is part of FAIRSHELL.
#
# FAIRSHELL is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# FAIRSHELL is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with FAIRSHELL.  If not, see <http://www.gnu.org/licenses/>.

# This program manages a VM (start, destroy, etc.)

import os
import syslog
import re
import time
import pwd
import grp
import json
import enum
import shutil
import tempfile
import netaddr
import threading
import xml.etree.ElementTree as ET
import libvirt
import Utils as util
import EventsHub as evh

# determine if iptables or nftables must be used
system_is_nftables=True
try:
    (status, out, err)=util.exec_sync(["/sbin/iptables", "--version"])
    if status!=0:
        raise Exception("Could not execute /sbin/iptables: %s"%err)
    if "legacy" in out or not os.path.exists("/sbin/nft"):
        system_is_nftables=False
except FileNotFoundError:
    pass
print("Using nftables: %s"%system_is_nftables)
if system_is_nftables:
    import NetworkNftables as nft
else:
    import NetworkIptables as nip

def _check_mount_directory(path):
    rpath=os.path.realpath(path)
    if not os.access(path, os.X_OK):
        raise Exception("No access to directory '%s'"%path)

class DisplayMode(str, enum.Enum):
    """Defines how a VM should be displayed"""
    NONE = "None"
    WINDOW = "Window"
    FULLSCREEN = "Fullscreen"

class NetworkMode(str, enum.Enum):
    """Defines hoow a VM is connected to the network"""
    NAT = "NAT"
    BRIDGE = "BRIDGE"

class VMConfig:
    confindex=0
    """Stores the configuration of a VM"""
    def __init__(self, id, conf_data, image_file_must_exist=True):
        """Object to describe a VM's configuration
        @id is used to determine libvirt's domain name
        """
        assert isinstance(id, str)
        VMConfig.confindex+=1
        self._index=VMConfig.confindex

        self._id=id
        for key in ("vm-imagefile", "os-variant", "descr", "shared-dir", "writable", "hardware", "allowed-users",
                    "resolved-names", "allowed-networks", "display", "usb-redir"):
            if key not in conf_data:
                raise Exception(f"Invalid VM '{id}' configuration: no '{key}' attribute")
        self._conf=conf_data

        key="vm-imagefile"
        value=conf_data[key]
        if not value or not isinstance(value, str):
            raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")
        if image_file_must_exist and not os.path.isfile(value):
            raise Exception(f"Invalid VM '{id}': VM image does not exist")

        key="os-variant"
        value=conf_data[key]
        if not value or not isinstance(value, str):
            raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")
        self._os_variant=value

        key="descr"
        value=conf_data[key]
        if not isinstance(value, str):
            raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")

        key="shared-dir"
        value=conf_data[key]
        if value is not None and not isinstance(value, str):
            raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")

        key="display"
        value=conf_data[key]
        if not value:
            self._display_mode=DisplayMode.NONE
        elif value=="window":
            self._display_mode=DisplayMode.WINDOW
        elif value=="fullscreen":
            self._display_mode=DisplayMode.FULLSCREEN
        else:
            raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")

        key="writable"
        value=conf_data[key]
        if not isinstance(value, bool):
            raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")

        key="hardware"
        value=conf_data[key]
        if not isinstance(value, dict):
            raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")
        try:
            self._memsize_g=float(value["mem"])/1024
            self._nb_cpus=int(value["cpu"])
            self._mac_addr=value["mac-addr"]
            if self._mac_addr and not re.match(r'^([a-f0-9][a-f0-9]:){5}[a-f0-9][a-f0-9]$', self._mac_addr):
                raise Exception()
        except Exception:
            raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")

        key="usb-redir"
        value=conf_data[key]
        if not isinstance(value, str):
            raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")
        parts=value.split(",")
        if value=="":
            self._usb_redir=[]
        else:
            for p in parts:
                if p not in ("all", "mass-storage", "smartcard"):
                    raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")
            self._usb_redir=parts
        syslog.syslog(syslog.LOG_INFO, "VM CONFIG USB REDIR=%s"%self._usb_redir)

        key="allowed-users"
        value=conf_data[key]
        if not isinstance(value, list):
            raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")
        for entry in value:
            if not isinstance(entry, str):
                raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")

        key="resolved-names"
        value=conf_data[key]
        if not isinstance(value, list):
            raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")
        for svalue in value:
            if not isinstance(svalue, str):
                raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")

        key="allowed-networks"
        value=conf_data[key]
        if not isinstance(value, list):
            raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")
        for svalue in value:
            if not isinstance(svalue, str):
                raise Exception(f"Invalid VM '{id}', configuration attribute '{key}'")

        self._extra_iso_images=[]
        self._iso_boot=None
        self._tmp_objects=[]

    @property
    def id(self):
        return self._id

    @property
    def descr(self):
        return self._conf["descr"]

    @property
    def base_image_file(self):
        """Base VM image file"""
        return self._conf["vm-imagefile"]

    @property
    def os_variant(self):
        """Virtualized OS variant, see 'osinfo-query os' for list"""
        return self._os_variant

    @property
    def dom_name(self):
        """Name of the domain (i.e. libvirt's VM name) for the VM"""
        return "fairshell-%s"%self._index

    @property
    def shared_dir(self):
        """Directory which is shared between the host and the VM"""
        return self._conf["shared-dir"]

    @property
    def memsize_g(self):
        return self._memsize_g

    @property
    def nb_cpus(self):
        return self._nb_cpus

    @property
    def mac_addr(self):
        """MAC address of the interface, or None if unspecified"""
        return self._mac_addr

    @property
    def usb_redir(self):
        """Types of devices to redirect, as a list of keywords among "all", "mass-storage", "smartcard"
        """
        return self._usb_redir

    @property
    def display_mode(self):
        return self._display_mode

    @property
    def writable(self):
        """Tells if the base VM image can be modified by commiting a runtime overlay"""
        return self._conf["writable"]

    @property
    def resolved_names(self):
        """List the names which are allowed to be resolved"""
        return self._conf["resolved-names"]

    @property
    def allowed_networks(self):
        """List the networks with which the VM can communicate (independently of the IP addresses resolved
        which are also allowed)"""
        return self._conf["allowed-networks"]

    @property
    def extra_iso_images(self):
        return self._extra_iso_images

    @extra_iso_images.setter
    def extra_iso_images(self, iso_images):
        self._extra_iso_images=iso_images

    @property
    def iso_boot(self):
        return self._iso_boot

    @iso_boot.setter
    def iso_boot(self, iso_image):
        if "," in iso_image:
            raise Exception("ISO image file '%s' can't contain the comma character"%iso_image)
        self._iso_boot=iso_image

    def keep_tmp_obj_ref(self, obj):
        self._tmp_objects+=[obj]

    def user_allowed(self, uid, gid):
        """Tells if the uid.gid user is allowed to use the configuration"""
        all_allowed=self._conf["allowed-users"]
        if all_allowed==[""]:
            return True

        # test if username specified AS-IS
        user=pwd.getpwuid(uid)
        if user.pw_name in all_allowed:
            return True

        # test if user is part of a group which is allowed
        for entry in all_allowed:
            if entry[0]!="@":
                continue
            try:
                gr=grp.getgrnam(entry[1:])
            except:
                syslog.syslog(syslog.LOG_ERR, "unknown group %s"%entry[1:])
                continue

            if user.pw_name in gr.gr_mem:
                return True
        return False

class State(str, enum.Enum):
    """State of the complete environment"""
    STOPPED = "STOPPED"
    PARTIAL = "PARTIAL"
    RUNNING = "RUNNING"
    COMMITTING = "COMMITTING"

#
#
# Networks management
#
#
class Network:
    """Represents an internal network"""
    netindex=12
    def __init__(self):
        Network.netindex+=1
        index=Network.netindex
        net="192.168.%d.0/24"%index
        self._network=netaddr.IPNetwork(net)
        self._name="fairshell%s"%index
        self._iface=self._name

    def create(self):
        """Create and start the network if it does not yet exist (virtual function)"""
        pass

    def destroy(self):
        """Stops and destroy the network if it was started and/or created (virtual function)"""
        pass

    def setup_filter_rules(self):
        """Ensure all the filtering rules are in place (virtual function)"""
        pass

    @property
    def name(self):
        """Name of the network as listed by libvirt or Docker (depending on the actual network
        type)"""
        return self._name

    @property
    def interface(self):
        """Actual Linux interface name of the network"""
        return self._iface

    @property
    def cidr(self):
        """CIRD of the network, as a str"""
        return str(self._network)

    def get_ip(self, index):
        """Get an IP address for the specified index system by comibining the CIDR with the
        requested IP index"""
        return str(self._network[index])

class VirtNetwork(Network):
    """Libvirt network, created and destroyed on the fly (not persistant)"""
    xml_nat="""<network>
                <name>%s</name>
                <bridge name='%s' stp='on' delay='0'/>
                <forward mode='nat'>
                    <nat>
                        <port start='1024' end='65535'/>
                    </nat>
                </forward>
                <ip address='%s' netmask='%s'>
                    <dhcp>
                        <range start='%s' end='%s'/>
                    </dhcp>
                </ip>
            </network>"""
    xml_direct="""<network>
                <name>%s</name>
                <bridge name='%s' stp='on' delay='0'/>
                <ip address='%s' netmask='%s'>
                    <dhcp>
                        <range start='%s' end='%s'/>
                    </dhcp>
                </ip>
            </network>"""

    def __init__(self, nat):
        """Creates a Libvirt network.
        The @nat argument defines of the network is NATed (if True) or directly plugged on the bridge (if False)
        """
        Network.__init__(self)
        self._nat=nat

    def _net_exists(self):
        """Tells if network exists"""
        (status, out, err)=util.exec_sync(["virsh", "net-list", "--all"], C_locale=True)
        if status!=0:
            syslog.syslog(syslog.LOG_ERR, "Could not list virt. networks: %s"%err)
            raise Exception("Could not list virt. networks: %s"%err)
        return self.name in out

    def create(self):
        if not self._net_exists():
            net=self._network
            if self._nat:
                xml_data=VirtNetwork.xml_nat%(self.name, self._iface, net[1], net.netmask, net[5], net[10])
            else:
                xml_data=VirtNetwork.xml_direct%(self.name, self._iface, net[1], net.netmask, net[5], net[10])

            tmpfile=tempfile.NamedTemporaryFile()
            tmpfile.write(xml_data.encode())
            tmpfile.flush()
            (status, out, err)=util.exec_sync(["virsh", "net-create", tmpfile.name], C_locale=True)
            if status!=0:
                syslog.syslog(syslog.LOG_ERR, "Could not define virt. network '%s': %s"%(self.name, err))
                raise Exception("Could not define virt. network '%s': %s"%(self.name, err))

    def destroy(self):
        if self._net_exists():
            # stop network
            (status, out, err)=util.exec_sync(["virsh", "net-destroy", self.name], C_locale=True)
            if status!=0:
                syslog.syslog(syslog.LOG_ERR, "Could not destroy virt. network '%s': %s"%(self.name, err))
                raise Exception("Could not destroy virt. network '%s': %s"%(self.name, err))

class DockerNetwork(Network):
    """Docker network"""
    def __init__(self):
        Network.__init__(self)

    def _net_exists(self):
        """Tells if network exists"""
        (status, out, err)=util.exec_sync(["docker", "network", "inspect", self.name])
        if status==0:
            # Docker network exists
            return True
        else:
            if "No such network" not in err:
                msg="Could not test if Docker network '%s' exists"%self.name
                syslog.syslog(syslog.LOG_ERR, msg)
                raise Exception(msg)
            return False

    def create(self):
        if not self._net_exists():
            args=["docker", "network", "create",
                  "-o", "com.docker.network.bridge.name=%s" % self.name,
                  "--subnet=%s"%self.cidr, self.name]
            (status, out, err)=util.exec_sync(args)
            if status!=0:
                msg="Could not create Docker network '%s': %s"%(self.name, err)
                syslog.syslog(syslog.LOG_ERR, msg)
                raise Exception(msg)

    def destroy(self):
        if self._net_exists():
            args=["docker", "network", "rm", self.name]
            (status, out, err)=util.exec_sync(args)
            if status!=0:
                syslog.syslog(syslog.LOG_ERR, "Could not remove Docker network '%s'"%self.name)
                raise Exception("Could not remove Docker network '%s'"%self.name)

class DockerContainer:
    """Represents a Docker container"""
    index=0
    images_dir="/usr/share/fairshell/virt-system/docker-images"

    def __init__(self, image_name, network, ip_index, image_id=None, env=None, shared_dirs=None):
        if not isinstance(network, DockerNetwork):
            raise Exception("CODEBUG: @network is not a DockerNetwork")

        DockerContainer.index+=1
        self._image_file="%s/%s.tar"%(DockerContainer.images_dir, image_name)
        self._image_name=image_name
        self._image_id=image_id
        self._cont_name="%s-%s"%(image_name, DockerContainer.index)
        self._network=network
        self._ip_index=ip_index
        self._env=env if env else {}
        self._shared_dirs=shared_dirs if shared_dirs else []

        # determine the exatc image ID from the image-ids.json is generated when 'compiling' the resources
        # which is like: {"fairshell-smb": "5589bf49aae0", "fairshell-unbound": "184edf88e011"}
        jids=util.load_file_contents("%s/image-ids.json"%DockerContainer.images_dir)
        ids=json.loads(jids)
        if not image_name in ids:
            raise Exception("Missing Docker ID for Docker image '%s'"%image_name)
        self._image_id=ids[image_name]

    @property
    def name(self):
        return self._cont_name

    @property
    def ip(self):
        return self._network.get_ip(self._ip_index)

    def _ensure_image_loaded(self):
        (status, out, err)=util.exec_sync(["docker", "images", "--format", "{{.ID}}", self._image_name])
        if status!=0:
            syslog.syslog(syslog.LOG_ERR, "Could not list Docker images: %s"%err)
            raise Exception("Could not list Docker images: %s"%err)
        if self._image_id and out!=self._image_id:
            if out:
                (status, out, err)=util.exec_sync(["docker", "rmi", self._image_name])
                if status!=0:
                    syslog.syslog(syslog.LOG_ERR, "Could not delete Docker image '%s': %s"%(self._image_name, err))

            if not os.path.exists(self._image_file):
                syslog.syslog(syslog.LOG_ERR, "Missing Docker image file '%s'"%self._image_file)
                raise Exception("Missing Docker image file '%s'"%self._image_file)

            (status, out, err)=util.exec_sync(["docker", "load", "-i", self._image_file])
            if status!=0:
                syslog.syslog(syslog.LOG_ERR, "Could not loadDocker image '%s': %s"%(self._image_name, err))
                raise Exception("Could not loadDocker image '%s': %s"%(self._image_name, err))

    def create(self):
        # if container exists but is not UP, destroy it
        (status, out, err)=util.exec_sync(["docker", "ps", "-a", "-f", "name=%s"%self._cont_name, "--format", "{{.Status}}"])
        if status!=0:
            syslog.syslog(syslog.LOG_ERR, "Could not list running Docker containers: %s"%err)
            raise Exception("Could not list running Docker containers: %s"%err)
        if out.startswith("Up "):
            return
        elif out and out!="\n":
            # remove container
            (status, out, err)=util.exec_sync(["docker", "rm", "-f", self._cont_name])
            if status!=0:
                syslog.syslog(syslog.LOG_ERR, "Could not remove stale Docker container '%s': %s"%(self._cont_name, err))
                raise Exception("Could not remove stale Docker container '%s': %s"%(self._cont_name, err))

        # ensure Docker image is loaded
        self._ensure_image_loaded()

        # prepare Docker'a arguments to start the container
        args=["docker", "run", "-d", "--name", self._cont_name, "--network", self._network.name,
             "--ip", self._network.get_ip(self._ip_index)]
        for entry in self._shared_dirs:
            if entry["host"] and entry["cont"] and entry["mode"]:
                args+=["-v", "%s:%s:%s,z"%(entry["host"], entry["cont"], entry["mode"])]
        for key in self._env:
            args+=["-e", "%s=%s"%(key, self._env[key])]
        args+=[self._image_id]

        # start container
        (status, out, err)=util.exec_sync(args)
        if status!=0:
            syslog.syslog(syslog.LOG_ERR, "Could not start Docker container '%s': %s"%(self._cont_name, err))
            util.exec_sync(["docker", "rm", "-f", self._cont_name]) # cleanup, to avoid stale containers in the created state
            raise Exception("Could not start Docker container '%s': %s"%(self._cont_name, err))

    def destroy(self):
        """Remove the container"""
        (status, out, err)=util.exec_sync(["docker", "ps", "-a", "-f", "name=%s"%self._cont_name,
                                           "--format", "{{.Status}}"])
        if status!=0:
            syslog.syslog(syslog.LOG_ERR, "Could not list running Docker containers: %s"%err)
            raise Exception("Could not list running Docker containers: %s"%err)

        # remove container
        if out and out!="\n":
            (status, out, err)=util.exec_sync(["docker", "rm", "-f", self._cont_name])
            if status!=0:
                syslog.syslog(syslog.LOG_ERR, "Could not remove stale Docker container '%s': %s"%(self._cont_name, err))
                raise Exception("Could not remove stale Docker container '%s': %s"%(self._cont_name, err))

    def get_environ(self):
        args=["docker", "inspect", "--format={{range .Config.Env}}{{println .}}{{end}}", self._cont_name]
        (status, out, err)=util.exec_sync(args)
        if status!=0:
            err="Could not get container '%s''s environment variables: %s"%(self._cont_name, err)
            syslog.syslog(syslog.LOG_ERR, err)
            raise Exception(err)

        env={}
        for line in out.splitlines():
            parts=line.split("=")
            if len(parts)==2:
                env[parts[0]]=parts[1]
        return env

class VM():
    """Start or stop the VM (does not handle the UI to display the VM) and the associated DNS and SMB
    infrastructure and filter rules.

    Arguments are:
    - uid: the ID of the user for which the VM will be started
    - gid: the group ID of the user for which the VM will be started
    - config: a dictionary with the folloginw keys:
        - vm-imagefile: full path of the file having the VM's HDD image
        - shared-dir: the directory which will be shared between the host and the VM (must be below the HOME directory)
        - vm-name: the name of the VM (in libvirt)
        - resolved-names: list of domain which can be resolved by the VM
        - allowed-networks: list of networks with which the VM is allowed to communicate
        - allow-smb: False if SMB is blocked
    """
    def __init__(self, config, uid, gid):
        assert isinstance(config, VMConfig)

        if not config.user_allowed(uid, gid):
            raise Exception("VM access denied")

        self._lock=threading.Lock()
        self._state=State.STOPPED
        self._config=config
        self._config_id=config.id
        self._update_state()
        self._uid=int(uid)
        self._gid=int(gid)

        base="/run/fairshell-virt-system"
        os.makedirs(base, exist_ok=True)
        os.chmod(base, 0o700)

        self._run_dir="%s/%s"%(base, self._config_id)
        os.makedirs(self._run_dir, exist_ok=True)
        os.chmod(self._run_dir, 0o700)

        base="/var/log/fairshell-virt-system" # hard coded in the systemd unit file
        os.makedirs(base, exist_ok=True)
        os.chmod(base, 0o700)

        self._logs_dir="%s/%s"%(base, self._config_id)
        os.makedirs(self._logs_dir, exist_ok=True)
        os.chmod(self._logs_dir, 0o777)

        self._resolved_dir="%s/resolved"%self._run_dir
        os.makedirs(self._resolved_dir, exist_ok=True)
        os.chmod(self._resolved_dir, 0o777)

        self._resolv_file="%s/resolv.json"%self._run_dir
        if not os.path.exists(self._resolv_file):
            self.set_dns_servers(["1.1.1.1"])

        # DNS zones for which a resolution is allowed
        self._forward_zones_file="%s/forward-zones.json"%(self._run_dir)
        util.write_data_to_file(json.dumps(self._config.resolved_names), self._forward_zones_file)

        # Networks' definitions for the VM and the Docker containers
        self._net_virt=VirtNetwork(True)
        self._net_dock_smb=DockerNetwork()
        self._net_dock_dns=DockerNetwork()

        print("VM's libvirt interface: %s"%self._net_virt.interface)
        print("Docker SMB interface: %s"%self._net_dock_smb.interface)
        print("Docker DNS interface: %s"%self._net_dock_dns.interface)

        # Docker container's definitions
        env={
            "SMBSERVERIP": self._net_dock_smb.get_ip(100)
        }
        shared=[
            {
                "host": self._resolv_file,
                "cont": "/etc/resolv.json",
                "mode": "ro"
            },
            {
                "host": self._forward_zones_file,
                "cont": "/etc/forward-zones.json",
                "mode": "ro"
            },
            {
                "host": self._resolved_dir,
                "cont": "/resolved",
                "mode": "rw"
            },
            {
                "host": self._logs_dir,
                "cont": "/logs",
                "mode": "rw"
            }
        ]
        self._container_dns=DockerContainer("fairshell-unbound", self._net_dock_dns, 100, env=env, shared_dirs=shared)
        self._container_smb=None # defined at run time

        # define networks' filtering rules
        if system_is_nftables:
            # using nftables
            self._nft_table=nft.Table(self._config.dom_name)

            vm_dns_nat=nft.Chain(self._nft_table, "vm-dns-nat", "nat", "prerouting")
            host_input=nft.Chain(self._nft_table, "host-input", "filter", "input")
            host_output=nft.Chain(self._nft_table, "host-output", "filter", "output")
            vm_ext_allow=nft.Chain(self._nft_table, "vm-ext-allow", None, None)
            vm_ext=nft.Chain(self._nft_table, "vm-ext", "filter", "forward")
            dns=nft.Chain(self._nft_table, "dns", "filter", "forward")
            smb=nft.Chain(self._nft_table, "smb", "filter", "forward")
            self._nft_chains=[
                vm_dns_nat,
                host_input,
                vm_ext_allow,
                vm_ext,
                dns,
                host_output,
                smb
            ]
            self._nft_rules=[
                nft.Rule(vm_dns_nat, ["iif", self._net_virt.interface, "udp", "dport", "53", "counter", "dnat", self._container_dns.ip]),

                nft.Rule(host_input, ["iif", self._net_virt.interface, "udp", "dport", "67", "counter", "accept"]),
                nft.Rule(host_input, ["iif", self._net_virt.interface, "ct", "state", "related,established", "accept"]),
                nft.Rule(host_input, ["iif", "{", self._net_virt.interface, ",", self._net_dock_smb.interface, ",", self._net_dock_dns.interface, "}", "counter", "log", "drop"]),

                nft.Rule(host_output, ["oif", self._net_virt.interface, "udp", "dport", "68", "counter", "accept"]),
                nft.Rule(host_output, ["oif", self._net_virt.interface, "tcp", "dport", "2443", "counter", "accept"]),
                nft.Rule(host_output, ["oif", "{", self._net_virt.interface, ",", self._net_dock_smb.interface, ",", self._net_dock_dns.interface, "}", "counter", "log", "drop"]),

                nft.Rule(vm_ext, ["iif", self._net_virt.interface, "oif", "!=", "{", self._net_dock_smb.interface, ",", self._net_dock_dns.interface, "}", "jump", "vm-ext-allow"]),
                nft.Rule(vm_ext, ["iif", self._net_virt.interface, "oif", "!=", "{", self._net_dock_smb.interface, ",", self._net_dock_dns.interface, "}", "counter", "log", "drop"]),

                nft.Rule(dns, ["iif", self._net_virt.interface, "oif", self._net_dock_dns.interface, "udp", "dport", "53", "accept"]),
                nft.Rule(dns, ["iif", self._net_dock_dns.interface, "oif", self._net_virt.interface, "udp", "sport", "53", "accept"]),
                nft.Rule(dns, ["iif", self._net_virt.interface, "oif", self._net_dock_dns.interface, "tcp", "dport", "53", "accept"]),
                nft.Rule(dns, ["iif", self._net_dock_dns.interface, "oif", self._net_virt.interface, "tcp", "sport", "53", "accept"]),
                nft.Rule(dns, ["iif", self._net_dock_dns.interface, "oif", "{", self._net_virt.interface, ",", self._net_dock_smb.interface, "}", "counter", "log", "drop"]),
                nft.Rule(dns, ["iif", "{", self._net_virt.interface, ",", self._net_dock_smb.interface, "}", "oif", self._net_dock_dns.interface, "counter", "log", "drop"]),
    
                nft.Rule(smb, ["iif", self._net_virt.interface, "oif", self._net_dock_smb.interface, "tcp", "dport", "445", "accept"]),
                nft.Rule(smb, ["iif", self._net_dock_smb.interface, "oif", self._net_virt.interface, "ct", "state", "related,established", "accept"]),
                nft.Rule(smb, ["oif", self._net_dock_smb.interface, "counter", "log", "drop"]),
                nft.Rule(smb, ["iif", self._net_dock_smb.interface, "counter", "log", "drop"])
            ]
            self._filter_chain=vm_ext_allow
            for cidr in self._config.allowed_networks:
                rule=nft.Rule(vm_ext_allow, ["ip", "daddr", cidr, "accept"])
                self._nft_rules+=[rule]
        else:
            # using iptables
            self._filter_chain=nip.VMChain(self._config.allowed_networks)
            self._iptables_rules=[
                # Communications from the host
                nip.Rule("filter", ["-I", "OUTPUT", "-o", self._net_virt.interface, "-j", "DROP"]),
                nip.Rule("filter", ["-I", "OUTPUT", "-o", self._net_virt.interface,
                                    "-j", "LOG", "--log-prefix", "FAIRSHELL-VM-BLOCKED-VM-IN "]),

                nip.Rule("filter", ["-I", "OUTPUT", "-o", self._net_dock_smb.interface, "-j", "DROP"]),
                nip.Rule("filter", ["-I", "OUTPUT", "-o", self._net_dock_smb.interface,
                                    "-j", "LOG", "--log-prefix", "FAIRSHELL-VM-BLOCKED-SMB-IN "]),

                nip.Rule("filter", ["-I", "OUTPUT", "-o", self._net_dock_dns.interface, "-j", "DROP"]),
                nip.Rule("filter", ["-I", "OUTPUT", "-o", self._net_dock_dns.interface,
                                    "-j", "LOG", "--log-prefix", "FAIRSHELL-VM-BLOCKED-DNS-IN "]),

                # Communications from the VM
                # redirect all VM' DNS queries to the local DNS server
                nip.Rule("nat", ["-I", "PREROUTING", "-i", self._net_virt.interface, "-p", "udp", "-m", "udp", "--dport", "53",
                                 "-j", "DNAT", "--to-destination", self._container_dns.ip]),
                nip.Rule("nat", ["-I", "PREROUTING", "-i", self._net_virt.interface, "-p", "tcp", "-m", "tcp", "--dport", "53",
                                 "-j", "DNAT", "--to-destination", self._container_dns.ip]),

                # allow the VM to only perform DHCP requests to the host
                nip.Rule("filter", ["-I", "INPUT", "-i", self._net_virt.interface, "-j", "DROP"]),
                nip.Rule("filter", ["-I", "INPUT", "-i", self._net_virt.interface, "-j", "LOG",
                                    "--log-prefix", "FAIRSHELL-VM-BLOCKED-I "]),

                nip.Rule("filter", ["-I", "INPUT", "-i", self._net_virt.interface, "-p", "udp", "-m", "udp", "--dport", "67", "-j", "ACCEPT"]),
                nip.Rule("filter", ["-I", "OUTPUT", "-o", self._net_virt.interface, "-p", "udp", "-m", "udp", "--dport", "68", "-j", "ACCEPT"]),

                # allow the VM to only open connections to the DNS and SMB server
                nip.Rule("filter", ["-I", "FORWARD", "-i", self._net_virt.interface, "-j", self._filter_chain.name]),
                nip.Rule("filter", ["-I", "FORWARD", "-i", self._net_virt.interface, "-p", "udp", "-m", "udp", "--dport", "53",
                                    "-o", self._net_dock_dns.interface, "-j", "ACCEPT"]),
                nip.Rule("filter", ["-I", "FORWARD", "-i", self._net_virt.interface, "-p", "tcp", "-m", "tcp", "--dport", "445",
                                    "-o", self._net_dock_smb.interface, "-j", "ACCEPT"]),

                # Communications from the DNS server
                #nip.Rule("filter", ["-I", "FORWARD", "-i", self._net_dock_dns.interface, "-p", "udp", "-m", "udp", "!", "--sport", "53", "-j", "DROP"]),
                #nip.Rule("filter", ["-I", "FORWARD", "-i", self._net_dock_dns.interface, "-p", "udp", "-m", "udp", "!", "--sport", "53",
                #                         "-j", "LOG", "--log-prefix", "FAIRSHELL-VM-BLOCKED-DNS-OUT "]),

                # Communications from the SMB server
                nip.Rule("filter", ["-I", "FORWARD", "-i", self._net_dock_smb.interface, "-j", "DROP"]),
                nip.Rule("filter", ["-I", "FORWARD", "-i", self._net_dock_smb.interface,
                                    "-j", "LOG", "--log-prefix", "FAIRSHELL-VM-BLOCKED-SMB-OUT "]),

                nip.Rule("filter", ["-I", "FORWARD", "-i", self._net_dock_smb.interface,
                                    "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"]),
            ]

    def __del__(self):
        self._virt_vm_ensure_undefined()
        run_imagefile=self.get_run_imagefile()
        if os.path.exists(run_imagefile):
            os.remove(run_imagefile)

    @property
    def uid(self):
        return self._uid

    @property
    def gid(self):
        return self._gid

    @property
    def resolv_notif_dir(self):
        """Name of the directory to monitor for sucessful DNS resolutions"""
        return self._resolved_dir

    @property
    def allow_table_name(self):
        """Name of the FW table used to filter communications for the VM"""
        if system_is_nftables:
            return self._nft_table.name
        else:
            return None

    @property
    def allow_chain_name(self):
        """Name of the FW chain used to filter communications for the VM"""
        return self._filter_chain.name

    @property
    def display_mode(self):
        return self._config.display_mode

    @property
    def usb_redir(self):
        return self._config.usb_redir

    @property
    def descr(self):
        return self._config.descr

    def _update_state(self):
        """Compute the current VM's state"""
        if self._state==State.COMMITTING:
            return

        self._lock.acquire()
        current=self._state

        # NB: the Docker container's status is not used here, only the VM is handled
        vm_name=self._config.dom_name
        (status, out, err)=util.exec_sync(["virsh", "-q", "list"], C_locale=True)
        if status==0:
            self._state=State.STOPPED
            for line in out.splitlines():
                parts=line.split()
                if len(parts)==3 and parts[1]==vm_name:
                    if parts[2]=="running":
                        self._state=State.RUNNING
                    else:
                        self._state=State.PARTIAL
                    break

            if self._state!=current and self._state==State.STOPPED:
               self._vm_infra_stop()
        else:
            self._state=None
            syslog.syslog(syslog.LOG_ERR, "Can't get list of running VMs")

        self._lock.release()

    def get_state(self):
        self._update_state()
        return self._state

    def set_dns_servers(self, dns_servers):
        """To be called when the host's DNS settings have changed.
        This function ensures the DNS server for the VM is updated to use the new DNS server(s)
        The @dns_servers should be a list of servers, like ["8.8.8.8"]
        """
        if not isinstance(dns_servers, list):
            raise Exception("CODEBUG: invalid @dns_servers argument: %s"%dns_servers)
        print("Setting DNS servers to: %s"%json.dumps(dns_servers))
        util.write_data_to_file(json.dumps(dns_servers), self._resolv_file)

    def get_spice_listening_port(self):
        """Get the port on which the Spice server is listening for the VM"""
        conn=libvirt.open("qemu:///system")
        dom=conn.lookupByName(self._config.dom_name)
        xml=dom.XMLDesc()
        root=ET.fromstring(xml)
        nodes=root.findall("./devices/graphics")
        for node in nodes:
            if node.attrib["type"]=="spice":
                return int(node.attrib["port"])
        return None

    def _check_vm_base_image_file(self):
        # check that the VM's base image file exists and is owned by libvirt
        if not os.path.exists(self._config.base_image_file):
            syslog.syslog(syslog.LOG_INFO, "VM image file '%s' does not exist"%self._config.base_image_file)
            raise Exception("VM image file '%s' does not exist"%self._config.base_image_file)

        owner=pwd.getpwuid(os.stat(self._config.base_image_file).st_uid).pw_name
        distrib=util.get_distrib()
        if distrib in ("debian", "ubuntu"):
            expowner="libvirt-qemu"
        elif distrib=="fedora":
            expowner="qemu"
        else:
            raise Exception("Unsupported Linux OS flavour '%s'"%distrib)
        if owner!=expowner:
            shutil.chown(self._config.base_image_file, expowner)

        # ensure image access permissions
        os.chmod(self._config.base_image_file, 0o600)

    def get_run_imagefile(self):
        """Get the image file name used by the VM"""
        if self._config.id is None:
            return self._config.base_image_file

        self._check_vm_base_image_file()
        return "%s.%s"%(self._config.base_image_file, self._config.dom_name)

    def _vm_infra_start(self, cancel_requested_func):
        """Start the infrastructure associated to the VM: networkd and services in a Docker container"""
        try:
            # start libvirt's & Docker networks
            for net in (self._net_virt, self._net_dock_smb, self._net_dock_dns):
                net.create()
                if cancel_requested_func and cancel_requested_func():
                    raise evh.Cancelled("Cancelled")

            # check directory to share with the VM
            home_dir=pwd.getpwuid(self._uid).pw_dir
            shared_dir=None
            if self._config.shared_dir:
                if os.path.isabs(self._config.shared_dir):
                    shared_dir=self._config.shared_dir
                else:
                    shared_dir="%s/%s"%(home_dir, self._config.shared_dir)
                _check_mount_directory(shared_dir)
                if not os.path.isdir(shared_dir):
                    if self._uid==0:
                        shared_dir=None
                    else:
                        raise Exception("Directory to share '%s' does not exist"%shared_dir)

            # start docker containers
            env={
                "UID": self._uid,
                "GID": self._gid,
                "SMBPASS": "poorpassword"
            }
            shared=[
                {
                    "host": shared_dir,
                    "cont": "/shared",
                    "mode": "rw"
                }
            ]
            self._container_smb=DockerContainer("fairshell-smb", self._net_dock_smb, 100, env=env, shared_dirs=shared)
            try:
                for cont in (self._container_smb, self._container_dns):
                    cont.create()
                    if cancel_requested_func and cancel_requested_func():
                        raise evh.Cancelled("Cancelled")
            except Exception as e:
                self._container_smb=None
                raise e

            # setup netfilter rules
            if system_is_nftables:
                self._nft_table.add()
                for chain in self._nft_chains:
                    chain.add()
                for rule in self._nft_rules:
                    rule.add()
            else:
                self._filter_chain.install()
                for obj in self._iptables_rules:
                    obj.install()
        except Exception as e:
            self._vm_infra_stop()
            raise e

    def _vm_infra_stop(self):
        """Does the opposite of _vm_infra_start()
        Any error is logged but no exception is raised"""
        # remove netfilter rules
        if system_is_nftables:
            self._nft_table.delete()
        else:
            for obj in self._iptables_rules:
                try:
                    obj.uninstall()
                except Exception as e:
                    syslog.syslog(syslog.LOG_ERR, "Could not remove netfilter rule: %s"%str(e))
            try:
                self._filter_chain.uninstall()
            except Exception as e:
                syslog.syslog(syslog.LOG_ERR, "Could not remove netfilter chain: %s"%str(e))

        # remove the Docker containers
        for cont in (self._container_smb, self._container_dns):
            try:
                if cont:
                    cont.destroy()
            except Exception as e:
                syslog.syslog(syslog.LOG_ERR, "Could not stop Docker container '%s': %s"%(cont.name, str(e)))
        self._container_smb=None

        # stop all networks
        for net in (self._net_virt, self._net_dock_smb, self._net_dock_dns):
            try:
                net.destroy()
            except Exception as e:
                syslog.syslog(syslog.LOG_ERR, "Could not stop network '%s': %s"%(net.name, str(e)))

    def start(self, cancel_requested_func):
        """Starts (killing the existing VM first if necessary)
        Returns: the password to be used to connect to the UI using Spice
        """
        # stop everything first for a fresh start
        self.stop()

        try:
            # load some user specific overwriting config, if any. FIXME: index by VM config ID
            memsize_g=self._config.memsize_g
            nb_cpus=self._config.nb_cpus
            home_dir=pwd.getpwuid(self._uid).pw_dir
            configpath="%s/.config/fairshell/virt-system/config.json"%home_dir
            if os.path.exists(configpath):
                try:
                    conf=json.loads(util.load_file_contents(configpath))
                    if "hardware" in conf:
                        memsize_g=float(conf["hardware"]["mem"])/1024
                        nb_cpus=int(conf["hardware"]["cpu"])
                except Exception as e:
                    raise Exception("Error loading '%s' config file: %s"%(configpath, str(e)))
        
            # ensure CPU virt.extensions are loaded
            (status, out, err)=util.exec_sync(["lsmod"])
            if status!=0:
                raise Exception("Can't run 'lsmod'")
            if "kvm" not in out:
                raise Exception("CPU virtualization extensions are not activated\n(check the BIOS/UEFI settings)")

            # determine the number of CPUs to allocate to the VM (keep 1 for Linux)
            hostcpus=0
            data=util.load_file_contents("/proc/cpuinfo")
            for line in data.splitlines():
                if line.startswith("processor"):
                    hostcpus+=1
            if nb_cpus>=hostcpus:
                nb_cpus=hostcpus-1
            if nb_cpus<1:
                raise Exception("Not enough vCPU available to start Windows")

            # determine the quantity of RAM to allocate to the VM (keep 2 Gb for Linux)
            host_memsize_g=0
            data=util.load_file_contents("/proc/meminfo")
            for line in data.splitlines():
                if line.startswith("MemTotal"):
                    parts=line.split()
                    host_memsize_g=int(parts[1])/2**20

            if memsize_g>=host_memsize_g-2:
                memsize_g=host_memsize_g-2
            if memsize_g<=1:
                raise Exception("Not enough RAM available to start Windows")

            if cancel_requested_func and cancel_requested_func():
                raise evh.Cancelled("Cancelled")

            # disable ipv6
            util.exec_sync(["sysctl", "-w", "net.ipv6.conf.all.disable_ipv6=1"])
            util.exec_sync(["sysctl", "-w", "net.ipv6.conf.default.disable_ipv6=1"])

            # start the whole dedicated infra
            self._vm_infra_start(cancel_requested_func)

            # create VM's HDD clone
            run_imagefile=self.get_run_imagefile()

            if self._config.id is not None:
                if os.path.exists(run_imagefile):
                    os.remove(run_imagefile)
                # cf. https://kashyapc.fedorapeople.org/virt/lc-2012/snapshots-handout.html
                (status, out, err)=util.exec_sync(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", self._config.base_image_file, run_imagefile])
                if status!=0:
                    raise Exception("Failed to create VM image's clone\n'%s' using backing file '%s':\n%s"%(run_imagefile, self._config.base_image_file, err))
                os.chmod(run_imagefile, 0o600)
                shutil.chown(run_imagefile, "libvirt-qemu", "kvm")

            if cancel_requested_func and cancel_requested_func():
                raise evh.Cancelled("Cancelled")

            # start VM
            # Apparmor problems: https://unix.stackexchange.com/questions/435837/how-to-configure-apparmor-so-that-kvm-can-start-guest-that-has-a-backing-file-ch
            password=self._virt_vm_start(run_imagefile, memsize_g, nb_cpus)

            if cancel_requested_func and cancel_requested_func():
                raise evh.Cancelled("Cancelled")

            # wait a bit before allowing the viewer to be started
            for i in range(1, 10):
                if cancel_requested_func and cancel_requested_func():
                    raise evh.Cancelled("Cancelled")
                time.sleep(1)

            return password
        except evh.Cancelled as e:
            syslog.syslog(syslog.LOG_WARNING, "Starting VM cancelled")
            self.stop()
            raise e

        except Exception as e:
            syslog.syslog(syslog.LOG_WARNING, "Could not start VM: %s"%str(e))
            self.stop()
            raise e

    def stop(self):
        """Destroy the VM."""
        try:
            self._virt_vm_ensure_destroyed()
        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, "Error stopping VM: %s"%str(e))
        self._vm_infra_stop()

    def commit(self):
        """Commit the VM changes"""
        if self.get_state()==State.RUNNING:
            raise Exception("VM must be stopped")
        if self._config.writable:
            # commit the clone image file
            run_imagefile=self.get_run_imagefile()
            if not os.path.exists(run_imagefile):
                raise Exception("Commit has already been done, or missing clone file")

            self._state=State.COMMITTING
            syslog.syslog(syslog.LOG_INFO, "Starting comitting clone")
            (status, out, err)=util.exec_sync(["qemu-img", "commit", "-d", run_imagefile])
            self._state=State.STOPPED
            if status!=0:
                syslog.syslog(syslog.LOG_INFO, "Failed to commit the cloned image file '%s': %s"%(run_imagefile, err))
                raise Exception("Failed to commit the cloned image file '%s': %s"%(run_imagefile, err))
            syslog.syslog(syslog.LOG_INFO, "Finished comitting clone")
            os.remove(run_imagefile)
        else:
            raise Exception("Configuration does not allow commiting VM changes")

    #
    # low level stuff
    #
    def _virt_vm_start(self, imagefile, memsize_g, nb_cpus):
        """Starts the VM and returns the password which must be used to connect to the VM using
        the SPICE protocol"""
        syslog.syslog(syslog.LOG_INFO, "VM %s is starting"%self._config_id)

        self._virt_vm_ensure_destroyed()

        # define the VM        
        # https://www.berrange.com/posts/2018/06/29/cpu-model-configuration-for-qemu-kvm-on-x86-hosts/
        args=["virt-install", "--virt-type", "kvm", "--name", self._config.dom_name,
              "--memory", str(int(memsize_g*1024)), "--import",
              "--disk", "%s,bus=virtio,cache=none"%imagefile, "--os-variant", self._config.os_variant, "--vcpus", str(nb_cpus),
              "--noreboot", "--noautoconsole"]
        if self._config.mac_addr:
            args+=["--network", "network=%s,model=virtio,mac=%s"%(self._net_virt.interface, self._config.mac_addr)]
        else:
            args+=["--network", "network=%s,model=virtio"%self._net_virt.interface]

        password=None
        if self._config.display_mode!=DisplayMode.NONE:
            password=util.generate_password()
            args+=["--graphics", "spice,password=%s"%password, "--video", "qxl", "--channel", "spicevmc", "--sound"]

        if self._config.iso_boot:
            args+=["--disk", "path=%s,device=cdrom,boot_order=1"%self._config.iso_boot]

        for isofile in self._config.extra_iso_images:
            if "," in isofile:
                raise Exception("ISO image file '%s' can't contain the comma character"%isofile)
            args+=["--disk", "path=%s,device=cdrom"%isofile]

        (status, out, err)=util.exec_sync(args)
        if status!=0:
            syslog.syslog(syslog.LOG_ERR, "Could not define the VM: %s"%err)
            raise Exception("Could not define the VM: %s"%err)

        # start the VM
        (status, out, err)=util.exec_sync(["virsh", "start", self._config.dom_name], C_locale=True)
        if status!=0:
            syslog.syslog(syslog.LOG_ERR, "Could not start VM: %s"%err)
            raise Exception("Could not start VM: %s"%err)

        return password

    def _virt_vm_ensure_destroyed(self):
        # destroy VM if it's running
        (status, out, err)=util.exec_sync(["virsh", "list", "--all"], C_locale=True)
        if status!=0:
            syslog.syslog(syslog.LOG_ERR, "Could not list virt. VMs: %s"%err)
            raise Exception("Could not list virt. VMs: %s"%err)

        for line in out.splitlines():
            parts=line.split()
            if len(parts)>=3 and parts[1]==self._config.dom_name and (parts[2]=="running" or parts[2]=="paused"):
                (status, out, err)=util.exec_sync(["virsh", "destroy", self._config.dom_name], C_locale=True)
                if status!=0:
                    syslog.syslog(syslog.LOG_ERR, "Could not destroy VM '%s': %s"%(self._config.dom_name, err))
                    raise Exception("Could not destroy VM '%s': %s"%(self._config.dom_name, err))
                break

        self._virt_vm_ensure_undefined()

    def _virt_vm_ensure_undefined(self):
        # undefine VM if it's defined
        (status, out, err)=util.exec_sync(["virsh", "list", "--all"], C_locale=True)
        if status!=0:
            syslog.syslog(syslog.LOG_ERR, "Could not list virt. VMs: %s"%err)
            raise Exception("Could not list virt. VMs: %s"%err)

        for line in out.splitlines():
            parts=line.split()
            if len(parts)>=3 and parts[1]==self._config.dom_name:
                (status, out, err)=util.exec_sync(["virsh", "undefine", "--nvram", self._config.dom_name], C_locale=True)
                if status!=0:
                    syslog.syslog(syslog.LOG_ERR, "Could not undefine VM '%s': %s"%(self._config.dom_name, err))
                    raise Exception("Could not undefine VM '%s': %s"%(self._config.dom_name, err))
                break


    