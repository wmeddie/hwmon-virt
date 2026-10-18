# k10temp-virt

Read host temperatures as native Linux sensors inside a VM, on demand.

A guest application reads `tempN_input`; a small guest module asks a userspace
bridge for a reading; the bridge requests it from the host over a persistent
virtio-serial channel. The host reads its existing sensor and returns the value.
A **100 ms in-memory cache** shares recent readings between applications.

There is no SSH, network listener, periodic temperature collector, or subprocess
per sample. When nobody reads a temperature, the services block waiting for work.
Disconnected services retry their connection every two seconds. Each service
logs one warning per outage; repeated failures are logged only with `--verbose`.
Successful communication resets this suppression and logs recovery.

```text
Resources / sensors / other hwmon readers
                 │ read tempN_input
                 ▼
     k10temp_virt.ko ── 100 ms per-channel cache
                 │ request/reply through /dev/k10temp-virt
                 ▼
        guest Python service
                 │ dedicated virtio-serial port
                 ▼
        host Python service
                 │ read a configured hwmon temperature
                 ▼
          host's sensor driver
```

## Scope

- One virtual hwmon device with 1–16 configurable temperature channels.
- AMD, Intel, GPU, or other host temperature sources; nothing accesses AMD
  hardware directly. Source order determines `temp1_input`, `temp2_input`, etc.
- Signed integer millidegrees Celsius, including subzero temperatures.
- Bounded reads, correlated request IDs, rejection of late replies, and explicit
  read errors on missing data. Expired readings are never served as a fallback.
- Kernel module plus one Python file, using only the standard library.
- DKMS and systemd examples. Requires a Linux guest with hwmon/virtio-console,
  QEMU on the host, Python 3.9+, and matching guest kernel headers for the build.

The reported values belong to the **physical host**, including other guests'
activity. Host hardware or its driver may itself cache readings. This project
does not force a new hardware conversion, control fans, or create thermal zones.

The default hwmon name is `k10temp` for CPU monitor compatibility. This is a
compatibility name, not the real AMD hardware driver. The module is independently
named `k10temp_virt`; its default channel label is `Host CPU`.
Resources 1.10.2 recognizes `k10temp`, `coretemp`, and `zenpower` by name. Other
applications have their own selection rules; a normal hwmon device does not
guarantee every application's CPU field will select it. Put the CPU source first
for a typical one- or two-channel setup, and restart Resources after setup because
it caches its sensor discovery. A neutral name such as `hosttemp` can be used
when automatic CPU classification is not wanted.

## Install with the scripts

Run each script from a copy of this project on the appropriate machine. The
installers need Python 3.9+ and systemd. The guest also needs DKMS, a compiler,
make, and headers for its running kernel. They check prerequisites without
installing distribution packages automatically.

In the **guest**:

```sh
sudo ./install-guest.sh --dry-run
sudo ./install-guest.sh
```

This installs the module through DKMS, the bridge, its default modprobe options,
and its systemd service. Existing modprobe options are preserved. If the virtio
port already exists, it starts/restarts the service; otherwise the service is
enabled for the next boot. Repeat installs skip an unchanged DKMS build. Updated
sources force a rebuild; an already-loaded module stays loaded until reboot.
Secure Boot key enrollment, if required by your guest, follows your distribution's
normal DKMS procedure.

On the **Proxmox host**, choose a source and replace `101` with your VM ID:

```sh
python3 k10temp_virt.py list
sudo ./install-host.sh --vmid 101 --sensor k10temp:Tctl --dry-run
sudo ./install-host.sh --vmid 101 --sensor k10temp:Tctl
```

The host installer verifies the selected sensor, installs the bridge and service,
and uses `qm set` to add the dedicated channel. Existing QEMU arguments, including
pending arguments, are preserved. A configuration digest guards against concurrent
VM edits. A complete existing channel is reused; partial, duplicate, or customized
channels are rejected for manual reconciliation. Full VM configuration backups
are saved under `/var/backups/k10temp-virt/`; changed configuration and service
files get adjacent `.bak.*` copies. Generated installations of the bridge, DKMS
sources, and build records are replaced without creating backup copies.
The VM is never stopped or started by the installer.

Repeat `--sensor` for additional channels and configure matching guest labels as
described below. On subsequent host installs, omit `--sensor` to preserve the
existing sensor configuration. Only the selected VM's host service is restarted.
Both installers stop on errors; any completed changes and their backups remain
in place so you can correct the error and rerun them.

To activate a newly added channel, use a Proxmox console or another machine:

```sh
qm shutdown 101 --timeout 120
qm status 101   # confirm stopped before continuing
qm start 101
```

This disconnects sessions in that guest. A reboot within the guest does not
recreate the QEMU command line. After it starts, check `/usr/bin/sensors` and
`systemctl status k10temp-virt-guest` in the guest, then restart Resources.
The manual setup sections below explain the individual steps.

## Build and check

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

Validated on Linux `7.0.0-38-generic`, Python 3.14.4, and QEMU 10.2.1:
51 distinct tests passed across the local, isolated DKMS, and disposable VM runs:
16 transport/source tests, 18 installer fixtures, 1 runner check, 1 C completion
check, 11 actual-module tests, 3 full virtio-serial/lm-sensors tests, and 1 DKMS
build test. The longest disconnected read in the VM run was 2.1 ms, below the
300 ms test limit and the former 500 ms timeout. The kernel sources also passed
`checkpatch` without warnings. Other kernel versions have not been tested.

Installer fixtures cover configuration preservation, repeat runs, dry runs,
failures, and channel conflicts. The host installer was also dry-run against
Proxmox VE 9.2.3. The isolated build passed through DKMS 3.2.2 using the installer
source set. These checks do not install the changes on the running host or guest.

## Set up the host

Copy this project onto the Proxmox host, then install just the Python program:

```sh
sudo make install-tools
/usr/local/libexec/k10temp-virt list
```

`list` prints selectors and their current paths, for example:

```text
k10temp:Tctl        /sys/devices/.../hwmon/hwmon2/temp1_input
coretemp:Package id 0 /sys/devices/.../hwmon/hwmon3/temp1_input
```

Choose the appropriate selector for that host. The program resolves chip name
and channel label, instead of assuming `hwmon2` will remain stable across boots.
If more than one source matches, it reports unavailable; choose an explicit
absolute input path for that case, and ensure it remains stable. An attribute
name such as `k10temp:temp1_input` is also accepted.

For an example VM **101**, create `/etc/k10temp-virt/host-101.json`:

```json
{
  "sensors": ["k10temp:Tctl"]
}
```

Use `coretemp:Package id 0` on an appropriate Intel host, or configure additional
sources in the array. Only these explicitly configured sources are exported;
the guest sends a numeric channel, never a filesystem path or command.

### Add a dedicated QEMU channel

With VM 101 shut down, append these arguments to its **existing** `args:` entry
in `/etc/pve/qemu-server/101.conf`, or add an entry if it has none. Keep this on
one line, replace `101` with your VM ID, and preserve unrelated arguments:

```text
args: -device virtio-serial-pci,id=ktvserial -chardev socket,id=ktvchar,path=/run/qemu-server/101.k10temp-virt.sock,server=on,wait=off -device virtserialport,bus=ktvserial.0,nr=1,chardev=ktvchar,name=org.k10temp-virt.0
```

This adds its own controller and port, independent of the QEMU guest agent.
QEMU creates the listening Unix socket; the host service connects to it. Its
socket and parent directory should remain accessible only to the intended host
administrator/service. No TCP port or guest network configuration is involved.

Inspect the resulting command with `qm showcmd 101 --pretty`, then start the VM.
The guest should have `/dev/virtio-ports/org.k10temp-virt.0`. Device changes require
a full VM stop/start; restarting a service or rebooting within the guest is not
sufficient to recreate the QEMU command line.

Install and start the host service:

```sh
sudo install -Dm644 systemd/k10temp-virt-host@.service \
  /etc/systemd/system/k10temp-virt-host@.service
sudo systemctl daemon-reload
sudo systemctl enable --now k10temp-virt-host@101
```

The example service uses root to connect to Proxmox's root-owned QEMU socket.
It only reads configured temperature files and serves values over that socket.
Outside Proxmox, the same QEMU arguments and `host --socket PATH --sensor
CHIP:LABEL` command work; choose your own socket path and service configuration.
For multiple VMs, use a separate channel/socket and host service per VM. Live
migration/HA of the custom channel is not managed by this project; arrange the
service/configuration on each target host before moving the VM.

## Set up the guest

For a one-time trial after building:

```sh
sudo insmod ./k10temp_virt.ko
sudo python3 k10temp_virt.py guest
```

From another terminal, run the **real** `sensors` command, or read the new
`temp1_input` under `/sys/class/hwmon/`. The bridge must be running; otherwise
the sensor returns a read error. Stop the bridge before `sudo rmmod k10temp_virt`.

For persistent installation with DKMS installed:

```sh
sudo install -d /usr/src/k10temp-virt-0.1.0
sudo install -m644 k10temp_virt.c k10temp_virt_internal.h protocol.h Kbuild Makefile dkms.conf \
  /usr/src/k10temp-virt-0.1.0/
sudo dkms add -m k10temp-virt -v 0.1.0
sudo dkms build -m k10temp-virt -v 0.1.0
sudo dkms install -m k10temp-virt -v 0.1.0
sudo make install-tools
sudo install -Dm644 config/k10temp-virt.conf /etc/modprobe.d/k10temp-virt.conf
sudo install -Dm644 systemd/k10temp-virt-guest.service \
  /etc/systemd/system/k10temp-virt-guest.service
sudo systemctl daemon-reload
sudo systemctl enable --now k10temp-virt-guest
```

Use your distribution's DKMS module-signing/key-enrollment procedure if the
guest enforces signed modules. The project does not change Secure Boot settings.

### Parameters and additional channels

All module parameters are set at load time:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `sensor_name` | `k10temp` | Nonempty hwmon name; whitespace, `-`, and `*` are rejected |
| `labels` | `Host CPU` | Comma-separated channel labels, 1–16, up to 64 characters each |
| `cache_ms` | `100` | Successful reading cache lifetime, 0–60000 ms; 0 disables caching |
| `timeout_ms` | `1000` | Per-request kernel response timeout, 1–10000 ms |

For host sources `["k10temp:Tctl", "k10temp:Tccd1"]`, configure two guest labels:

```text
options k10temp_virt sensor_name=k10temp labels=Host_CPU,Host_CCD1 cache_ms=100 timeout_ms=1000
```

The host export count/order and guest label count/order must match. Each channel
has its own cache. Successful reads are timestamped at request start so a slow
reply does not gain an extra cache lifetime. An error is never cached. Closing
the guest bridge invalidates all cached values immediately; a host failure may
remain invisible until a still-fresh cache entry expires (at most `cache_ms`).
Concurrent reads are serialized, so nearby readers share the resulting cache.
Waiting behind another reader can add latency to the per-request timeout.

The guest bridge defaults to a 500 ms transport deadline, shorter than the
module's 1000 ms deadline. If changing these, keep the bridge's `--timeout`
below the module's `timeout_ms`. A disconnected host fails promptly with
`ENOTCONN`; reads while the guest bridge reconnects return `ENODATA`. A connected
but unresponsive host still uses the transport deadline. Applications usually
display failed reads as unavailable. CPU utilization and graph cadence are
controlled by the consuming application; this module supplies temperature only.

## Protocol and implementation

`protocol.h` defines a 24-byte, little-endian message shared by the module,
bridge, and host. It contains the `KTV1` magic/version, a 64-bit request ID, a
zero-based channel, a signed status, and a signed millidegree value. Requests
have zero status/value. Replies echo the ID/channel and carry zero status or
`-EIO`, `-ENODATA`, `-EPROTO`, `-ETIMEDOUT`, or `-ENOTCONN`. Other negative statuses
in the Linux errno range (−4095 through −1) become `-EIO` at both the guest bridge
and kernel boundary, preventing kernel-internal restart codes from reaching
applications. Positive statuses and values below −4095 are malformed.
Accepted temperatures range from −273150 to 1000000 m°C.

The root-only `/dev/k10temp-virt` accepts exactly one bridge opener. It supports
blocking/nonblocking reads and poll; each read yields one complete request and
each write must contain one complete response. Replies are accepted only for
the currently pending, delivered request, otherwise writes return `ESTALE`.
Each module load seeds its request counter randomly to avoid matching old
transport data after a reload. Bridge replies use a separate lock from hwmon
reads, so a waiting hwmon callback cannot deadlock its responder. A completed
reply observed under the state lock wins over an expired wait; a wait interrupted
by a signal retains its interruption result.

The Python stream layer uses `poll` to detect hangups even when a disconnected
virtio port is not writable, while draining readable data before reporting EOF.
It handles fragmented/coalesced frames and discards replies with mismatched
IDs/channels. After a torn frame or reconnect, a request may fail and subsequent
reads recover; no stale-data fallback is used. The wire format is a small
project-specific protocol, not a standard VirtIO sensor device.

## Troubleshooting and removal

- Missing virtio port: check the QEMU arguments and fully stop/start the VM.
- `ENODATA` before the bridge starts is expected. Check `systemctl status
  k10temp-virt-guest` and `journalctl -u k10temp-virt-guest`.
- Host source errors: run `k10temp-virt list` on the host and check
  `journalctl -u k10temp-virt-host@101`. `--verbose` before the subcommand adds
  details about missing sources and expired requests.
- `EBUSY` opening the bridge: another bridge process already has it open.
- Resources stays at N/A: restart it after the bridge is available, confirm the
  `k10temp` name and a readable temperature file, and check sandbox access to sysfs.

To remove the guest installation, stop/disable its service, unload
`k10temp_virt`, and run `dkms remove -m k10temp-virt -v 0.1.0 --all`. Remove its
service, modprobe configuration, `/usr/src/k10temp-virt-0.1.0`, and installed
`/usr/local/libexec/k10temp-virt` if unused. The guest installer's build records
under `/var/lib/k10temp-virt/` can also be removed. On the host, stop/disable the
corresponding service, remove its configuration and only this project's QEMU
arguments, then fully stop/start the VM. Run `systemctl daemon-reload` after
removing unit files.

## License and references

GPL version 2 **only**, matching the kernel's GPL version. `protocol.h` has the
Linux syscall exception, like kernel UAPI headers. See [COPYING](COPYING),
[LICENSE](LICENSE), and [the exception](LICENSES/exceptions/Linux-syscall-note).

- [Linux hwmon API](https://docs.kernel.org/hwmon/hwmon-kernel-api.html)
- [Linux hwmon sysfs interface](https://docs.kernel.org/hwmon/sysfs-interface.html)
- [QEMU character devices](https://www.qemu.org/docs/master/system/invocation.html#character-device-options)
- [Proxmox VM configuration](https://pve.proxmox.com/pve-docs/qm.conf.5.html)
- [Proxmox qm commands](https://pve.proxmox.com/pve-docs/qm.1.html)
- [Resources 1.10.2 CPU sensor selection](https://github.com/nokyan/resources/blob/v1.10.2/src/utils/cpu.rs)
