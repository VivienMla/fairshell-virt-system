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
# This module allows one to integrate asynchronous components such as DBus services, GLib idle functions and
# Pyinotify monitoring.

import time
import uuid
import syslog
import threading
import psutil
import pyinotify

from gi.repository import GLib

import dbus
import dbus.service
import dbus.mainloop.glib

class Cancelled(Exception):
    pass

class Hub:
    def __init__(self):
        self._components=[]
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.loop=GLib.MainLoop()

    def register(self, component):
        """Register a component which events must be handled"""
        assert isinstance(component, Component)
        if component not in self._components:
            self._components+=[component]
            component._registered(self)

    def unregister(self, component):
        """Unregister a component"""
        assert isinstance(component, Component)
        if component in self._components:
            self._components.remove(component)
            component._unregistered()

    def run(self):
        """Run all the events of the components (starts a GLib main loop)"""
        if len(self._components)==0:
            raise Exception("No component to be run")

        # run GLib's main loop
        self.loop.run()

class Component():
    def __init__(self):
        self._async_threads={} # indexed by thread ID
        self._loop=None

        # sub thread execution
        self._sync_thread=None
        self._result=None
        self._exception=None

    def _registered(self, hub):
        # we need a reference to the hub'd main loop
        self._loop=hub.loop

    def _unregistered(self):
        # we need a reference to the hub'd main loop
        self._loop=None

    #
    # Synchronous long running operations: the main loop is executed while the sub thread being run
    # to execute the requested function does execute. The drawbacks are that:
    # - if the context is a DBus method call, then no other method call will be accepted while in this function
    # - otherwise, if several such functions are executed, then they will terminate in a LIFO way
    #
    def _sub_sync_thread(self, func, args):
        # function actually called in a sub thread
        self._result=None
        self._exception=None
        try:
            self._result=func(args)
        except Exception as e:
            self._exception=e

    def job_run_wait(self, func, args):
        """Run a function (job) in a sub thread and waits for its completion, while at the same time still handling
        events. For long running jobs, use the job_run() and associated method.
        Note however that:
        * each component can only run one such synchronous thread
        * for DBus server objects, this function will block all other DBus calling methods
        """
        if self._sync_thread:
            raise Exception("Sub thead already in use")
        self._sync_thread=threading.Thread(target=self._sub_sync_thread, args=(func, args))
        self._sync_thread.start()
        context=self._loop.get_context()

        while True:
            while context.pending():
                context.iteration(False)
            time.sleep(0.1)
            if not self._sync_thread.is_alive():
                self._sync_thread.join()
                self._sync_thread=None
                break

        if self._exception:
            raise self._exception
        return self._result

    #
    # Asynchronous long running operations: a runner thread is executed and a "thread ID" (random UUID)
    # is returned. Use job_get_status() to check if the execution has finished.
    #
    def _sub_async_thread(self, func, args, job_id):
        # function actually called in a sub thread
        assert threading.current_thread()!=threading.main_thread()
        tdata=self._async_threads[job_id]
        try:
            result=func(lambda: tdata["cancel"], args)
            tdata["result"]=result
        except Exception as e:
            tdata["exception"]=e
        finally:
            tdata["finished"]=True

    def _check_job_finished(self):
        assert threading.current_thread()==threading.main_thread()
        copy=list(self._async_threads.keys())
        for job_id in copy:
            try:
                tdata=self._async_threads[job_id]
                callback_func=tdata["callback"]
                if callback_func and self.job_is_finished(job_id):
                    callback_func(job_id)
                    tdata["callback"]=None # so the callback is not executed several times
            except Exception as e:
                err="WARN: while checking jobs' status: %s"%str(e)
                print(err)
                syslog.syslog(syslog.LOG_WARNING, err)

        if len(self._async_threads)==0:
            return False # don't keep timer
        else:
            return True # keep timer

    def job_run(self, func, args, callback_func=None):
        """Run a function (job) in a sub thread, and returns a job ID.
        The @func is a function accepting the following arguments:
        - a @cancel_request function argument which should be executed each time the thread could be safely stopped to check
          if a cancel request has been made. What the job does on such event is up to the implementation to define (raise an
          exception etc.)
        - the arguments specified in @args
        the @func function is called from a sub thread.

        Call job_get_status() to check the status of the job

        This function _must_ be called from the main thread.
        @callback_func can be a function called when the jobs is finished (even if it failed), it takes a single job_id argument,
        it will also be called from the main thread."""
        assert threading.current_thread()==threading.main_thread()
        job_id=str(uuid.uuid4())
        tdata={"thread": None,    # None when not started of already joined()
               "finished": False, # True when thread has finished
               "result": None,    # thread's actual result, if any
               "exception": None, # thread's raised exception, if any
               "callback": callback_func,  # callback function when the job is done, if any
               "cancel": False}   # True when a cancel request has been made
        self._async_threads[job_id]=tdata
        thread=threading.Thread(target=self._sub_async_thread, args=(func, args, job_id))
        tdata["thread"]=thread
        thread.start()

        # start timer if not yet started
        if len(self._async_threads)==1:
            GLib.timeout_add(500, self._check_job_finished)

        return job_id

    def job_cancel(self, job_id):
        """Cancels a running job (the actual job's function may or not ignore that request)"""
        if job_id not in self._async_threads:
            raise Exception("Unknown thread ID %s"%job_id)
        tdata=self._async_threads[job_id]
        tdata["cancel"]=True

    def job_is_finished(self, job_id):
        """Tell if a job started using job_run() has finished"""
        if job_id not in self._async_threads:
            raise Exception("Unknown job ID %s"%job_id)
        tdata=self._async_threads[job_id]
        return tdata["finished"]

    def job_get_result(self, job_id):
        """Get the result of a thread started using job_run(), ONCE IT HAS FINISHED.
        Is the job raised an exception, that exception is raised here.
        If @keep is False, then any trace of the jobs is removed.
        """
        if job_id not in self._async_threads:
            raise Exception("Unknown job ID %s"%job_id)
        tdata=self._async_threads[job_id]

        if tdata["finished"]:
            if tdata["thread"]:
                tdata["thread"].join() # to avoid stale threads
                tdata["thread"]=None
            res=tdata["result"]
            exp=tdata["exception"]
            try:
                if exp:
                    raise exp
                else:
                    return res
            finally:
                # remove any trace of that thread's execution
                del self._async_threads[job_id]
        else:
            raise Exception("Job is not yet finished")

class InotifyComponent(Component):
    """Component to monitor a set of directories or files.
    You need to:
    * implement the inotify_handler() method which will be called whenever an event is triggered
    * call add_watch() to monitor directories / files
    """
    def __init__(self):
        Component.__init__(self)
        self._wm=pyinotify.WatchManager()
        self._notifier=pyinotify.Notifier(self._wm, default_proc_fun=self.inotify_handler)
        GLib.io_add_watch(self._notifier._fd, GLib.IO_IN, self._inotify_process_events, self._notifier)

    def _inotify_process_events(self, source, condition, notifier):
        #print("InoyifyComponent::_inotify_process_events()")
        notifier.read_events()
        notifier.process_events()
        while notifier.check_events(10): # don't lock while waiting
            notifier.read_events()
            notifier.process_events()
        #print("InoyifyComponent:: done")
        return True

    def add_watch(self, path, mask, rec=False, auto_add=False, do_glob=False, quiet=True, exclude_filter=None):
        return self._wm.add_watch(path, mask, rec, auto_add, do_glob, quiet, exclude_filter)

    def del_watch(self, watch):
        for path in watch:
            self._wm.del_watch(watch[path])

    def inotify_handler(self, event):
        raise Exception("inotify_handler() is a pure virtual function")

class DBusServer(Component, dbus.service.Object):
    """Implements a DBus server offering a single object.
    * @system_bus must be True for the system bus, or False for the session bus
    * @service_name must be the name of the service on that bus, like "org.fairshell.VMManager"
    * @object_path: path of the object

    You need to implement the methods and signal using the appropriate decorators.
    """
    def __init__(self, system_bus, service_name, object_path):
        Component.__init__(self)
        self._loop=None
        if system_bus:
            self._bus=dbus.SystemBus()
        else:
            self._bus=dbus.SessionBus()

        # register service name on the bus
        name=dbus.service.BusName(service_name, self._bus)

        # register service
        dbus.service.Object.__init__(self, name, object_path)

    def get_user_ident(self, sender, bus):
        """Get the UID and GID of the user having sent a command, using the @sender information"""
        dbus_info=dbus.Interface(bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus/Bus", False),
                                 "org.freedesktop.DBus")
        pid=dbus_info.GetConnectionUnixProcessID(sender) # also: GetConnectionUnixUser()
        po=psutil.Process(pid)
        return (po.uids().real, po.gids().real)
