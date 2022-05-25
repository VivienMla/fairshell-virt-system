FROM alpine

COPY entrypoint.sh install.sh /
RUN /install.sh

# NB: Samba password file is in /var/lib/samba/private/
COPY smb.conf /etc/samba/

ENTRYPOINT ["/entrypoint.sh"]
