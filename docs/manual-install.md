# Manual installation

These are the individual steps performed by the [install scripts](../README.md#installation).
Run commands from the project directory on the indicated machine. Examples use
VM **101**; substitute your guest's ID. For an existing installation, follow
[Upgrading](../README.md#upgrading).

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
sudo python3 hwmon_virt.py guest
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

## Monitoring applications

The default hwmon name is `k10temp` for CPU monitor compatibility. This is a
compatibility name, not the real AMD hardware driver. The module is independently
named `k10temp_virt`; its default channel label is `Host CPU`.
Resources 1.10.2 recognizes `k10temp`, `coretemp`, and `zenpower` by name. Other
applications have their own selection rules; a normal hwmon device does not
guarantee every application's CPU field will select it. Put the CPU source first
for a typical one- or two-channel setup. If Resources still shows N/A after
setup, restart it to refresh its cached sensor discovery. A neutral name such as
`hosttemp` can be used when automatic CPU classification is not wanted.

## Installer behavior

The host installer verifies the selected sensor, installs the bridge and service,
and uses `qm set` to add the dedicated channel. Existing QEMU arguments, including
pending arguments, are preserved. A configuration digest guards against concurrent
VM edits. A complete existing channel is reused; partial, duplicate, or customized
channels are rejected for manual reconciliation. Full VM configuration backups
are saved under `/var/backups/k10temp-virt/`; changed configuration and service
files get adjacent `.bak.*` copies. Generated installations of the bridge, DKMS
sources, and build records are replaced without creating backup copies.
The VM is never stopped or started by the installer.

Both installers stop on errors; completed changes and their backups remain
in place so you can correct the error and rerun them. The guest installer
preserves existing modprobe options and rebuilds DKMS sources when they change.
It leaves a loaded module in place until you reload it or reboot.

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

