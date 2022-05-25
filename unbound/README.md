This Docker container acts as a small DNS server to allow resolution for some
of the zones only.

It filters the zones to be resolved and forwards them to the DNS resolver
of the host. Non allowed zones return with a DNS failure resolution.

The actual DNS server(s) used by this DNS server is defined in a JSON file
mapped to `/etc/resolv.json`. This file is being monitored for changes so the
host OS can update the actual DNS server(s) used while the container is running.