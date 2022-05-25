#!/bin/bash

# -*- coding: utf-8 -*-
#
# Copyright 2020 Vivien Malerba <vmalerba@gmail.com>
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

set -e
umask 0022

[ $# != 4 ] && {
    echo "$0 <distrib> <pkg name> <version> <source tarball>"
    exit 1
}
distrib="$1"
udistrib=${distrib^^} # uppercase
pkgname="$2"
version="$3"
tarball=$(realpath "$4")

echo "===== $udistrib ====="
echo "Tarball: $tarball"
echo "Version: $version"

scriptdir=$(realpath $(dirname "$0"))

# create package
echo "Building package..."

rm -f "$scriptdir/$udistrib"_*
outtmpdir=$(mktemp -d)
tmpstd=$(mktemp)
sudo docker run --rm -e "VERSION=$version" -e "NAME=$pkgname" \
     -e "UID=$(id -u)" -e "GID=$(id -g)" \
     -v "$tarball:/tarball.tar.gz:ro" -v "$outtmpdir:/out" "fairshell-$distrib-builder" > "$tmpstd" 2>&1 || {
    echo "Failed: "
    cat "$tmpstd"
    rm -f "$tmpstd"
    exit 1
}
rm -f "$tmpstd"

for file in "$outtmpdir/"*
do
    base=$(basename "$file")
    mv "$file" "$scriptdir/${udistrib}_$base"
    echo "Generated: $scriptdir/${udistrib}_$base"
done

exit 0
