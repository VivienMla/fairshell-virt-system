#!/usr/bin/python3

# -*- coding: utf-8 -*-
#
# Copyright 2022 Vivien Malerba <vmalerba@gmail.com>
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

import sys
import dbus
import json
import gi
import argparse
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
import VMUI

# command line arguments
parser=argparse.ArgumentParser()
parser.add_argument("id", help="VM configuration to use")
parser.add_argument("--console-size", help="Leave the window's size to its default console size", action="store_true")
args=parser.parse_args()

# get the VM connection's parameters
bus=dbus.SystemBus()
obj=bus.get_object("org.fairshell.VMManager", "/remote/virtualmachines")
proxy=dbus.Interface(obj, dbus_interface='org.fairshell.VMManager')

# Gtk program
def quit_cb(dummy, vmui):
    vmui.session_disconnect()
    Gtk.main_quit()

def fullscreen_cb(self, fullscreen, main_window):
    if fullscreen:
        main_window.fullscreen()
    else:
        main_window.unfullscreen()

try:
    win=Gtk.Window()
    viewer_config=proxy.get_ui_access(args.id)
    viewer_config=json.loads(viewer_config)
    vmui=VMUI.VMUI(viewer_config)
    win.add(vmui)
    vmui.session_connect()
    win.connect("destroy", quit_cb, vmui)
    vmui.actions.connect("close", quit_cb, vmui)
    vmui.actions.connect("fullscreen", fullscreen_cb, win)
    win.show_all()
    if not args.console_size:
        (w,h)=VMUI.get_sane_default_vmui_size()
        win.resize(w, h)
    Gtk.main()
except Exception as e:
    print("ERROR: %s"%str(e), file=sys.stderr)
    sys.exit(1)