This project enables one to run an OS in a controled virstualized environment, on a "normal" Linux desktop session.

The VM is "short lived" as it is created on the fly when needed and destroyed afterwards, always starting from the same (assumed
to be in a known and clean state). So, any changes made to the VM (being a normal usage or after a sucessful attack) are lost
when the VM is stopped.

The Documents/ folder of the VM is "mapped" to a directory of the Linux host using a dedicated SMB server.

Also, in order to improve security, the outgoing connections are allowed only to some IP ranges and/or named systems (i.e. a connection can only be opened after a sucessful DNS resolution). All the DNS resolutions of the VM are handled by a dedicated
DNS server.

# Source directories

- `desktop-app/`: UI application to start the VM
- `manager/`: systemd service to manage the VM, and various other tools
  to install or upgrade the VM
- `packaging/`: resources to create packages for the supported Linux distributions
- `smb/` and `unbound/`: resources to build the Docker images for the SMB and DNS servers
  used with the VM

# Supported Linux host platforms

- Debian
- Ubuntu
- Fedora

# Creating packages

The packages are built in the build environments created as Docker images in the `packagers/` sub project.

Use the `make` command:

- `make dist`: create a tarball of the sources (incl. the Docker images of the SMB and DNS servers)
- `make packages`: create all packages
- `make clean`: clean up everything

The global software version is specified via the `version` variable in the Makefile.
