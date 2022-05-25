"""Some utility functions"""
# -*- coding: utf-8 -*-
#
# Copyright 2018 - 2022 Vivien Malerba <vmalerba@gmail.com>
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
import re
import subprocess
import tempfile
import sys
import pwd
import shutil
import time
import distro
import shlex
import psutil
import json
import xdg.DesktopEntry

def get_distrib():
    """Get the linux distribution, e.g. "fedora", "debian" or "ubuntu". """
    pf=distro.linux_distribution(full_distribution_name=False)
    return pf[0].lower()

def write_data_to_file(data, filename, append=False, mode=None):
    """Creates a file with the specified data and filename"""
    wmode="wb"
    if append:
        wmode="ab"

    if mode:
        original_umask=os.umask(0)
        opt=0
        if append:
            opt=os.O_APPEND
        fd=os.open(filename, os.O_CREAT | os.O_RDWR | opt, mode)
        os.umask(original_umask)
        file=os.fdopen(fd, wmode)
        stat=os.stat(filename)
        if stat.st_mode & 0o777 !=mode:
            raise Exception("Invalid permissions for '%s': expected %s and got %s"%(filename, oct(mode), oct(stat.st_wmode & 0o777)))
    else:
        file=open(filename, wmode)

    rdata=data
    if isinstance(rdata, str):
        rdata=data.encode()
    if rdata is not None:
        file.write(rdata)
    file.close()

def load_file_contents(filename, binary=False):
    """Load the contents of a file in memory, as a string if @binary is False,
    or a bytearray if @binary is True"""
    with open(filename, "rb") as file:
        if binary:
            return file.read()
        else:
            return file.read().decode()

def exec_sync(args, stdin_data=None, as_bytes=False, exec_env=None, cwd=None, C_locale=False, timeout=None):
    """Run a command and wait for it to terminate, returns (exit code, stdout, stderr)
    Notes:
    - @stdin_data allows to specify some input data, while @as_bytes specifies if the output data
      need to be converted to a string (when False), or left as a bytes array (True), or passed to stdout
      if None.
    - if @C_locale is True, then the LANG environment variable is set to "C" (useful when parsing output which
      repends on the locale)
    - if @timeout is specified, then the sub process is killed after that number of seconds and the return code is 250
    """
    if C_locale:
        if exec_env:
            raise Exception("The @exec_env and @C_locale can't be both specified")
        exec_env=os.environ.copy()
        exec_env["LANG"]="C.UTF-8"
        exec_env["LC_ALL"]="C.UTF-8"

    # start process
    if as_bytes is None:
        outs=sys.stdout
        errs=sys.stderr
    else:
        outs=subprocess.PIPE
        errs=subprocess.PIPE
    if stdin_data is None:
        bdata=None
        sub=subprocess.Popen(args, stdout=outs, stderr=errs, env=exec_env, cwd=cwd)
    else:
        bdata=stdin_data
        if isinstance(bdata, str):
            bdata=bdata.encode()
        sub = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=outs, stderr=errs, env=exec_env, cwd=cwd)

    # let process run
    try:
        (out, err)=sub.communicate(input=bdata, timeout=timeout)
        retcode=sub.returncode
    except subprocess.TimeoutExpired:
        sub.kill()
        (out, err)=sub.communicate(timeout=timeout)
        retcode=250

    # prepare returned values
    sout=None
    serr=None
    if as_bytes:
        sout=out
        serr=err
    else:
        sout=re.sub (r'[\r\n]+$', '', out.decode()) if out else ""
        serr=re.sub (r'[\r\n]+$', '', err.decode()) if err else ""

    return (retcode, sout, serr)

def is_run_as_root():
    """Tell if the application is run as root or not"""
    return True if os.getuid()==0 else False

def generate_password(length=25, alphabet=None):
    """Generate a random password containing letters and numbers, of the specified length (which can't be less than 12 characters)."""
    # https://www.pleacher.com/mp/mlessons/algebra/entropy.html
    if length<12:
        raise Exception("Can't generate a password with specified %d length"%length)
    if not alphabet:
        import string
        alphabet = string.ascii_letters + string.digits
    elif len(alphabet)<26:
        raise Exception("Alphabet to generate password from is too small: %d long"%len(alphabet))
    if sys.version_info >= (3,6):
        # use the new Pyhton 3.6 secrets module
        import secrets
        return ''.join(secrets.choice(alphabet) for i in range(length))
    else:
        # fallbask to the random module
        import random
        return ''.join(random.choice(alphabet) for i in range(length))

def get_logged_user_id():
    """Returns the (uid, gid) of the logged user. If no user is logged, or more than one user is logged
    then an exception is raised"""
    # find the ID of the logged user, or the UID of the user running sudo
    uid=None

    if is_run_as_root():
        if "SUDO_UID" in os.environ:
            uid=int(os.environ["SUDO_UID"])

    if uid is None:
        for filename in os.listdir("/run/user"):
            luid=None
            try:
                luid=int(filename)
                if luid<1 or luid >2**32 or luid<1000: # invalid or service accounts
                    luid=None
            except Exception:
                pass

            if luid is not None:
                if uid is None:
                    uid=luid
                else:
                    raise Exception("More than one user is logged")
    if uid is None:
        raise Exception("No user logged")

    # get the primary GID of the user and start the job
    entry=pwd.getpwuid(uid)
    gid=entry.pw_gid

    return (uid, gid)

def run_viewer(conf_id):
    """Starts the remote viewer and returns the subprocess.Popen of the remote viewer
    """
    session_uid=None
    if os.getuid()==0:
        # running as root, need to run as 'real' user if sudo or pkexec were used
        if "SUDO_UID" in os.environ:
            session_uid=int(os.environ["SUDO_UID"])
        elif "PKEXEC_UID" in os.environ:
            session_uid=int(os.environ["PKEXEC_UID"])

    cenv=os.environ.copy()
    if session_uid is not None:
        cenv["XDG_RUNTIME_DIR"]="/run/user/%d"%session_uid
        cenv["DBUS_SESSION_BUS_ADDRESS"]="unix:path=/run/user/%d/bus"%session_uid
        cenv["WAYLAND_DISPLAY"]="wayland-0"
        cenv["DISPLAY"]=":0"
        cenv["XAUTHORITY"]="/run/user/%d/gdm/Xauthority"%session_uid

    if "VIEWER_CONSOLE_SIZE" in os.environ:
        args=["%s/fairshell-viewer.py"%os.path.dirname(__file__), "--console-size", conf_id]
    else:
        args=["%s/fairshell-viewer.py"%os.path.dirname(__file__), conf_id]
    proc=subprocess.Popen(args, env=cenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    if proc.poll()!=None:
        raise Exception("Could not start viewer (%s)"%" ".join(args))
    return proc

def check_libvirt_readable(path):
    testprog="""
import sys
try:
    print("Testing %s"%sys.argv[1])
    f=open(sys.argv[1], "r")
    f.close()
    exit(0)
except Exception as e:
    print("%s"%str(e))
    exit(1)
"""
    tmptester=tempfile.NamedTemporaryFile()
    tmptester.write(testprog.encode())
    tmptester.flush()
    shutil.chown(tmptester.name,  "libvirt-qemu")
    args=["sudo", "-u", "libvirt-qemu", "/usr/bin/python3", tmptester.name, path]
    (status, out, err)=exec_sync(args)
    if status==0:
        return True
    else:
        return False

def is_iso(path):
    lpath=path.lower()
    if lpath.endswith(".iso") or lpath.endswith(".img"):
        return True
    else:
        return False

def get_iso_images_list(files_list, resources_dir=None):
    """Ensures all resources are available:
    - all ISO files are readable by the libvirt-qemu user
    - regular files are packed in an ephemeral ISO
    - paths are all resolved

    Returns a list with:
    - the list of ISO image files, or [] if none
    - a tempfile.NamedTemporaryFile object which will be released when not used anymore, or None
    """
    # check availability of the files
    efiles=[]
    for path in files_list:
        if path[0]!="/":
            if resources_dir and resources_dir[0]=="/":
                path="%s/%s"%(resources_dir, path)
            else:
                raise Exception("No valid resources directory provided")
            path=os.path.realpath(path)
        if not check_libvirt_readable(path):
            raise Exception("Resource '%s' can't be read by Libvirt daemon"%path)
        efiles+=[path]

    # prepare list of ISO files and of the non ISO files which will be groupped in a tmp ISO file
    isofiles=[]
    extra_iso_contents=[] # files to include in one extra ISO image
    for path in efiles:
        if is_iso(path):
            isofiles+=[path]
        else:
            extra_iso_contents+=[path]

    tmpiso=None
    if len(extra_iso_contents)>0:
        tmpiso=tempfile.NamedTemporaryFile()
        args=["genisoimage", "-iso-level", "4", "-o", tmpiso.name]
        for path in extra_iso_contents:
            args+=[path]
        (status, out, err)=exec_sync(args)
        if status!=0:
            raise Exception("Could not create ISO image: %s"%err)
        isofiles+=[tmpiso.name]

    return (isofiles, tmpiso)

def get_root_live_partition(exception_if_no_live=True):
    """Get the live partition from which the system has booted.
    Returns devfile, for ex.: /dev/vda3"""
    # get the overlay's 'lower dir'
    (status, out, err)=exec_sync(["mount"])
    if status!=0:
        raise Exception("Could not list mount points: %s"%err)
    mounts=out
    ovline=None
    for line in mounts.splitlines():
        if line.startswith("overlay on / type overlay "):
            #  line will be like: overlay on / type overlay (rw,noatime,lowerdir=/run/live/rootfs/filesystem.squashfs/,upperdir=/run/live/overlay/rw,workdir=/run/live/overlay/work)
            ovline=line
            break
    if ovline is None:
        if exception_if_no_live:
            raise Exception("Could not identify the overlay filesystem")
        return None

    parts=re.split(r'\(|\)', ovline)
    params=re.split(r',', parts[1])
    found=False
    for param in params:
        if param.startswith("lowerdir="):
            (dummy, lowerdir)=param.split("=")
            # dir will be something like "/run/live/rootfs/filesystem.squashfs/"
            found=True
            break
    if not found:
        raise Exception("Could not identify overlay's lower dir")

    # get the loop device associated with the overlay's lower dir
    loopdev=None
    if lowerdir[-1]=="/":
        lowerdir=lowerdir[:-1]
    for line in mounts.splitlines():
        if "on %s type squashfs"%lowerdir in line:
            (loopdev, dummy)=line.split(" ", 1)
            break
    if loopdev!="/dev/loop0": # at this point, should always be loop0, otherwise something is very wrong...
        raise Exception("Unexpected loop device '%s'"%loopdev)

    # get the file serving as backend for the loopdev
    (status, out, err)=exec_sync(["/sbin/losetup", "-l", "-J", loopdev]) # as JSON!
    if status!=0:
        raise Exception("Could not list loop devices set up: %s"%err)
    data=json.loads(out)
    backend=data["loopdevices"][0]["back-file"]

    # get the mounted device partition holding that backend file
    (status, out, err)=exec_sync(["df", os.path.dirname(backend)]) # use the dirname and not the file itself for access permissions issues
    if status!=0:
        raise Exception("Could not use df: %s"%err)
    first=True
    for line in out.splitlines():
        if first:
            first=False
        else:
            (devfile, dummy)=line.split(" ", 1)
            # devfile will be like "/dev/vda3"
            if not devfile.startswith("/dev/vd") and not devfile.startswith("/dev/sd"):
                raise Exception("Invalid boot partition '%s'"%devfile)
            return devfile
    raise Exception("Internal error: boot partition is not mounted, where is the '%s' file ???"%backend)