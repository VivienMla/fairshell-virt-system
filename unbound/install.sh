#!/bin/sh

set -e

# soft. install
apt update
apt install -y unbound python3-unbound python3 python3-pyinotify python3-distutils # python3-distutils is required by unbound
#apt install -y iputils-ping iproute2 dnsutils procps vim
mkdir -p /etc/unbound/unbound.conf.d
mv /unbound.conf /etc/unbound/

set +e
unbound-anchor -a /var/lib/unbound/root.key -v
rc=$?
[ $rc != 0 ] && [ $rc != 1 ] && {
    # man page says exit code 0 or 1 are Ok
    echo "unbound-anchor update failed"
    exit 1
}
set -e

ln -s /usr/lib/python3/dist-packages/unboundmodule.py /etc/unbound/unboundmodule.py

# remove itself
rm -f "$0"
