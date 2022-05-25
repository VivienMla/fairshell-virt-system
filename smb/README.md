This Docker container implements a SMB server which allows sharing a directory with a
Windows VM.

The password of the 'smbshare' user must be specified when starting the container as the SMBPASS variable,
the mapped Linux user and group IDs must be passed using the UID and GID variables.

The SMB server will be executed as the UID and UIG passed as environment variables.
