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
import time
import pwd
import grp
import json
import enum
import shutil
import tempfile
import netaddr
import xml.etree.ElementTree as ET
import libvirt
import Utils as util
import EventsHub as evh

def _nft_cmd(verb, obj, nft_args, context):
    """Runs the nft command
    If @verb is "add", returns the new object's handle (as a string)
    """
    assert verb in ("add", "delete")
    assert isinstance(nft_args, list)
    assert isinstance(context, str)
    if verb=="add":
        args=["/sbin/nft", "-a", "-e", verb] # so we can get the handle
    else:
        args=["/sbin/nft", verb]
    if isinstance(obj, Table):
        args+=["table", "ip", obj.name]
    elif isinstance(obj, Chain):
        args+=["chain", "ip", obj.table.name, obj.name]+nft_args
    elif isinstance(obj, Rule):
        args+=["rule", "ip", obj.table.name, obj.chain.name]+nft_args
    else:
        raise Exception("Unknown @obj type %s"%type(obj))

    (status, out, err)=util.exec_sync(args)
    if status!=0:
        msg="nft error while %s: %s"%(context, err)
        syslog.syslog(syslog.LOG_ERR, msg)
        raise Exception(msg)
    else:
        handle=None
        for line in out.splitlines():
            if "# handle" in out:
                parts=line.split()
                handle=parts[-1]
                break
        #msg="nft %s: %s"%(context, nft_args)
        #syslog.syslog(syslog.LOG_INFO, msg)
        return handle

class Table:
    """Represents an nftables IP table"""
    def __init__(self, name):
        assert isinstance(name, str) and name!=""
        self._name=name

    @property
    def name(self):
        return self._name

    def add(self):
        _nft_cmd("add", self, [], "Table add")

    def delete(self):
        (status, out, err)=util.exec_sync(["/sbin/nft", "list", "table", self._name])
        if status==0:
            _nft_cmd("delete", self, [], "Table delete")

class Chain:
    """Represents a, nftable chain within a table"""
    def __init__(self, table, name, ctype, hook, priority=0):
        assert isinstance(table, Table)
        assert isinstance(name, str) and name!=""
        assert ctype is None or ctype in ("nat", "filter")
        assert hook is None or hook in ("prerouting", "input", "output", "forward")
        assert isinstance(priority, int)
        self._table=table
        self._name=name
        self._type=ctype
        self._hook=hook
        self._priority=priority

    @property
    def table(self):
        return self._table

    @property
    def name(self):
        return self._name

    def add(self):
        if self._type is None:
            # regular chain
            args=[]
        else:
            # base chain
            args=["{", "type", self._type, "hook", self._hook, "priority", str(self._priority), ";", "}"]
        _nft_cmd("add", self, args, "Chain add")

class Rule:
    """Represents a rule in an nftables chain"""
    def __init__(self, chain, args):
        assert isinstance(chain, Chain)
        assert isinstance(args, list)
        self._chain=chain
        self._args=args
        self._handle=None

    @property
    def table(self):
        return self._chain.table

    @property
    def chain(self):
        return self._chain

    def add(self):
        self._handle=_nft_cmd("add", self, self._args, "Rule add")

    def delete(self):
        if self._handle is None:
            raise Exception("Rule has no handle")
        # if the chain does not exist anymore, nothing needs to be done
        (status, out, err)=util.exec_sync(["/sbin/nft", "list", "chain", "ip", self.table.name, self.chain.name])
        if status==0:
            _nft_cmd("delete", self, ["handle", self._handle], "Rule delete")

