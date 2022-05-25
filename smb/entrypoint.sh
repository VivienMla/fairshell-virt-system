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
    usermod -u "$UID" smbshare
    groupmod -g "$GID" smbshare
}

echo -e "$SMBPASS\n$SMBPASS" | smbpasswd -s -a smbshare

smbd -F
