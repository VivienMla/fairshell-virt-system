#!/bin/sh

[ "$SMBPASS" == "" ] && {
    echo "SMBPASS not defined"
    exit 1
}

[ "$UID" == "" ] && {
    echo "UID not defined"
    exit 1
}

[ "$GID" == "" ] && {
    echo "GID not defined"
    exit 1
}

set -e

[ "$UID" != "0" ] && {
    # remove user with UID if it exists and is not smbshare
    uname=$(cat /etc/passwd | grep "x:$UID:" | awk -F : '{print $1}')
    [ "$uname" != "" ] && [ "$uname" != "smbshare" ] && userdel "$uname"
    usermod -u "$UID" smbshare

    # remove group with GID if it exists and is not smbshare
    grname=$(cat /etc/group | grep ":$GID:" | awk -F : '{print $1}')
    [ "$grname" != "" ] && [ "$grname" != "smbshare" ] && groupmod -g 10000 "$grname"
    groupmod -g "$GID" smbshare
}

echo -e "$SMBPASS\n$SMBPASS" | smbpasswd -s -a smbshare

smbd -F
