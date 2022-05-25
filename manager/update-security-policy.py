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
# Run this file whenever you have updated the "vm-imagefile" value in the
# configuration file, it will update the policies allowing the new file
# to be used as a VM disk image

import os
import sys
import json
import Utils as util

def restart_service():
    args=["systemctl", "restart", "fairshell-virt-system"]
    (status, out, err)=util.exec_sync(args)
    if status!=0:
        raise Exception("Could not restart the 'fairshell-virt-system' service: %s"%err)

def adjust_debian_apparmor(config):
    """adjust Debian/Ubuntu apparmor policy"""
    imgdirs=[]
    if config:
        for id in config:
            imgdir=os.path.dirname(config[id]["vm-imagefile"])
            if imgdir not in imgdirs:
                imgdirs+=[imgdir]

    if os.path.exists("/etc/apparmor.d/local/abstractions"):
        apparmor_profile="/etc/apparmor.d/local/abstractions/libvirt-qemu"
    else:
        apparmor_profile="/etc/apparmor.d/abstractions/libvirt-qemu"
    if os.path.exists(apparmor_profile):
        data=util.load_file_contents(apparmor_profile)
        new=data.splitlines()
    else:
        new=[]
    for path in imgdirs:
        line="%s/* rk,"%path
        new+=[line]

    new+=["", ""]
    util.write_data_to_file("\n".join(new), apparmor_profile)

try:
    if not util.is_run_as_root():
        raise Exception("This program must be run as root")

    config=None # will be None when unstalling the system
    if len(sys.argv)==1:
        configfile="/etc/fairshell-virt-system.json"
        if os.path.exists(configfile):
            config=json.loads(util.load_file_contents(configfile))
    elif len(sys.argv)==2:
        if sys.argv[1]=="-h" or sys.argv[1]=="--help":
            print("%s [-u]"%sys.argv[0])
            exit(0)
        if sys.argv[1]!="-u":
            raise Exception("Unknown option '%s', use -h"%sys.argv[1])
    else:
        raise Exception("Unknown arguments, use '-h'")

    distrib=util.get_distrib()
    if  distrib in ("debian", "ubuntu"):
        adjust_debian_apparmor(config)

    if config:
        restart_service()
except Exception as e:
    print("Error: %s"%str(e))
    sys.exit(1)
