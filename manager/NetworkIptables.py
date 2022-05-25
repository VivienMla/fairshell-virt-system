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

import syslog
import netaddr
import Utils as util

# iptables timeout constant
_iptables_lock_wait=20

def _iptables_cmd(table, iptable_args, context):
    assert table in ("nat", "filter")
    args=["/sbin/iptables", "-w", str(_iptables_lock_wait), "-t", table]+iptable_args
    (status, out, err)=util.exec_sync(args)
    if status!=0:
        msg="Iptables error while %s: %s"%(context, err)
        syslog.syslog(syslog.LOG_ERR, msg)
        raise Exception(msg)
    else:
        msg="Iptables %s: table %s, rule: %s"%(context, table, " ".join(iptable_args))
        syslog.syslog(syslog.LOG_INFO, msg)

class Chain:
    """Represents an iptables chain"""
    def __init__(self, table, chain_name):
        assert table in ("nat", "filter")
        assert isinstance(chain_name, str)
        self._table=table
        self._name=chain_name

    @property
    def table(self):
        """Table on which the chain works"""
        return self._table

    @property
    def name(self):
        """Name of the filter chain"""
        return self._name

    @property
    def installed(self):
        """Tells if the chain is defined"""
        args=["/sbin/iptables", "-w", str(_iptables_lock_wait), "-t", self._table, "-S", self._name]
        (status, out, err)=util.exec_sync(args)
        if status==0:
            return True
        elif "No chain/target/match by that name." in err:
            return False
        else:
            raise Exception("Could not determine if iptables chain '%s' exists: %s"%(self._name, err))

    def install(self):
        """Install the chain"""
        if not self.installed:
            _iptables_cmd(self._table, ["-N", self._name], "installing chain '%s'"%self._name)

    def uninstall(self):
        """Uninstall the chain"""
        if self.installed:
            _iptables_cmd(self._table, ["-F", self._name], "flushing chain '%s'"%self._name)
            _iptables_cmd(self._table, ["-X", self._name], "uninstalling chain '%s'"%self._name)

class VMChain(Chain):
    """Represents the iptables chain used to filter the VM's communications"""
    chainindex=0
    def __init__(self, allowed_networks):
        assert isinstance(allowed_networks, list)
        # create chain
        VMChain.chainindex+=1
        Chain.__init__(self, "filter", "FAIRSHELL-VM-%s"%VMChain.chainindex)
        self._allowed_networks=allowed_networks

    def install(self):
        Chain.install(self)

        # remove any rule
        _iptables_cmd(self.table, ["-F", self.name], "flushing chain '%s'"%self.name)
        _iptables_cmd(self.table, ["-A", self.name, "-j", "LOG", "--log-prefix", "FAIRSHELL-VM-BLOCKED-F "], "installing chain '%s'"%self.name)
        _iptables_cmd(self.table, ["-A", self.name, "-j", "DROP"], "installing chain '%s'"%self.name)

        # allow validated networks
        nets=self._allowed_networks.copy()
        nets.reverse()
        for net in nets:
            network=netaddr.IPNetwork(net)
            _iptables_cmd(self.table, ["-I", self.name, "-d", str(network), "-j", "ACCEPT"], "allow network %s"%net)

class Rule:
    """Represents a single iptables rule"""
    def __init__(self, table, args):
        assert table in ("nat", "filter")
        assert isinstance(args, list)
        assert args[0] in ("-I", "-A")
        self._table=table
        self._args=args

    @property
    def installed(self):
        """Tells if the rule is present"""
        args=["/sbin/iptables", "-w", str(_iptables_lock_wait), "-t", self._table, "-C"]+self._args[1:]
        (status, out, err)=util.exec_sync(args)
        if status==0:
            return True
        elif "Bad rule" in err or \
                "No chain/target/match by that name." in err:
            return False
        else:
            raise Exception("Could not determine if iptables rule '%s' exists: %s"%(args, err))

    def install(self):
        """Install the rule"""
        # we don't check if the rule is installed as it might already be, but at a wrong place
        _iptables_cmd(self._table, self._args, "setting up rule '%s'"%" ".join(self._args))

    def uninstall(self):
        """Uninstall the rule"""
        if self.installed:
            args=self._args.copy()
            args[0]="-D"
            _iptables_cmd(self._table, args, "setting up rule")
