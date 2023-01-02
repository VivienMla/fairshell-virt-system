#!/bin/sh

set -e

# soft. install
apk update && apk add shadow samba

# Add user context files
groupadd -g 1000 smbshare
useradd -ms /bin/sh -u 1000 -g 1000 smbshare

# remove itself
rm -f "$0"
