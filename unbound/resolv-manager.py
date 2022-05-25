#!/usr/bin/python3

import os
import json
import subprocess
import pyinotify

class ListenEventHandler(pyinotify.ProcessEvent):
    resolv_json="/etc/resolv.json"
    zones_json="/etc/forward-zones.json"
    confdir="/etc/unbound/unbound.conf.d"
    def __init__(self):
        if "SMBSERVERIP" not in os.environ:
            raise Exception("Environment variable SMBSERVERIP is not defined")
        pyinotify.ProcessEvent.__init__(self)
        denyfile="%s/deny.conf"%self.confdir
        self._process=None
        self._current_ns=None
        self._resolv_changed()

    def reconfigure_server(self):
        # recreate forward.conf file
        conf_filename="%s/forward.conf"%self.confdir
        if self._current_ns:
            config="""server:
  forward-zone:
    name: "."
    forward-addr: %s"""%self._current_ns

            # write file
            f=open(conf_filename, "w")
            f.write(config)
            f.close()

        conf_filename="%s/smb.conf"%self.confdir
        if self._current_ns:
            config="""server:
  local-data: "smb.local. IN A %s"
            """%os.environ["SMBSERVERIP"]

            # write file
            f=open(conf_filename, "w")
            f.write(config)
            f.close()
        else:
            # no name server configured
            if os.path.exists(conf_filename):
                os.remove(conf_filename)

        # (re)start service
        if self._process:
            self._process.kill()
            self._process=None
        self._process=subprocess.Popen(["/usr/sbin/unbound", "-d", "-p"])

    def _resolv_changed(self):
        ns_list=json.loads(open(self.resolv_json).read())
        if len(ns_list)>0:
            ns=ns_list[0]
            if ns!=self._current_ns:
                self._current_ns=ns
                self.reconfigure_server()
        elif self._current_ns:
            self._current_ns=None
            self.reconfigure_server()

    def process_IN_MOVED_TO(self, event):
        print("MOVE_TO event for %s"% event.pathname)
        if event.pathname==self.resolv_json:
            self._resolv_changed()
        elif event.pathname==self.zones_json:
            self.reconfigure_server()

    def process_IN_CLOSE_WRITE(self, event):
        print("CLOSE_WRITE event for %s"% event.pathname)
        if event.pathname==self.resolv_json:
            self._resolv_changed()
        elif event.pathname==self.zones_json:
            self.reconfigure_server()

wm=pyinotify.WatchManager()
handler=ListenEventHandler()
notifier=pyinotify.Notifier(wm, handler)
wdd=wm.add_watch(handler.resolv_json, pyinotify.IN_CLOSE_WRITE)
wdd=wm.add_watch(handler.zones_json, pyinotify.IN_CLOSE_WRITE)
wdd=wm.add_watch("/etc", pyinotify.IN_MOVED_TO)
handler.reconfigure_server()

# overwrite /etc/resolv.conf
open("/etc/resolv.conf", "w").write("\n")

notifier.loop()
