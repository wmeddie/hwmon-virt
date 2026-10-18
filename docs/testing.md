# Building and testing

Run these commands from the project root.

In the guest, with a compiler, make, and matching kernel headers installed:

```sh
make
make test
```

`make` builds `k10temp_virt.ko` with extra compiler warnings. `make test` runs
unprivileged transport/source, installer, and test-runner checks. It also compiles
and exercises the module's completion-result helper to cover replies accepted
at the timeout boundary and signal interruptions deterministically; this checks
the decision logic, not a forced kernel scheduling interleaving. Actual-module
tests are skipped outside the disposable VM. To target another installed kernel:

```sh
make KDIR=/lib/modules/KERNEL_VERSION/build
```

To exercise the actual DKMS build path (including its injected `KERNELRELEASE`),
run this **without sudo**, with DKMS, matching headers, a compiler, make, and
modinfo installed:

```sh
K10TEMP_VIRT_DKMS_TESTS=1 python3 -m unittest discover -s tests -p test_dkms.py -v
```

It builds the exact source set staged by the guest installer in temporary DKMS
directories and checks the resulting module. It does not install or load the
module. Signing is omitted in this isolated build test; the actual installation
uses the distribution's normal DKMS signing configuration. `Kbuild` contains the
kernel build rules so DKMS's `KERNELRELEASE` cannot hide the outer Makefile targets.

For real kernel tests without loading this module into the running machine, use
an x86-64 kernel image matching the headers used for the module build:

```sh
python3 tests/run_vm.py /path/to/readable/matching/vmlinuz
```

The test runner requires QEMU, a **statically linked BusyBox**, cpio, lm-sensors,
and Python. It boots an ephemeral initramfs with TCG, uses no VM disks or network,
and removes its temporary files on exit. It tests module loading/unloading,
cache hits/expiry, simultaneous readers, channel isolation, invalid replies,
error-code filtering, invalid device names, timeouts, and bridge disconnects.
It also runs both services over a real QEMU virtio-serial channel, stops/restarts
the fixture host responder to verify prompt failures, warning suppression, and
automatic recovery, and checks that unmodified `sensors -j` discovers the device
and reports the fixture values. The `K10TEMP_VIRT_KERNEL_TESTS` and
`K10TEMP_VIRT_TRANSPORT_TESTS` flags are for the disposable VM only; never enable
them for the ordinary test suite on a production machine.

## Local validation

The userspace suite passed on Python 3.9.24, 3.10.19, 3.11.14, 3.12.12, 3.13.9,
and 3.14.4. Module builds and isolated DKMS builds passed against
`6.17.0-23-generic` and `7.0.0-38-generic` headers. Runtime kernel and transport
tests passed in QEMU 10.2.1 with Linux `7.0.0-38-generic` and Python 3.14.4.

52 distinct tests passed across the local, isolated DKMS, and disposable VM runs:
16 transport/source tests, 19 installer fixtures, 1 runner check, 1 C completion
check, 11 actual-module tests, 3 full virtio-serial/lm-sensors tests, and 1 DKMS
build test. The longest disconnected read in the VM run was 2.4 ms, below the
300 ms test limit and the former 500 ms timeout. The kernel sources also passed
`checkpatch` without warnings. Other kernel versions have not been locally tested.

Installer fixtures cover configuration preservation, repeat runs, dry runs,
failures, channel conflicts, and release-version consistency. The isolated build
passed through DKMS 3.2.2 using the installer source set.

A live installation and module upgrade have also been reported working with
Proxmox VE 9.2.3 and an Ubuntu 26.04.1 guest on Linux `7.0.0-38-generic`. Resources stayed
open during that upgrade without an observed crash or interruption requiring an
application restart. This is an observation from that setup, not a guarantee
about every monitor's sensor-discovery behavior.

## Continuous integration

The GitHub Actions workflow runs local tests on Python 3.9 through 3.14 and
separate kernel/DKMS/QEMU jobs on Ubuntu 22.04 and 24.04. Each kernel job installs
a matching generic image and headers into its disposable runner and uses the
system Python to build the test initramfs. It never loads the module into the
runner's own kernel. The workflow is configured for publication; its first
hosted results will be available after the repository is created.

The automated target matrix is not a claim that every combination is already
validated. Consult the test results for the release you use. Other architectures,
Secure Boot enrollment, and Proxmox live migration are outside the automated tests.

To test DKMS against an installed kernel other than the running one, set
`K10TEMP_VIRT_TEST_KERNEL` to that kernel's release while enabling the DKMS test.
The installer always targets the running guest kernel; this override is only
for the isolated build test.
