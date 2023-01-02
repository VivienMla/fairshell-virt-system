#!/usr/bin/python3

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

#
# This program manages the Windows VM (start, destroy, etc.)
# it sets up a DBUS service on the system bus for all accepted commands.

import sys
import os
import syslog
import json
import datetime
import signal
import pyinotify
import netaddr
from gi.repository import GLib

import dbus
import dbus.service

sys.path+=[os.path.dirname(os.path.realpath(sys.argv[0]))]
import Utils as util
import VM

import EventsHub as evh

if VM.system_is_nftables:
    import NetworkNftables as nft
else:
    import NetworkIptables as nip

_install_reserved_id_prefix="_install_"

def get_timestamp():
    """Get the current Unix timestamp as UTC"""
    now=datetime.datetime.utcnow()
    return int(datetime.datetime.timestamp(now))

class DNSWatcher(evh.InotifyComponent):
    """Watch for HOST DNS servers changes and call a predefined callback function with the new list of DNS
    servers.
    It uses either the NetworkManager's DnsManager object if it exists, or the /etc/resolv.conf file.
    """
    def __init__(self, callback_func):
        self._callback_func=callback_func
        evh.InotifyComponent.__init__(self)
        try:
            bus=dbus.SystemBus()
            self._nm=bus.get_object("org.freedesktop.NetworkManager",
                                    "/org/freedesktop/NetworkManager/DnsManager")
            self._nm.connect_to_signal("PropertiesChanged", self._nm_prop_changed,
                                       dbus_interface="org.freedesktop.DBus.Properties")
            syslog.syslog(syslog.LOG_INFO, "Using /org/freedesktop/NetworkManager/DnsManager as DNS source")
        except Exception:
            # NetworkManager may not have the /org/freedesktop/NetworkManager/DnsManager object
            self._nm=None
            syslog.syslog(syslog.LOG_INFO, "Using /etc/resolv.conf as DNS source")
            self.watch("/etc/resolv.conf", pyinotify.IN_MOVED_TO | pyinotify.IN_CLOSE_WRITE)

        # generate initial version of the file
        self._ns_list=["1.1.1.1", "8.8.8.8", "9.9.9.9"]
        self._update_resolvers()

    @property
    def ns_list(self):
        return self._ns_list

    def _update_resolvers(self):
        ns_list=[]
        if self._nm:
            interface=dbus.Interface(self._nm, "org.freedesktop.DBus.Properties")
            conf=interface.Get("org.freedesktop.NetworkManager.DnsManager", "Configuration")
            for item in conf:
                for ns in item["nameservers"]:
                    ns=str(ns)
                    if ns not in ns_list:
                        if ":" not in ns: # we only want IPV4 for now
                            ns_list+=[ns]
        else:
            for line in util.load_file_contents("/etc/resolv.conf").splitlines():
                if line.startswith("nameserver "):
                    try:
                        (dummy, ns)=line.split()
                        if ns not in ns_list:
                            if not ":" in ns:
                                ns_list+=[ns]
                    except Exception:
                        pass # malformed line ???
        self._ns_list=ns_list

    def inotify_handler(self, event):
        self._ns_changed()

    def _nm_prop_changed(self, interface, changed_properties, invalidated_properties):
        if "Configuration" in changed_properties:
            self._ns_changed()

    def _ns_changed(self):
        # called wheneved the list of name servers has changed
        self._update_resolvers()
        syslog.syslog(syslog.LOG_INFO, "Updated list of DNS servers: %s"%self._ns_list)
        self._callback_func(self._ns_list)

class AllowedIPs:
    """This object allows IPs via the Linux's netfilter chain specified at creation, and removes that
    authorization once the TTL has been reached"""
    def __init__(self, allow_table_name, allow_chain_name):
        self._chain=None
        self._by_ttl={} # key: unix TS corresponding to the TTLs, value= IPs list expiring @ the TTL
        self._by_ips={}  # key: ip address, value= unix TS of the TTL for that IP address

        # define netfilter chain
        if VM.system_is_nftables:
            self._table=nft.Table(allow_table_name) # should already be present
            self._chain=nft.Chain(self._table, allow_chain_name, None, None) # should already be present
            self._rules={} # key=IP address, value= associated nft.Rule (needed to be able to uninstall rules)
        else:
            self._chain=nip.Chain("filter", allow_chain_name)
            self._chain.install()

        # timeout management
        self._tid=None
        self._timeout_ttl=None # unix TS for the TTL for which the timeout is defined

    def _dump_state(self):
        if False:
            print("ALLOWED by TTL: %s"%json.dumps(self._by_ttl, indent=4, sort_keys=True))
            print("ALLOWED by IPs: %s"%json.dumps(self._by_ips, indent=4, sort_keys=True))

    def add_allowed(self, ip, ttl):
        ip=netaddr.IPAddress(ip) # ensure @ip's format is correct
        ip=str(ip)

        now=get_timestamp()
        nttl=now+ttl+60 # keep IP allowed for a minute more

        # test if ip is already allowed
        if ip in self._by_ips:
            ettl=self._by_ips[ip]
            if abs(ettl-nttl)<3:
                # nothing to do here
                return

            # remove information related to @ip
            entry=self._by_ttl[ettl]
            entry.remove(ip)
            if len(entry)==0:
                del self._by_ttl[ettl]
            del self._by_ips[ip]

        # take into account new IP and TTL
        if not nttl in self._by_ttl:
            self._by_ttl[nttl]=[]
        entry=self._by_ttl[nttl]
        if ip not in entry:
            entry+=[ip]
        self._by_ips[ip]=nttl

        # actually allow IP address
        syslog.syslog(syslog.LOG_INFO, "ALLOWING IP address %s (TTL %s)"%(ip, ttl))
        if VM.system_is_nftables:
            rule=nft.Rule(self._chain, ["ip", "daddr", "%s/32"%ip, "accept"])
            rule.add()
            self._rules[ip]=rule
        else:
            rule=nip.Rule("filter", ["-I", self._chain.name, "-d", "%s/32"%ip, "-j", "ACCEPT"])
            rule.install()

        # (re)define the next timeout for the next TTL expiring
        if self._tid:
            GLib.source_remove(self._tid)
            self._tid=None

        self._define_next_timeout()

        self._dump_state()

    def _define_next_timeout(self):
        # previous timeout, if any, must have been undefined
        assert self._tid is None
        k=list(self._by_ttl.keys())
        if len(k)>0:
            k.sort()
            self._timeout_ttl=k[0]
            now=get_timestamp()
            to=max(self._timeout_ttl-now, 10) # we want to avoid negative timers (in case PC was suspended for example)
            self._tid=GLib.timeout_add((to)*1000, self._ttl_timed_out)

    def _ttl_timed_out(self):
        for ip in self._by_ttl[self._timeout_ttl]:
            syslog.syslog(syslog.LOG_INFO, "Denying access to %s (expired)"%ip)
            del self._by_ips[ip]
            if VM.system_is_nftables:
                rule=self._rules[ip]
                try:
                    rule.delete()
                except:
                    pass
                del self._rules[ip]
            else:
                rule=nip.Rule("filter", ["-I", self._chain.name, "-d", "%s/32"%ip, "-j", "ACCEPT"])
                rule.uninstall()
        del self._by_ttl[self._timeout_ttl]

        self._timeout_ttl=None
        self._tid=None
        self._define_next_timeout()
        self._dump_state()
        return False # remove this timeout

class ResolvedIpWatcher(evh.InotifyComponent):
    """Get information of the unbound server's resolved names, and adapt the
    iptables rules according to the resolved IPs and TTLs.

    Each resolved IP is in a JSON file in the @dirname directory, with a contents like:
    [{'TTL': 86252, 'A': '209.82.215.200', 'AAAA': None}]
    """
    def __init__(self, allow_table_name, allow_chain_name, dirname):
        evh.InotifyComponent.__init__(self)
        self._ips=AllowedIPs(allow_table_name, allow_chain_name)
        os.makedirs(dirname, exist_ok=True)
        os.chmod(dirname, 0o777)
        self.watch(dirname, pyinotify.IN_MOVED_TO | pyinotify.IN_CLOSE_WRITE)

        # NB: the existing authorised IPs are ignored as we don't know their associated TTL
        #     and as te risk of being a security risk is low. We can't remove them because
        #     this could lead to some legitimate traffic being denied (as the TTL for the DNS server
        #     might not be reached and the responses might still be in its cache)

    def inotify_handler(self, event):
        try:
            syslog.syslog(syslog.LOG_INFO, "Detected new allowed IP info (file '%s')"%event.pathname)
            data=json.loads(open(event.pathname, "r").read())
            for entry in data: # only use IPV4 for now
                if entry["A"]:
                    self._ips.add_allowed(entry["A"], entry["TTL"])
            syslog.syslog(syslog.LOG_INFO, "Removing IP info file '%s'"%event.pathname)
            os.remove(event.pathname)
        except Exception as e:
            syslog.syslog(syslog.LOG_WARNING, "Error treating file '%s': %s"%(event.pathname, str(e)))

class ManagedVM(VM.VM):
    """VM.VM object with support for the Manager"""
    def __init__(self, id, config, uid, gid):
        assert isinstance(config, VM.VMConfig)
        VM.VM.__init__(self, config, uid, gid)
        self._id=id
        self._start_job_id=None
        self._stop_job_id=None
        self._commit_job_id=None
        self._ui_config=None

    @property
    def id(self):
        return self._id

    @property
    def start_job_id(self):
        """ID of the job run in the background to start the VM"""
        return self._start_job_id

    @start_job_id.setter
    def start_job_id(self, job_id):
        self._start_job_id=job_id
        print("VM %s, user %s.%s start job ID: %s"%(self._id, self.uid, self.gid, job_id))

    @property
    def stop_job_id(self):
        """ID of the job run in the background to stop the VM"""
        return self._stop_job_id

    @stop_job_id.setter
    def stop_job_id(self, job_id):
        print("VM %s stop job ID: %s"%(self._id, job_id))
        self._stop_job_id=job_id

    @property
    def commit_job_id(self):
        """ID of the job run in the background to commit the VM"""
        return self._commit_job_id

    @commit_job_id.setter
    def commit_job_id(self, job_id):
        self._commit_job_id=job_id
        print("VM %s, user %s.%s commit job ID: %s"%(self._id, self.uid, self.gid, job_id))

    @property
    def ui_config(self):
        """UI configuration of the started VM"""
        return self._ui_config

    @ui_config.setter
    def ui_config(self, config):
        self._ui_config=config

class Manager(evh.DBusServer):
    """Manages VM defined by their configurations"""
    def __init__(self, conf_filename, hub):
        evh.DBusServer.__init__(self, True, "org.fairshell.VMManager", "/remote/virtualmachines")
        self._hub=hub

        # define VM objects from config
        self._vms={} # key=config ID, value=list of ManagedVM objects
        self._confs={} # key=config ID, value=VM.VMConfig object
        conf=json.loads(util.load_file_contents(conf_filename))
        for id in conf:
            try:
                if id.startswith(_install_reserved_id_prefix):
                    raise Exception("ID '%s' is reserved"%id)
                config=VM.VMConfig(id, conf[id])
                print("Loaded '%s' configuration"%id)
                self._vms[id]=[]
                self._confs[id]=config
            except Exception as e:
                syslog.syslog(syslog.LOG_ERR, "Failed to load configuration '%s': %s"%(id, str(e)))
                print("Ignored configuration '%s': %s"%(id, str(e)))
        self._discarding_all=False

        self.run_dir="/run/fairshell-virt-system" # hard coded in the systemd unit file
        logs_dir="/var/log/fairshell-virt-system" # hard coded in the systemd unit file
        os.makedirs(logs_dir, exist_ok=True)
        os.chmod(logs_dir, 0o777)

        signal.signal(signal.SIGINT, self._exit_gracefully)
        signal.signal(signal.SIGTERM, self._exit_gracefully)

        self._dns_watcher=None

    @property
    def dns_watcher(self):
        return self._dns_watcher
    
    @dns_watcher.setter
    def dns_watcher(self, dns_watcher):
        assert isinstance(dns_watcher, DNSWatcher)
        self._dns_watcher=dns_watcher

    def dns_list_update_cb(self, ns_list):
        """Callback function for when the list of DNS servers has changed"""
        print("== dns_list_update_cb")
        for id in self._vms:
            print("== for conf ID %s"%id)
            for vmo in self._vms[id]:
                print("== for VMO %s"%vmo)
                vmo.set_dns_servers(ns_list)

    def _exit_gracefully(self, signum, frame):
        """Function called when time comes to kill the Windows VM"""
        syslog.syslog(syslog.LOG_INFO, "Exiting gracefully (service killed)")
        self._do_discard_all()
        sys.exit(0)

    def _do_discard_all(self):
        self._discarding_all=True
        allvmo=[]
        for id in self._vms:
            allvmo+=self._vms[id].copy()

        # stop VMs
        for vmo in allvmo:
            try:
                vmo.auto_undefine=True
                syslog.syslog(syslog.LOG_INFO, "Discarding all VMs: VM '%s', user %s.%s"%(vmo.id, vmo.uid, vmo.gid))
                if vmo.get_state() in (VM.State.RUNNING, VM.State.PARTIAL):
                    self._stop(vmo.id, vmo.uid, vmo.gid)
            except Exception as e:
                syslog.syslog(syslog.LOG_ERR, "Error while discarding VM '%s', user %s.%s: %s"%(vmo.id, vmo.uid, vmo.gid, str(e)))
        allvmo=None

        self._discarding_all=False

    #
    # Configurations management
    #
    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus", out_signature="a{sas}")
    def get_configurations(self, sender=None, bus=None):
        """List the configurations available to the caller"""
        (uid, gid)=self.get_user_ident(sender, bus)
        res={}
        for id in self._confs:
            config=self._confs[id]
            if config.user_allowed(uid, gid):
                res[id]=[config.descr]
        return res

    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus", in_signature="s", out_signature="s")
    def get_configuration(self, id, sender=None, bus=None):
        """List the configurations available to the caller"""
        (uid, gid)=self.get_user_ident(sender, bus)
        if id in self._confs:
            config=self._confs[id]
            if config.user_allowed(uid, gid):
                data={
                    "id": id,
                    "descr": config.descr,
                    "image-file": config.base_image_file,
                    "writable": config.writable
                }
                return json.dumps(data)
        raise Exception("No configuration '%s' available"%id)

    def _get_vmo(self, id, uid, gid):
        """Get the VM object which has been started by user uid.gid,
        Returns None if no VM has been started"""
        vmo=None
        if id not in self._vms:
            raise Exception("Unknown VM ID '%s'"%id)
        for _vmo in self._vms[id]:
            if _vmo.uid==uid and _vmo.gid==gid:
                vmo=_vmo
                break
        return vmo

    #
    # Status querying
    #
    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus", out_signature="as")
    def get_virtual_machines(self, sender=None, bus=None):
        """Get the list of VM of the user, as a list of "<config ID>:<uid>.<gid>"
        If called by root, returns a list of all the VMs
        """
        (uid, gid)=self.get_user_ident(sender, bus)
        res=[]
        for id in self._vms:
            for vmo in self._vms[id]:
                if (uid==0 and gid==0) or (vmo.uid==uid and vmo.gid==gid):
                    res+=["%s:%s.%s"%(vmo.id, vmo.uid, vmo.gid)]
        return res

    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus", in_signature="s", out_signature="s")
    def get_state(self, id, sender=None, bus=None):
        """Get the state of a VM"""
        (uid, gid)=self.get_user_ident(sender, bus)
        state=VM.State.STOPPED
        vmo=self._get_vmo(id, uid, gid)
        if vmo:
            state=vmo.get_state().value
        return state

    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus", in_signature="sii", out_signature="s")
    def get_state_ext(self, id, uid, gid, sender=None, bus=None):
        """Get the state of a VM, reserved to root if uid or gid are not the same as the caller"""
        (cuid, cgid)=self.get_user_ident(sender, bus)
        state=VM.State.STOPPED
        if (cuid!=0 or cgid!=0) and (uid!=cuid or gid!=cgid):
            raise Exception("Must be root")
        vmo=self._get_vmo(id, uid, gid)
        if vmo:
            state=vmo.get_state().value
        return VM.State.STOPPED

    #
    # VM starting
    #
    @dbus.service.signal("org.fairshell.VMManager")
    def starting(self, id, uid, gid):
        """Signal that the VM is starting."""
        syslog.syslog(syslog.LOG_INFO, "'started' signal emitted for conf '%s', user %s.%s"%(id, uid, gid))

    @dbus.service.signal("org.fairshell.VMManager")
    def started(self, id, uid, gid):
        """Signal that the VM has started."""
        syslog.syslog(syslog.LOG_INFO, "'started' signal emitted for conf '%s', user %s.%s"%(id, uid, gid))

    @dbus.service.signal("org.fairshell.VMManager")
    def start_error(self, id, uid, gid, reason):
        """Signal that the VM could not be started (and is stopped).
        """
        syslog.syslog(syslog.LOG_INFO, "'start_error' signal emitted for conf '%s', user %s.%s: %s"%(id, uid, gid, reason))

    def _start_job(self, cancel_requested_func, args):
        # executed in its own thread
        # job to start the VM
        # Returns: the password to connect to the VM's UI
        vmo=args["vm-object"]
        passwd=vmo.start(cancel_requested_func)
        vmo.set_dns_servers(self._dns_watcher.ns_list)
        return passwd

    def _start_done_callback(self, job_id):
        """Called when the VM start job has finished (VM has thus started) or failed"""
        # Identify the VM associated object
        vmo=None
        for id in self._vms:
            for _vmo in self._vms[id]:
                if _vmo.start_job_id==job_id:
                    vmo=_vmo
                    break
            if vmo:
                break
        if vmo is None:
            raise Exception("CODEBUG: could not identify VM object with start_job_id=%s"%job_id)

        # final handling
        try:
            _vmo.start_job_id=None
            password=self.job_get_result(job_id)
            syslog.syslog(syslog.LOG_INFO, "VM '%s' started for %s.%s"%(vmo.id, vmo.uid, vmo.gid))
            conf=""
            if vmo.display_mode!=VM.DisplayMode.NONE:
                spice_port=vmo.get_spice_listening_port()
                conf={
                    "port": spice_port,
                    "password": password,
                    "fullscreen": True if vmo.display_mode==VM.DisplayMode.FULLSCREEN else False,
                    "usb-redir": vmo.usb_redir,
                    "title": vmo.descr
                }
                vmo.ui_config=json.dumps(conf)
            self.started(vmo.id, vmo.uid, vmo.gid)
        except evh.Cancelled as e:
            syslog.syslog(syslog.LOG_INFO, "VM '%s' start cancelled: %s"%(id, str(e)))
            self._vms[vmo.id].remove(vmo)
            self.stopped(vmo.id, vmo.uid, vmo.gid)
        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, "VM '%s' start failed: %s"%(id, str(e)))
            self._vms[vmo.id].remove(vmo)
            self.start_error(vmo.id, vmo.uid, vmo.gid, str(e))

    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus", in_signature="s")
    def start(self, id, sender=None, bus=None):
        """Start the VM. Connect to the started() or stopped() signals to be informed of the outcome"""
        (uid, gid)=self.get_user_ident(sender, bus)
        vmo=self._get_vmo(id, uid, gid)
        if vmo:
            if vmo.stop_job_id:
                raise Exception("Can't start VM '%s', stop has already been requested"%id)
            return
        if self._discarding_all:
            raise Exception("Root requested discarding all VMs")

        vmo=ManagedVM(id, self._confs[id], uid, gid)
        vmo.auto_undefine=False
        self._vms[id]+=[vmo]
        rip=ResolvedIpWatcher(vmo.allow_table_name, vmo.allow_chain_name, vmo.resolv_notif_dir)
        self._hub.register(rip)
        vmo.rip=rip
        syslog.syslog(syslog.LOG_INFO, "START requested for VM '%s'"%id)
        args={
            "vm-object": vmo,
        }
        self.starting(vmo.id, vmo.uid, vmo.gid)
        vmo.start_job_id=self.job_run(self._start_job, args, self._start_done_callback)
        syslog.syslog(syslog.LOG_INFO, "start job ID for VM '%s', user %s.%s: %s"%(id, uid, gid, vmo.start_job_id))

    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus", in_signature="s")
    def get_ui_access(self, id, sender=None, bus=None):
        """Get the settings required to connect to the UI of a VM"""
        (uid, gid)=self.get_user_ident(sender, bus)
        vmo=self._get_vmo(id, uid, gid)
        if vmo is None or vmo.get_state() not in (VM.State.RUNNING, VM.State.PARTIAL):
            raise Exception("VM not running")
        if vmo.ui_config is None:
            raise Exception("VM is running without any display")
        return vmo.ui_config

    #
    # VM stop
    #
    @dbus.service.signal("org.fairshell.VMManager")
    def stopping(self, id, uid, gid):
        """Signal that the VM is stopping."""
        syslog.syslog(syslog.LOG_INFO, "VM stopping, conf '%s', user %s.%s"%(id, uid, gid))

    @dbus.service.signal("org.fairshell.VMManager")
    def stopped(self, id, uid, gid):
        """Signal that the VM has stopped."""
        syslog.syslog(syslog.LOG_INFO, "VM stopped, conf '%s', user %s.%s"%(id, uid, gid))

    def _stop_job(self, cancel_requested_func, args):
        # # executed in its own thread
        # job to stop the VM
        # (the @cancel_requested_func argument is required by the evh.DBusServer object even though it's not used here)
        vmo=args["vm-object"]
        vmo.stop()

    def _stop_done_callback(self, job_id):
        # called when the VM stop job has finished or failed
        vmo=None
        for id in self._vms:
            for _vmo in self._vms[id]:
                if _vmo.stop_job_id==job_id:
                    vmo=_vmo
                    break
            if vmo:
                break
        if vmo is None:
            syslog.syslog(syslog.LOG_ERR, "Could not identify VM object with stop_job_id=%s"%job_id)
            return

        try:
            vmo.stop_job_id=None
            self.job_get_result(job_id)
            self.stopped(vmo.id, vmo.uid, vmo.gid)

            if vmo.auto_undefine:
                self._vms[vmo.id].remove(vmo)
                vmo=None
                syslog.syslog(syslog.LOG_INFO, "VM undefined, conf '%s' because of auto undefine"%id)

        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, "Error while handling post VM stop: %s"%str(e))

    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus", in_signature="s")
    def stop(self, id, sender=None, bus=None):
        """Stop the VM."""
        (uid, gid)=self.get_user_ident(sender, bus)
        self._stop(id, uid, gid)

    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus")
    def discard_all(self, sender=None, bus=None):
        """Discard all the VM
        Reserved to root"""
        (uid, gid)=self.get_user_ident(sender, bus)
        if uid!=0 or gid!=0:
            raise Exception("Must be root")
        self._do_discard_all()

    def _stop(self, id, uid, gid):
        vmo=self._get_vmo(id, uid, gid)
        if vmo is None:
            return
        if vmo.stop_job_id:
            return

        syslog.syslog(syslog.LOG_INFO, "STOP VM '%s', user %s.%s requested"%(id, uid, gid))
        if vmo.start_job_id:
            # VM is starting, issue a job cancel request
            self.job_cancel(vmo.start_job_id)
        else:
            rip=vmo.rip
            if rip:
                self._hub.unregister(rip)
                rip.stop()
                vmo.rip=None
            self.stopping(id, uid, gid)
            args={
                "vm-object": vmo,
            }
            vmo.stop_job_id=self.job_run(self._stop_job, args, self._stop_done_callback)

    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus", in_signature="s")
    def undefine(self, id, sender=None, bus=None):
        """Get rid of a VM once it has been stopped"""
        (uid, gid)=self.get_user_ident(sender, bus)
        vmo=self._get_vmo(id, uid, gid)
        if vmo:
            state=vmo.get_state()
            if state==VM.State.STOPPED and vmo.stop_job_id is None:
                self._vms[vmo.id].remove(vmo)
                vmo=None
                syslog.syslog(syslog.LOG_INFO, "VM undefined, conf '%s', user %s.%s"%(id, uid, gid))
            else:
                syslog.syslog(syslog.LOG_INFO, "requested undefining VM, conf '%s', user %s.%s"%(id, uid, gid))
                vmo.auto_undefine=True
            

    #
    # VM committing changes
    #
    @dbus.service.signal("org.fairshell.VMManager")
    def committing(self, id, uid, gid):
        """Signal that the VM is committing."""
        syslog.syslog(syslog.LOG_INFO, "VM committing, conf '%s', user %s.%s"%(id, uid, gid))

    @dbus.service.signal("org.fairshell.VMManager")
    def commit_error(self, id, uid, gid, reason):
        """Signal that the VM commit has failed."""
        syslog.syslog(syslog.LOG_INFO, "VM commit error, conf '%s', user %s.%s: %s"%(id, uid, gid, reason))

    @dbus.service.signal("org.fairshell.VMManager")
    def committed(self, id, uid, gid):
        """Signal that the VM has been committed."""
        syslog.syslog(syslog.LOG_INFO, "VM committed, conf '%s', user %s.%s"%(id, uid, gid))

    def _commit_job(self, cancel_requested_func, args):
        # # executed in its own thread
        # job to commit the VM
        # (the @cancel_requested_func argument is required by the evh.DBusServer object even though it's not used here)
        vmo=args["vm-object"]
        vmo.commit()

    def _commit_done_callback(self, job_id):
        # called when the VM commit job has finished or failed
        vmo=None
        for id in self._vms:
            for _vmo in self._vms[id]:
                if _vmo.commit_job_id==job_id:
                    vmo=_vmo
                    break
            if vmo:
                break
        if vmo is None:
            syslog.syslog(syslog.LOG_ERR, "Could not identify VM object with commit_job_id=%s"%job_id)
            return

        try:
            vmo.commit_job_id=None
            self.job_get_result(job_id)
            self.committed(vmo.id, vmo.uid, vmo.gid)
        except Exception as e:
            self.commit_error(vmo.id, vmo.uid, vmo.gid, str(e))
            syslog.syslog(syslog.LOG_ERR, "Error while VM commit: %s"%str(e))

    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus", in_signature="s")
    def commit(self, id, sender=None, bus=None):
        """Commit the changes to a VM once it has been stopped"""
        (uid, gid)=self.get_user_ident(sender, bus)
        vmo=self._get_vmo(id, uid, gid)
        if vmo:
            if vmo.get_state() not in (VM.State.STOPPED, VM.State.PARTIAL):
                raise Exception("Can't commit VM '%s', not stopped"%id)

            if vmo.commit_job_id:
                # already committing
                return

            self.committing(id, uid, gid)
            args={
                "vm-object": vmo,
            }
            vmo.commit_job_id=self.job_run(self._commit_job, args, self._commit_done_callback)
            syslog.syslog(syslog.LOG_INFO, "commit job ID for VM '%s', user %s.%s: %s"%(id, uid, gid, vmo.commit_job_id))

    #
    # VM install, specific because the configuration does not yet exist and
    # is specified by the caller, and because the shared directory points to XXX
    #
    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus", in_signature="s")
    def install_conf_prepare(self, config_file, sender=None, bus=None):
        """Create a new configuration to install a VM
        @config_file must be a full path to a VM configuration file
        Returns: the new VM configuration ID"""
        (uid, gid)=self.get_user_ident(sender, bus)
        if uid!=0:
            raise Exception("Must be root")

        if not os.path.isabs(config_file) or not os.path.exists(config_file):
            raise Exception("Invalid path to configuration file '%s'"%config_file)
        try:
            conf_data=json.load(open(config_file, "r"))
        except:
            raise Exception("Invalid configuration file '%s'"%config_file)

        if "install" not in conf_data or "run" not in conf_data:
            raise Exception("Invalid configuration file '%s'"%config_file)
        conf_data_run=conf_data["run"]
        conf_data_install=conf_data["install"]

        # prepare VMConfig
        now=datetime.datetime.utcnow()
        id=_install_reserved_id_prefix+str(int(datetime.datetime.timestamp(now)))
        conf_data_run["descr"]="Install"
        conf_data_run["netmode"]="NAT"
        conf_data_run["shared-dir"]=None
        conf_data_run["display"]="window"
        conf_data_run["writable"]=True
        conf_data_run["allowed-users"]=["root"]
        conf_obj=VM.VMConfig(id, conf_data_run, image_file_must_exist=False)

        # prepare install specifics
        for key in ("disk-size", "boot-iso", "resources"):
            if key not in conf_data_install:
                raise Exception(f"Invalid configuration: no attribute '{key}'")

        boot_iso=conf_data_install["boot-iso"]
        if not os.path.isabs(boot_iso) or not os.path.exists(boot_iso):
            raise Exception("No boot ISO file '%s'"%boot_iso)

        if os.path.exists(conf_obj.base_image_file):
            raise Exception("File '%s' already exists"%conf_obj.base_image_file)
        size="%sM"%conf_data_install["disk-size"]
        args=["qemu-img", "create", "-f", "qcow2", conf_obj.base_image_file, size]
        (status, out, err)=util.exec_sync(args)
        if status!=0:
            raise Exception("Could not create VM image file '%s': %s"%(imagefile, err))

        extra=conf_data_install["resources"]
        if extra is None:
            extra=[]
        (iso_images, tmpiso)=util.get_iso_images_list(extra)
        conf_obj.extra_iso_images=iso_images
        conf_obj.iso_boot=boot_iso
        if tmpiso:
            conf_obj.extra_iso_images+=[tmpiso.name]
            conf_obj.keep_tmp_obj_ref(tmpiso) # keep a reference on the TMP file

        self._vms[conf_obj.id]=[]
        self._confs[conf_obj.id]=conf_obj
        return conf_obj.id

    @dbus.service.method("org.fairshell.VMManager", sender_keyword="sender", connection_keyword="bus", in_signature="s")
    def install_conf_remove(self, id, sender=None, bus=None):
        """Performs the opposite of install_conf_prepare()"""
        (uid, gid)=self.get_user_ident(sender, bus)
        if uid!=0:
            raise Exception("Must be root")
        assert isinstance(id, str)
        if not id.startswith(_install_reserved_id_prefix):
            raise Exception("Invalid ID '%s'"%id)
        if id not in self._confs:
            raise Exception("ID '%s' not found"%id)

        vmo=self._get_vmo(id, uid, gid)
        if vmo:
            try:
                syslog.syslog(syslog.LOG_INFO, "Discarding install VMs: VM '%s', user %s.%s"%(vmo.id, vmo.uid, vmo.gid))
                if vmo.get_state() in (VM.State.RUNNING, VM.State.PARTIAL):
                    self._stop(vmo.id, vmo.uid, vmo.gid)
            except Exception as e:
                syslog.syslog(syslog.LOG_ERR, "Error while discarding install VM '%s', user %s.%s: %s"%(vmo.id, vmo.uid, vmo.gid, str(e)))
            self._vms[id].remove(vmo)
        del self._vms[id]
        del self._confs[id]


#
# Main
#
try:
    if not util.is_run_as_root():
        raise Exception("This programm must be run as root")

    hub=evh.Hub()
    manager=Manager("/etc/fairshell-virt-system.json", hub)
    hub.register(manager)

    dns_watcher=DNSWatcher(manager.dns_list_update_cb)
    hub.register(dns_watcher)
    manager.dns_watcher=dns_watcher

    hub.run()

except Exception as e:
    syslog.syslog(syslog.LOG_ERR, "ERROR: %s"%str(e))
    print("Error: %s"%str(e))
    #raise e
    exit(1)
