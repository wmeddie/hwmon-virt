# SPDX-License-Identifier: GPL-2.0-only
# Keep wrapper targets available when DKMS supplies KERNELRELEASE; see Kbuild.
KDIR ?= /lib/modules/$(shell uname -r)/build
PYTHON ?= python3
PREFIX ?= /usr/local
DESTDIR ?=

.PHONY: all modules clean test install-tools
all: modules
modules:
	$(MAKE) -C $(KDIR) M=$(CURDIR) W=1 modules
clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
test:
	$(PYTHON) -m unittest discover -s tests -v
install-tools:
	install -Dm755 hwmon_virt.py $(DESTDIR)$(PREFIX)/libexec/hwmon-virt
