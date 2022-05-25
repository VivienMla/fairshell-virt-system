#!/bin/bash

conf_file="conf.d/ads.conf"
rm -f "$conf_file"

echo "server:" > "$conf_file"
curl -s https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts | \
        grep ^0.0.0.0 - | \
        sed 's/ #.*$//;
        s/^0.0.0.0 \(.*\)/  local-zone: "\1" refuse/' \
        >> "$conf_file"
