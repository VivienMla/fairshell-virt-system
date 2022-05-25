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


import os
import sys
import json
import time
import dbus
import dbus.mainloop.glib
from gi.repository import GLib
import syslog
import argparse
import tempfile

import Utils as util

_timer_delay=2000

class VMRunner:
    """Start a VM and a viewer, and regularly check if both are still running"""
    def __init__(self, main_loop, dbus_proxy, conf_id):
        self._main_loop=main_loop
        self._proxy=dbus_proxy
        self._conf_id=conf_id
        self._proxy.connect_to_signal("started", self._vm_started, dbus_interface="org.fairshell.VMManager")
        self._proxy.connect_to_signal("start_error", self._vm_start_error, dbus_interface="org.fairshell.VMManager")
        self._proxy.connect_to_signal("committed", self._vm_committed, dbus_interface="org.fairshell.VMManager")
        self._proxy.connect_to_signal("commit_error", self._vm_commit_error, dbus_interface="org.fairshell.VMManager")

        self._vm_running=None
        self._viewer_proc=None # remote viewer Popen

        self._commit_state=False

    @property
    def vm_running(self):
        return self._vm_running

    @property
    def vm_commit_state(self):
        return self._commit_state

    def kill_viewer(self):
        if self._viewer_proc is not None:
            self._viewer_proc.kill()
            self._viewer_proc=None

    def _check_state(self, with_viewer):
        """Regularly check if both the VM and the viewer are still alive.
        If any of them failed, then the regular check stops.
        """
        # determine if viewer is still running
        viewer_running=True
        if with_viewer:
            if self._viewer_proc:
                vres=self._viewer_proc.poll() # None if viewer is still running
                viewer_running=vres is None
            else:
                viewer_running=False

        # determine if VM is still running
        self._vm_running=True
        try:
            state=self._proxy.get_state(self._conf_id)
        except Exception: # maybe the DBus server failed!
            self._main_loop.quit()
            return False # stop regular check

        if state=="STOPPED":
            self._vm_running=False

        if not (viewer_running and self._vm_running):
            self._main_loop.quit()
            return False # stop regular check
        return True # keep the timer

    def wait_vm(self):
        """Wait until the VM or the viewer have been stopped"""
        GLib.timeout_add(_timer_delay, self._check_state, False) # regularly check if the VM or the viewer have stopped
        self._main_loop.run()

    #
    # VM start handling
    #
    def _vm_started(self, id, uid, gid):
        if id==self._conf_id:
            print("VM started, running the viewer and waiting for it to be closed or the VM to be shut down")
            try:
                self._viewer_proc=util.run_viewer(self._conf_id)
                GLib.timeout_add(_timer_delay, self._check_state, True)
            except Exception as e:
                print("%s"%str(e))
                self._proxy.stop(self._conf_id)
                self._main_loop.quit()

    def _vm_start_error(self, id, uid, gid, reason):
        if id==self._conf_id:
            print("VM failed to start: %s"%reason)
            self._main_loop.quit()

    #
    # VM commit handling, to define self._commit_state
    #
    def _vm_committed(self, id, uid, gid):
        #print("COMMIT SIGNAL for '%s'"%id)
        if id==self._conf_id:
            self._commit_state=True

    def _vm_commit_error(self, id, uid, gid, reason):
        #print("COMMIT ERROR SIGNAL for '%s'"%id)
        if id==self._conf_id:
            self._commit_state=Exception(reason)    

parser=argparse.ArgumentParser()
parser.add_argument("-v", "--verbose", help="Display more information", action="store_true")

subparsers=parser.add_subparsers(help="Allowed commands", dest="cmde")

sparser=subparsers.add_parser("install", help="Install a new VM")
sparser.add_argument("out_vm_image", metavar="out-vm-image", help="VM image file to create")
sparser.add_argument("boot_iso", metavar="boot-iso", help="OS boot installation ISO file")
sparser.add_argument("--extra", action="append", help="Extra software resources (ISO files or other)")
sparser.add_argument("--disk-size", metavar="disk_size", default=32768, help="Disk size in Mb (32768 by default)")
sparser.add_argument("--mem-size", metavar="mem_size", default=3072, help="Disk size in Mb (3072 by default)")
sparser.add_argument("--allow-resolv", action="append", help="Allowed resolved names (can be specified multiple times")
sparser.add_argument("--allow-network", action="append", help="Allowed network (can be specified multiple times")

sparser=subparsers.add_parser("run", help="Runs (and optionaly update if conf. allows) a VM")
sparser.add_argument("id", help="ID of the configuration to use")

sparser=subparsers.add_parser("discard", help="Discard a running VM")
sparser.add_argument("id", help="ID of the configuration to use")

sparser=subparsers.add_parser("list-available", help="List VM configurations available")
sparser=subparsers.add_parser("list-vm", help="List started VM")

sparser=subparsers.add_parser("status", help="Get the status of a VM")
sparser.add_argument("id", help="ID of the configuration to get status of")

sparser=subparsers.add_parser("discard-all", help="Discard all running VMs")

args=parser.parse_args()

def _common_vm_run(proxy, conf_id):
    """Run a VM, start a viewer and handle the rest
    Returns: True if the VM image was mofidied
    """
    # verifications
    state=proxy.get_state(conf_id)
    if state=="RUNNING":
        raise Exception("VM with Id '%s' is already running"%conf_id)

    vm_conf=proxy.get_configuration(conf_id)
    vm_conf=json.loads(vm_conf)
    writable=vm_conf["writable"]

    # run the VM and the viewer, and
    # use a main loop to "wait" until the VM or the viewer is stopped
    main_loop=GLib.MainLoop()
    handler=VMRunner(main_loop, proxy, conf_id)
    proxy.start(conf_id)

    # run the main loop which returns when the viewer has been closed
    # or the VM has stopped
    main_loop.run()

    # stop VM if necessary
    state=proxy.get_state(conf_id)
    default_commit=True
    if state=="RUNNING":
        # stop the VM
        default_commit=False
        proxy.stop(conf_id)
        handler.wait_vm()
    else:
        # kill the viewer
        handler.kill_viewer()

    # commit or discard?
    retval=False
    if writable:
        # ask what to do
        if default_commit:
            commit=input("Commit changes [Y/n]: ")
        else:
            commit=input("Commit changes [y/N]: ")
        if commit in ("y", "Y") or (default_commit and commit==""):
            proxy.commit(conf_id)
            print("Commit started")
            handler.wait_vm()
            if handler.vm_commit_state==True:
                print("Commit Ok")
                retval=True
            else:
                print("Commit error: %s"%str(handler.vm_commit_state))
    proxy.undefine(conf_id)
    return retval

def _do_install(args, proxy):
    # add VM conf. to perform the install
    temp_conf=tempfile.NamedTemporaryFile("w")
    conf_data={
        "install": {
            "disk-size": 25600,
            "boot-iso": args.boot_iso,
            "resources": []
        },
        "run": {
            "vm-imagefile": args.out_vm_image,
            "os-variant": "macosx10.5",
            "usb-redir": "all",
            "hardware": {
                "mem": args.mem_size,
                "cpu": 2,
                "mac-addr": None
            },
            "resolved-names": args.allow_resolv if args.allow_resolv is not None else [],
            "allowed-networks": args.allow_network if args.allow_network is not None else []
        }
    }
    if args.extra:
        for path in args.extra:
            conf_data["install"]["resources"]+=[path]
    temp_conf.write(json.dumps(conf_data))
    temp_conf.flush()
    conf_id=proxy.install_conf_prepare(temp_conf.name)
    temp_conf=None # remove TMP file
    os.environ["VIEWER_CONSOLE_SIZE"]="1"
    done=_common_vm_run(proxy, conf_id)
    if not done:
        # remove empty VM image
        os.remove(args.out_vm_image)

try:
    # DBus connection
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus=dbus.SystemBus()
    obj=bus.get_object("org.fairshell.VMManager", "/remote/virtualmachines")
    proxy=dbus.Interface(obj, dbus_interface="org.fairshell.VMManager")

    if args.cmde is None:
        print("%s"%parser.format_help())
    elif args.cmde=="install":
        _do_install(args, proxy)
    elif args.cmde=="run":
        _common_vm_run(proxy, args.id)
    elif args.cmde=="discard":
        state=proxy.get_state(args.id)
        if state!="STOPPED":
            proxy.stop(args.id)
            while True:
                time.sleep(1)
                state=proxy.get_state(args.id)
                if state=="STOPPED":
                    break
        proxy.undefine(args.id)
    elif args.cmde=="list-available":
        allvms=proxy.get_configurations()
        for id in allvms:
            print("%s: %s"%(id, allvms[id][0]))
    elif args.cmde=="list-vm":
        allvms=proxy.get_virtual_machines()
        for entry in allvms:
            print("%s"%entry)
    elif args.cmde=="status":
        state=proxy.get_state(args.id)
        print("%s"%state)
    elif args.cmde=="discard-all":
        proxy.discard_all()
        while True:
            vms=proxy.get_virtual_machines()
            if len(vms)==0:
                break
            time.sleep(1)
    else:
        raise Exception("CODEBUG: unknown '%s' command"%args.cmde)
except Exception as e:
    #raise e
    print("Error: %s"%str(e), file=sys.stderr)
    sys.exit(1)
