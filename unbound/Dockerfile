FROM debian:bullseye-slim

LABEL maintainer="vmalerba@gmail.com"

COPY install.sh unbound.conf master-module.py resolv-manager.py /
RUN /install.sh
COPY conf.d/* /etc/unbound/unbound.conf.d/

ENTRYPOINT ["/resolv-manager.py"]
