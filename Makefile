# -*- coding: utf-8 -*-
#
# Copyright 2020 - 2022 Vivien Malerba <vmalerba@gmail.com>
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


# This Makefile is used for several purposes:
# - from FAIRSHELL's source tree to build a tarball of the program, target 'dist'
# - from FAIRSHELL's source tree to build packaging (RPM, Deb, ...), target 'packages'
# - from each package building environment to install the program, target 'install'


# version
version=2.0.7

help:
	@echo "Possible targets: dist, packages, clean"

# application's name and files
appname=virt-system
srcfiles=Makefile \
	LICENSE \
	manager/EventsHub.py \
	manager/fairshell-virt-system.json \
	manager/example-fairshell-virt-system.json \
	manager/fairshell-virt-system.service \
	manager/org.fairshell.VMManager.conf \
	manager/update-security-policy.py \
	manager/Utils.py \
	manager/vm-tool.py \
	manager/vm-manager.py \
	manager/VM.py \
	manager/NetworkIptables.py \
	manager/NetworkNftables.py \
	\
	desktop-app/icons/tux.png \
	desktop-app/icons/win10.png \
	desktop-app/icons/win11.png \
	desktop-app/example-VM.desktop \
	desktop-app/fairshell-VM.py \
	desktop-app/fairshell-VM.ui \
	desktop-app/fairshell-viewer.py \
	desktop-app/VMUI.py \
	\
	fairshell-smb.tar \
	fairshell-unbound.tar \
	image-ids.json \
	\
	packaging/debian/control \
	packaging/debian/changelog.Debian \
	packaging/debian/conffiles \
	packaging/debian/postinst \
	packaging/debian/prerm \
	packaging/ubuntu/control \
	packaging/ubuntu/changelog.Debian \
	packaging/ubuntu/conffiles \
	packaging/ubuntu/postinst \
	packaging/ubuntu/prerm \
	packaging/fedora/virt-system.spec

packaging/fedora/virt-system.spec: packaging/fedora/virt-system.spec.in
	@echo "Generating $@"
	@cat $< | sed -e "s/@appname@/$(appname)/" -e "s/@version@/$(version)/" > $@
packaging/debian/control: packaging/debian/control.in
	@echo "Generating $@"
	@cat $< | sed -e "s/@appname@/$(appname)/" -e "s/@version@/$(version)/" > $@
packaging/ubuntu/control: packaging/ubuntu/control.in
	@echo "Generating $@"
	@cat $< | sed -e "s/@appname@/$(appname)/" -e "s/@version@/$(version)/" > $@

arname=fairshell-$(appname)-$(version)
tarname=$(arname).tar
distfile=$(tarname).gz

dist: $(distfile)

# create the TAR file
$(tarname): $(srcfiles)
	@echo "Creating $@"
	@tar cf "$@" --transform 's/^/$(arname)\//' $(srcfiles)

# create a compressed TAR file
$(distfile): $(tarname)
	@echo "Creating $@"
	@rm -f $@
	@gzip -9 $<

# build packages for Debian, Ubuntu, Fedora, ...
packages: $(distfile)
	@for distrib in debian ubuntu fedora; do ./packaging/build-package.sh "$$distrib" "$(appname)" "$(version)" "$(distfile)"; done

# clean the source tree
clean:
	rm -f *.tar *.tar.gz image-ids.json
	rm -f packaging/fedora/virt-system.spec packaging/debian/control
	rm -f packaging/*.rpm packaging/*.deb

# build Docker images
fairshell-smb.tar:
	$(MAKE) -C smb

fairshell-unbound.tar:
	$(MAKE) -C unbound

# create the versions.json file which contains Docker image IDs
image-ids.json: fairshell-smb.tar fairshell-unbound.tar
	$(eval SMBID := $(shell sudo docker images --format "{{.ID}}" fairshell-smb))
	$(eval UNBOUNDID := $(shell sudo docker images --format "{{.ID}}" fairshell-unbound))
	@echo "{\"fairshell-smb\": \"$(SMBID)\", \"fairshell-unbound\": \"$(UNBOUNDID)\"}" > $@


# install files from source tree to $DESTDIR
# this target is called by the packages builders (rpmbuild, dpkg-deb, ...)
install:
	# directories
	for dir in "etc" "lib/systemd/system" "etc/dbus-1/system.d" "usr/share/fairshell/$(appname)" "usr/share/fairshell/$(appname)/icons" "usr/share/fairshell/$(appname)/example-conf" "usr/share/fairshell/$(appname)/docker-images" "usr/share/applications"; do $(INSTALL) -d -m 0755 "$(DESTDIR)/$$dir"; done

	# manager files
	for file in EventsHub.py VM.py NetworkIptables.py NetworkNftables.py Utils.py; do $(INSTALL) -m 0644 "manager/$$file" "$(DESTDIR)/usr/share/fairshell/$(appname)"; done
	for file in vm-tool.py vm-manager.py update-security-policy.py; do $(INSTALL) -m 0755 "manager/$$file" "$(DESTDIR)/usr/share/fairshell/$(appname)"; done
	$(INSTALL) -m 0644 "manager/fairshell-$(appname).service" "$(DESTDIR)/lib/systemd/system"
	$(INSTALL) -m 0644 "manager/org.fairshell.VMManager.conf" "$(DESTDIR)/etc/dbus-1/system.d"
	$(INSTALL) -m 0644 "manager/fairshell-virt-system.json" "$(DESTDIR)/etc"

	# Docker images
	$(INSTALL) -m 0600 fairshell-smb.tar fairshell-unbound.tar "$(DESTDIR)/usr/share/fairshell/$(appname)/docker-images"
	$(INSTALL) -m 0600 image-ids.json "$(DESTDIR)/usr/share/fairshell/$(appname)/docker-images"

	# desktop application
	for file in fairshell-VM.ui VMUI.py; do $(INSTALL) -m 0644 desktop-app/$$file "$(DESTDIR)/usr/share/fairshell/$(appname)"; done
	for file in icons/*; do $(INSTALL) -m 0644 desktop-app/$$file "$(DESTDIR)/usr/share/fairshell/$(appname)/icons"; done
	$(INSTALL) -m 0755 "desktop-app/fairshell-VM.py" "$(DESTDIR)/usr/share/fairshell/$(appname)"
	$(INSTALL) -m 0755 "desktop-app/fairshell-viewer.py" "$(DESTDIR)/usr/share/fairshell/$(appname)"

	# example configurations
	$(INSTALL) -m 0644 "desktop-app/example-VM.desktop" "$(DESTDIR)/usr/share/fairshell/$(appname)/example-conf/VM.desktop"
	$(INSTALL) -m 0644 "manager/example-fairshell-$(appname).json" "$(DESTDIR)/usr/share/fairshell/$(appname)/example-conf/fairshell-$(appname).json"

