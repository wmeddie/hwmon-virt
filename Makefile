# SPDX-License-Identifier: GPL-2.0-only
ifneq ($(KERNELRELEASE),)
obj-m := k10temp_virt.o
else
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
	install -Dm755 k10temp_virt.py $(DESTDIR)$(PREFIX)/libexec/k10temp-virt
endif
