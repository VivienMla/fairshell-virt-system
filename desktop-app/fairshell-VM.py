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

import sys
import os
import json
import syslog
import dbus
import dbus.service
import dbus.mainloop.glib
import gi
import argparse
import xdg.DesktopEntry
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk
from gi.repository import GdkPixbuf
from gi.repository import GLib
from gi.repository import Gio
import VMUI

class SingleApplication(dbus.service.Object):
    """Singleton DBus client for the specified VM"""
    # https://stackoverflow.com/questions/14088297/how-can-i-manually-invoke-dbus-service-signal-decorator
    def __init__(self, conf_id, main_window):
        self._app_main_window=main_window

        bus=dbus.SessionBus()
        try:
            # register service name on the bus
            name=dbus.service.BusName("org.fairshell.VMRunner-%s"%conf_id, bus, do_not_queue=True)
            # register service
            dbus.service.Object.__init__(self, name, "/main_window")
        except Exception as e:
            print("VM is already running...")
            # tell the existing instance to come to the foreground
            obj=bus.get_object("org.fairshell.VMRunner-%s"%conf_id, "/main_window")
            proxy=dbus.Interface(obj, dbus_interface='org.fairshell.VMRunner')
            proxy.present()
            sys.exit(0)

    @dbus.service.method("org.fairshell.VMRunner")
    def present(self):
        self._app_main_window.present()

class VMRunner(SingleApplication):
    """Actual object to run the VM: a UI with
    - a window to display the VM starting and status report
    - a VM viewer window"""
    def __init__(self, conf_id, spawn_viewer):
        self._app_desktop=None # name of the app as when run by a .desktop file
        self._proxy=None
        self._conf_id=conf_id
        self._spawn_viewer=spawn_viewer

        # app. name & icon: locate and use the .desktop being used if possible
        app_name="Virtual machine"
        app_base=""
        app_icon=None
        if "GIO_LAUNCHED_DESKTOP_FILE" in os.environ:
            de_path=os.environ["GIO_LAUNCHED_DESKTOP_FILE"]
            self._app_desktop=os.path.basename(de_path)
            de=xdg.DesktopEntry.DesktopEntry(de_path)
            app_name=de.getGenericName() # this is the key used to 'bind' the GtkWindow to the .desktop file
            app_base=os.path.basename(de_path)[:-8] # remove the ".desktop"
            app_icon=de.getIcon()
        else:
            print("Not launched using a .desktop, UI information is generic")
        GLib.set_prgname(app_base)
        self._app_name=app_name

        # UI part
        self._builder=Gtk.Builder()
        self._builder.add_from_file("%s/fairshell-VM.ui"%os.path.dirname(__file__))
        self._main_window=self._builder.get_object("main")
        self._main_window.set_title(self._app_name)
        self._main_nb=self._builder.get_object("main-nb")
        self._spinner=self._builder.get_object("spinner")

        self._cancel_button=self._builder.get_object("cancel")
        self._close_button=self._builder.get_object("close")
        self._builder.connect_signals(self)
        self._main_window.show()
        self._vmui_window=None

        # initialise SingleApplication
        SingleApplication.__init__(self, self._conf_id, self._main_window)

        # DBus proxy to access the VM manager
        self._connect_dbus_proxy()

        # Get VM's config infos
        self._vm_infos=json.loads(self._proxy.get_configuration(self._conf_id))
        img=self._builder.get_object("vm-icon")
        if app_icon:
            if not os.path.isabs(app_icon):
                app_icon="%s/icons/%s"%(os.path.realpath(os.path.dirname(__file__)), app_icon)
            pix=GdkPixbuf.Pixbuf.new_from_file_at_size(app_icon, -1, 128)
            img.set_from_pixbuf(pix)
        else:
            img.hide()
        label=self._builder.get_object("vm-descr")
        label.set_markup("<b><span size='x-large'>%s</span></b>"%self._vm_infos["descr"])

    def _connect_dbus_proxy(self):
        try:
            bus=dbus.SystemBus()
            obj=bus.get_object("org.fairshell.VMManager", "/remote/virtualmachines")
            proxy=dbus.Interface(obj, dbus_interface='org.fairshell.VMManager')
            proxy.connect_to_signal("started", self._vm_started, dbus_interface="org.fairshell.VMManager")
            proxy.connect_to_signal("start_error", self._vm_start_error, dbus_interface="org.fairshell.VMManager")
            proxy.connect_to_signal("stopped", self._vm_stopped, dbus_interface="org.fairshell.VMManager")
            self._proxy=proxy
            return True
        except Exception as e:
            self._proxy=None # service is not available
            return False

    #
    # UI handling
    #
    def _show_spinner(self, message, can_cancel):
        self._main_nb.set_current_page(0)
        self._cancel_button.show()
        self._cancel_button.set_sensitive(can_cancel)
        self._close_button.hide()
        label=self._builder.get_object("spinnermessage")
        label.set_text(message)
        self._spinner.start()

    def _show_message(self, message):
        self._main_nb.set_current_page(0)
        self._cancel_button.show()
        self._cancel_button.set_sensitive(False)
        self._close_button.hide()
        label=self._builder.get_object("spinnermessage")
        label.set_text(message)
        self._spinner.stop()

    def _show_error(self, error_message, error_details=None):
        self._main_nb.set_current_page(1)
        self._cancel_button.hide()
        self._close_button.show()
        label=self._builder.get_object("errormessage")
        label.set_text(error_message)
        exp=self._builder.get_object("errorexp")
        if error_details:
            label=self._builder.get_object("errordetails")
            label.set_text(error_details)
        else:
            exp.hide()

    def _quit_cb(self, button):
        Gtk.main_quit()

    def _cancel_cb(self, button):
        print("Start cancel")
        self._proxy.stop(self._conf_id)
        self._cancel_button.show()
        self._cancel_button.set_sensitive(False)
        self._close_button.hide()
        label=self._builder.get_object("spinnermessage")
        label.set_text("Shutting down")

    #
    # VM handling
    #
    def start(self):
        try:
            if self._proxy:
                if self._proxy.get_state(self._conf_id)!="STOPPED":
                    self._show_error("Already running...")
                else:
                    self._proxy.start(self._conf_id)
                    self._show_spinner("Starting", can_cancel=True)
            else:
                self._show_error("VMManager service is not available")
        except Exception as e:
            print("Could not start VM: %s"%str(e))

    def _vmui_quit_cb(self, dummy):
        # VMUI closed => stopping VM"
        try:
            self._vmui_window.hide()
            self._show_spinner("Shutting down", can_cancel=False)
            self._main_window.show()
            self._proxy.stop(self._conf_id)
        except dbus.exceptions.DBusException:
            if self._connect_dbus_proxy():
                self._proxy.stop(self._conf_id)
                self._vm_stopped()
            else:
                # proxy not available, quitting
                self._vm_stopped()
        except Exception as e:
            # some other error, quitting
            self._vm_stopped()

    def _vmui_fullscreen_cb(self, dummy, fullscreen):
        if fullscreen:
            self._vmui_window.fullscreen()
        else:
            self._vmui_window.unfullscreen()

    def _vm_started(self, id, uid, gid):
        if id!=self._conf_id:
            return
        self._show_message("VM is running")
        if not self._spawn_viewer:
            print("No viewer required, leaving program.")
            Gtk.main_quit()
            return

        try:
            viewer_config=self._proxy.get_ui_access(self._conf_id)
            viewer_config=json.loads(viewer_config)
            vmui=VMUI.VMUI(viewer_config)
            vmui.actions.has_keyboard=False
            self._vmui_window=Gtk.Window()
            self._vmui_window.add(vmui)
            vmui.session_connect()
            vmui.show()
            self._vmui_window.show()
            if viewer_config["fullscreen"]:
                self._vmui_window.fullscreen()
            self._vmui_window.set_title(self._app_name)
            (w,h)=VMUI.get_sane_default_vmui_size()
            self._vmui_window.resize(w, h)

            self._vmui_window.connect("destroy", self._vmui_quit_cb)
            vmui.actions.connect("close", self._vmui_quit_cb)
            vmui.actions.connect("fullscreen", self._vmui_fullscreen_cb)

            self._main_window.hide()
        except Exception as e:
            msg="Could not start UI viewer: %s"%str(e)
            syslog.syslog(syslog.LOG_ERR, msg)
            print("%s"%msg)

    def _vm_start_error(self, id, uid, gid, reason):
        if id!=self._conf_id:
            return

        print("VM failed to start: %s"%reason)
        self._show_error("Failed to start", reason)

    def _vm_stopped(self, id, uid, gid):
        if id!=self._conf_id:
            return
        # VM stopped, exiting program
        self._proxy.undefine(self._conf_id)
        Gtk.main_quit()

#
# main
#
parser=argparse.ArgumentParser()
parser.add_argument("id", help="VM configuration to use")
parser.add_argument("--noviewer", help="Don't start the viewer", action="store_true")
args=parser.parse_args()

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
obj=VMRunner(args.id, not args.noviewer)
GLib.timeout_add(1, obj.start)
Gtk.main()
