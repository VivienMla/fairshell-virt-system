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

import gi
import enum
import syslog
import Utils as util
gi.require_version("Gtk", "3.0")
from gi.repository import GObject
from gi.repository import Gtk
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk
gi.require_version('SpiceClientGLib', '2.0')
from gi.repository import SpiceClientGLib
gi.require_version('SpiceClientGtk', '3.0')
from gi.repository import SpiceClientGtk

class DeviceType(str, enum.Enum):
    """USB device types"""
    MASS_STORAGE = "mass-storage"
    SMARTCARD = "smartcard"
    OTHER = "other"

class DeviceState(str, enum.Enum):
    """USB device states with regards to the VM"""
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTING = "DISCONNECTING"

_mapping={
    "8": DeviceType.MASS_STORAGE,
    #"11": DeviceType.SMARTCARD # for future evolution
}

def _identify_device_type(vendor_product):
    """Identifies an USB device and returns (DeviceType, human description)"""
    human=None
    (status, out, err)=util.exec_sync(["lsusb", "-v", "-d", vendor_product])
    if status!=0:
        raise Exception("Could not get infos. about USB device %s"%vendor_product)
    for line in out.splitlines():
        if line.startswith("Bus ") and vendor_product in line:
            # get the product's user friendly description 
            pos=line.rfind(vendor_product)+len(vendor_product)+1
            human=line[pos:]
        else:
            line=line.strip()
            if line.startswith("bInterfaceClass"):
                parts=line.split()
                if parts[1] in _mapping:
                    return (_mapping[parts[1]], human)
    return (DeviceType.OTHER, human)

class UsbDevice(GObject.GObject):
    """Represents an USB device which can be connected to the VM"""
    # notation:
    # 'usb_dev' usually represents a SpiceUsbDevice
    # 'dev' usually represents a UsbDevice object
    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, usb_dev, descr):
        GObject.GObject.__init__(self)
        self._usb_dev=usb_dev
        self._descr=descr
        self._state=DeviceState.DISCONNECTED

    @property
    def descr(self):
        return self._descr

    @property
    def usb_dev(self):
        """SpiceUsbDevice associated to the current object"""
        return self._usb_dev

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, state):
        state=DeviceState(state)
        if self._state!=state:
            self._state=state
            self.emit("state-changed")


class VMActions(Gtk.Grid):
    """Top banner showing the different actions on the VM (fulscreen, devices management, and window close)"""
    __gsignals__ = {
        "fullscreen": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "close": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, vmui):
        Gtk.Grid.__init__(self)
        self._vmui=vmui
        self._session=vmui.spice_session
        usb_dm=SpiceClientGLib.UsbDeviceManager.get(self._session)
        usb_dm.connect("device-added", self._device_added_cb)
        usb_dm.connect("device-removed", self._device_removed_cb)
        usb_dm.connect("device-error", self._device_error_cb)

        self._usb_redir_classes=["all"]
        self._devices={} # key = a SpiceUsbDevice, value=UsbDevice, if device is presented to the user
        self._devices_analyzed=False # devices are analysed the 1st time they are required, not before as the
                                     # session may not yet be fully "connected" and redirection may not be possible
        self._devices_popover=None
        self._devices_grid=None
        self._devices_grid_vindex=0
        self._keyboard_popover=None
        self._reveal=Gtk.Revealer()
        self.attach(self._reveal, 0, 0, 1, 1)
        self._reveal.show()
        self.reveal()
        self.set_property("halign", Gtk.Align.CENTER)

        # enabled features
        self._has_keyboard=True

        bb=Gtk.HBox()
        self._reveal.add(bb)

        # fullscreen
        image=Gtk.Image.new_from_icon_name("view-fullscreen", Gtk.IconSize.DIALOG)
        button=Gtk.ToggleButton()
        button.set_tooltip_text("Toggle fullscreen")
        button.set_image(image)
        bb.add(button)
        self._fullscreen_button=button
        self._fullscreen_button_sigid=button.connect("toggled", self._button_fullscreen_cb)
        self._vmui.connect("size-allocate", self._size_allocate_cb, button)

        # USB devices to connect
        button=Gtk.Button(label="USB devices")
        button.set_tooltip_text("Transfer USB devices")
        bb.add(button)
        button.connect("clicked", self._button_devices_cb)
        self._dev_button=button
        self._dev_button.connect("show", self._dev_button_show_cb)

        # send keyboard keys combinations
        button=Gtk.Button(label="Keyboard")
        button.set_tooltip_text("Send keyboard events")
        bb.add(button)
        button.connect("clicked", self._button_keyboard_cb)
        self._keyb_button=button
        self._keyb_button.connect("show", self._keyb_button_show_cb)

        # close button
        image=Gtk.Image.new_from_icon_name("window-close", Gtk.IconSize.MENU)
        button=Gtk.Button()
        button.set_tooltip_text("Close")
        button.set_image(image)
        bb.add(button)
        button.connect("clicked", self._button_close_cb)

        bb.show_all()

    def _size_allocate_cb(self, widget, rect, toggle_button):
        # ensure that the toggle button's position is always on par with the actual window state
        topwin=widget.get_ancestor(Gtk.Window)
        if topwin:
            gdkwin=topwin.get_window()
            if gdkwin:
                is_full=True if gdkwin.get_state() & Gdk.WindowState.FULLSCREEN else False
                GObject.signal_handler_block(self._fullscreen_button, self._fullscreen_button_sigid)
                toggle_button.set_active(is_full)
                GObject.signal_handler_unblock(self._fullscreen_button, self._fullscreen_button_sigid)

    def _dev_button_show_cb(self, dummy):
        if len(self._usb_redir_classes)==0:
            self._dev_button.hide()
        else:
            self._dev_button.show()

    def _keyb_button_show_cb(self, dummy):
        if self._has_keyboard:
            self._keyb_button.show()
        else:
            self._keyb_button.hide()

    @property
    def has_keyboard(self):
        return self._has_keyboard

    @has_keyboard.setter
    def has_keyboard(self, value):
        self._has_keyboard=value
        self._keyb_button_show_cb(None)

    @property
    def usb_redir_classes(self):
        return self._usb_redir_classes

    @usb_redir_classes.setter
    def usb_redir_classes(self, classes):
        self._usb_redir_classes=classes
        self._dev_button_show_cb(None)

    def _send_key_cb(self, button, codes):
        display=self._vmui.spice_display
        display.send_keys(codes, SpiceClientGtk.DisplayKeyEvent.PRESS)
        display.send_keys(codes, SpiceClientGtk.DisplayKeyEvent.RELEASE)
        self._keyboard_popover.hide()

    def _button_keyboard_cb(self, button):
        if self._keyboard_popover is None:
            popover=Gtk.Popover()
            popover.set_relative_to(self._keyb_button)
            
            grid=Gtk.Grid()
            grid.set_row_spacing(10)
            grid.set_column_spacing(10)
            grid.set_property("row-spacing", 0)

            keys={
                "Ctrl+Alt+Del": [Gdk.KEY_Control_L, Gdk.KEY_Alt_L, Gdk.KEY_Delete],
                "Ctrl+Alt+BackSpace": [Gdk.KEY_Control_L, Gdk.KEY_Alt_L, Gdk.KEY_BackSpace],
                "Ctrl+Alt+F1": [Gdk.KEY_Control_L, Gdk.KEY_Alt_L, Gdk.KEY_F1],
                "Ctrl+Alt+F2": [Gdk.KEY_Control_L, Gdk.KEY_Alt_L, Gdk.KEY_F2],
                "Ctrl+Alt+F3": [Gdk.KEY_Control_L, Gdk.KEY_Alt_L, Gdk.KEY_F3],
                "Ctrl+Alt+F4": [Gdk.KEY_Control_L, Gdk.KEY_Alt_L, Gdk.KEY_F4],
                "Ctrl+Alt+F5": [Gdk.KEY_Control_L, Gdk.KEY_Alt_L, Gdk.KEY_F5]
            }
            top=0
            for combo in keys:
                button=Gtk.Button(label=combo)
                button.connect("clicked", self._send_key_cb, keys[combo])
                button.set_property("relief", Gtk.ReliefStyle.NONE)
                grid.attach(button, 0, top, 1, 1)
                top+=1

            popover.add(grid)
            popover.show_all()
            self._keyboard_popover=popover
        else:
            self._keyboard_popover.show()

    #
    # devices handling
    #
    def _device_added_cb(self, usb_dm, usb_dev):
        """Signalled by Spice's device manager: a device has been added"""
        syslog.syslog(syslog.LOG_INFO, "Device added: %s"%usb_dev)
        if self._devices_analyzed:
            dev=self._analyze_usb_device(usb_dev)
            if dev is not None and self._devices_popover:
                self._add_device_entry(dev)
        else:
            # stash the device to be analysed later
            self._devices[usb_dev]=None

    def _device_removed_cb(self, usb_dm, usb_dev):
        """Signalled by Spice's device manager: a device has been removed"""
        syslog.syslog(syslog.LOG_INFO, "Device removed: %s"%usb_dev)
        dev=self._devices[usb_dev]
        if dev is not None and self._devices_popover:
            self._remove_device_entry(dev)
        del self._devices[usb_dev]

    def _device_error_cb(self, usb_dm, usb_dev, error):
        """Signalled by Spice's device manager: a device has issued an error"""
        syslog.syslog(syslog.LOG_ERR, "Device error: %s / %s"%(usb_dev, error))
        del self._devices[usb_dev]

    def _analyze_usb_device(self, usb_dev):
        """Analyse a specific USB device, and determine if it can be "connected"
        to the VM"""
        root_dev=util.get_root_live_partition(exception_if_no_live=False)
        usb_dm=SpiceClientGLib.UsbDeviceManager.get(self._session)
        descr=usb_dev.get_description("%s|%s|%s|%d|%d")
        parts=descr.split("|")
        add_to_redirect=False
        try:
            vp=parts[-3] # like [1b2c:1a0f]
            vp=vp[1:-1]
            (vendor_id,product_id)=vp.split(":")
            (dtype, human)=_identify_device_type("%s:%s"%(vendor_id,product_id))
            if usb_dm.can_redirect_device(usb_dev):
                add_to_redirect=True
                if "all" in self._usb_redir_classes:
                    pass
                elif dtype.value not in self._usb_redir_classes:
                    add_to_redirect=False

                #  remove mass storage device if it's where the live Linux is
                if add_to_redirect and dtype==DeviceType.MASS_STORAGE and root_dev:
                    # check that this device is not the one where a live Linux resides
                    (status, out, err)=util.exec_sync(["udevadm", "info", "-n", root_dev ,"-a"])
                    if status==0:
                        found=0
                        for line in out.splitlines():
                            if '=="%s"'%vendor_id in line and "{idVendor}" in line:
                                found+=1
                            elif '=="%s"'%product_id in line and "{idProduct}" in line:
                                found+=1
                            if found==2:
                                add_to_redirect=False
                                break
        except Exception:
            pass

        if add_to_redirect:
            dev=UsbDevice(usb_dev, human)
            self._devices[usb_dev]=dev
        else:
            self._devices[usb_dev]=None
        return self._devices[usb_dev]

    def _device_connect_result_cb(self, usb_dev, res, dev):
        """Called when the operation of connecting a device terminates""" 
        usb_dm=SpiceClientGLib.UsbDeviceManager.get(self._session)
        try:
            fres=usb_dm.connect_device_finish(res)
            dev.state=DeviceState.CONNECTED
            syslog.syslog(syslog.LOG_INFO, "Device connected: %s"%dev.descr)
        except Exception as e:
            dev.state=DeviceState.DISCONNECTED
            syslog.syslog(syslog.LOG_ERR, "Device not connected: %s / %s"%(dev.descr, str(e)))

    def _device_disconnect_result_cb(self, usb_dev, res, dev):
        """Called when the operation of disconnecting a device terminates""" 
        usb_dm=SpiceClientGLib.UsbDeviceManager.get(self._session)
        dev.state=DeviceState.DISCONNECTED
        try:
            fres=usb_dm.disconnect_device_finish(res)
            syslog.syslog(syslog.LOG_INFO, "Device no more connected: %s"%dev.descr)
        except Exception as e:
            syslog.syslog(syslog.LOG_ERR, "Device not disconnected: %s / %s"%(dev.descr, str(e)))

    def _button_devices_cb(self, button):
        """Called to show the connectable and already connected devices"""
        # force devices anlysis if not yet done
        if not self._devices_analyzed:
            for usb_dev in list(self._devices.keys()):
                self._analyze_usb_device(usb_dev)
            self._devices_analyzed=True

        # build widgets if necessary
        if not self._devices_popover:
            self._devices_popover=Gtk.Popover()
            self._devices_popover.set_relative_to(self._dev_button)
            
            grid=Gtk.Grid()
            grid.set_row_spacing(10)
            grid.set_column_spacing(10)
            grid.set_property("margin", 10)
            self._devices_grid=grid

            # label displayed when no device can be shared with the VM
            self._devices_none_label=Gtk.Label(label="N/A")
            self._devices_grid.attach(self._devices_none_label, 1, self._devices_grid_vindex, 2, 1)
            self._devices_none_label.show()
            self._devices_none_label.dev=None # dummy but used in _remove_device_entry()
            self._devices_none_label.connect("show", self._devices_none_label_keyb_button_show_cb)
            self._devices_grid_vindex+=1

            # add already found devices
            for usb_dev in self._devices:
                dev=self._devices[usb_dev]
                if dev:
                    self._add_device_entry(dev)
                    
            self._devices_popover.add(grid)
            self._devices_popover.show_all()
        else:
            self._devices_popover.show()

    def _devices_none_label_keyb_button_show_cb(self, widget):
        children=self._devices_grid.get_children()
        nbchildren=len(children)
        if nbchildren!=1:
            self._devices_none_label.hide()

    def _add_device_entry(self, dev):
        # Adding widgets associated to @dev
        cbox=Gtk.CheckButton()
        cbox.connect("toggled", self._device_toggled_cb, dev)
        if dev.state in (DeviceState.CONNECTING, DeviceState.CONNECTED):
            cbox.set_active(True)
        if dev.state in (DeviceState.CONNECTING, DeviceState.DISCONNECTING):
            cbox.set_sensitive(False)
        cbox.dev=dev
        dev.connect("state-changed", self._dev_state_changed_cb, cbox)

        self._devices_grid.attach(cbox, 0, self._devices_grid_vindex, 1, 1)
        label=Gtk.Label(label=dev.descr)
        label.dev=dev
        label.set_property("xalign", 0)
        self._devices_grid.attach(label, 1, self._devices_grid_vindex, 1, 1)
        self._devices_grid_vindex+=1

        cbox.show()
        label.show()
        self._devices_none_label.hide()

    def _remove_device_entry(self, dev):
        # Removing widgets associated to @dev
        children=self._devices_grid.get_children()
        nbchildren=len(children)-2
        for child in children:
            if child.dev==dev: # GtkLabel and GtkCheckButton both point to the same @dev
                self._devices_grid.remove(child)
        if nbchildren==1: # only the self._devices_none_label remains
            self._devices_none_label.show()

    def _device_toggled_cb(self, checkbox, dev):
        if checkbox.get_active():
            dev.state=DeviceState.CONNECTING
            usb_dm=SpiceClientGLib.UsbDeviceManager.get(self._session)
            usb_dm.connect_device_async(dev.usb_dev, None, self._device_connect_result_cb, dev)
        else:
            dev.state=DeviceState.DISCONNECTING
            usb_dm=SpiceClientGLib.UsbDeviceManager.get(self._session)
            usb_dm.disconnect_device_async(dev.usb_dev, None, self._device_disconnect_result_cb, dev)

    def _dev_state_changed_cb(self, dev, cbox):
        assert dev==cbox.dev
        if dev.state in (DeviceState.CONNECTING, DeviceState.CONNECTED):
            cbox.set_active(True)
        else:
            cbox.set_active(False)
        if dev.state in (DeviceState.CONNECTING, DeviceState.DISCONNECTING):
            cbox.set_sensitive(False)
        else:
            cbox.set_sensitive(True)

    #
    # misc. other features
    #
    def _button_fullscreen_cb(self, button):
        self.emit("fullscreen", button.get_active())

    def _button_close_cb(self, button):
        self.emit("close")

    def reveal(self):
        self._reveal.set_reveal_child(True)

    def unreveal(self):
        self._reveal.set_reveal_child(False)

class VMUI(Gtk.Overlay):
    """Actual viewer"""
    def __init__(self, config_data):
        # @config_data should contain the 'port', 'password' and 'usb-redir' keys
        if not isinstance(config_data, dict) or "port" not in config_data or \
            "password" not in config_data or "usb-redir" not in config_data:
            raise Exception("Invalid UI config data %s"%config_data)

        Gtk.Overlay.__init__(self)

        # widgets
        self._session=SpiceClientGLib.Session()
        self._session.set_property("uri", "spice://localhost?port=%s"%config_data["port"])
        self._session.set_property("password", config_data["password"])
        self._session.connect_after("channel-new", self._channel_new_cb)

        self._actions=VMActions(self)
        self._actions.usb_redir_classes=config_data["usb-redir"]
        self.add_overlay(self._actions)
        self.set_overlay_pass_through(self._actions, True)
        self._actions.show()

        # misc.
        self._input_channel=None
        self._display=None # will be a SpiceClientGtk.Display
        display=Gdk.Display.get_default()
        seat=display.get_default_seat()
        self._pointer=seat.get_pointer()
        self._gdkwin=None

    @property
    def actions(self):
        return self._actions

    @property
    def input_channel(self):
        return self._input_channel

    @property
    def spice_session(self):
        return self._session

    @property
    def spice_display(self):
        return self._display

    def _mouse_move_cb(self, window, event):
        if self._gdkwin is None:
            self._gdkwin=window.get_window()
        (dummy, x, y, mask)=self._gdkwin.get_device_position(self._pointer)
        r=False
        if y<10:
            win_w=self._gdkwin.get_width()
            mid=win_w/2
            act_w=self._actions.get_allocated_width()
            if x>=mid-act_w and x<=mid+act_w:
                self._actions.reveal()
                r=True
        if not r:
            self._actions.unreveal()
        self._display.set_property("keypress-delay", 0)
        return False

    def session_connect(self):
        self._session.connect()

    def session_disconnect(self):
        self._session.disconnect()

    def _channel_event(self, channel, event):
        syslog.syslog(syslog.LOG_INFO, "Spice main channel event %s"%event)
        if event not in (SpiceClientGLib.ChannelEvent.OPENED, SpiceClientGLib.ChannelEvent.CLOSED):
            print("Connection to VM failed")

    def _channel_new_cb(self, session, channel):
        ctype=channel.get_property("channel-type")
        syslog.syslog(syslog.LOG_INFO, "Spice new channel: %s, type: %s"%(channel, ctype))
        if ctype==1: # main channel
            channel.connect_after("channel-event", self._channel_event)
            #channel.set_property("mouse-mode", 1)
        elif ctype==2: # display channel
            cid=channel.get_property("channel-id")
            self._display=SpiceClientGtk.Display.new(self._session, cid)
            self._display.set_property("resize-guest", True)
            self._display.set_property("keypress-delay", 0)
            self.add(self._display)
            self._display.show()

            self._display.add_events(Gdk.EventMask.POINTER_MOTION_MASK)
            self._display.connect("motion-notify-event", self._mouse_move_cb)
        elif ctype==3: # input channel
            self._input_channel=channel


def get_sane_default_vmui_size():
    """Compute a default reasonable size for the VM'UI window"""
    display=Gdk.Display.get_default()
    w=20000
    h=20000
    for index in range(0, display.get_n_monitors()):
        mon=display.get_monitor(index)
        rect=mon.get_workarea()
        w=min(w, rect.width)
        h=min(h, rect.height)
    w=max(w-200, 1080)
    h=max(h-200, 824)
    return (w,h)