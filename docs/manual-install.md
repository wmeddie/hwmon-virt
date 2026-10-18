# Manual installation

These are the individual steps performed by the [install scripts](../README.md#installation)
on a Proxmox VE host and a Linux guest.
Run commands from the project directory on the indicated machine. Examples use
VM **101**; substitute your guest's ID. For an existing installation, follow
[Upgrading](../README.md#upgrading).

## Set up the host

Copy this project onto the Proxmox host, then install just the Python program:

```sh
sudo make install-tools
/usr/local/libexec/hwmon-virt list
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

For an example VM **101**, create `/etc/hwmon-virt/host-101.json`:

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
args: -device virtio-serial-pci,id=hmvserial -chardev socket,id=hmvchar,path=/run/qemu-server/101.hwmon-virt.sock,server=on,wait=off -device virtserialport,bus=hmvserial.0,nr=1,chardev=hmvchar,name=org.hwmon-virt.0
```

This adds its own controller and port, independent of the QEMU guest agent.
QEMU creates the listening Unix socket; the host service connects to it. Its
socket and parent directory should remain accessible only to the intended host
administrator/service. No TCP port or guest network configuration is involved.

Inspect the resulting command with `qm showcmd 101 --pretty`, then start the VM.
The guest should have `/dev/virtio-ports/org.hwmon-virt.0`. Device changes require
a full VM stop/start; restarting a service or rebooting within the guest is not
sufficient to recreate the QEMU command line.

Install and start the host service:

```sh
sudo install -Dm644 systemd/hwmon-virt-host@.service \
  /etc/systemd/system/hwmon-virt-host@.service
sudo systemctl daemon-reload
sudo systemctl enable --now hwmon-virt-host@101
```

The example service uses root to connect to Proxmox's root-owned QEMU socket.
It only reads configured temperature files and serves values over that socket.
For multiple VMs, use a separate channel/socket and host service per VM. Live
migration/HA of the custom channel is not managed by this project; arrange the
service/configuration on each target host before moving the VM.

## Set up the guest

For a one-time trial after building:

```sh
sudo insmod ./hwmon_virt.ko
sudo python3 hwmon_virt.py guest
```

From another terminal, run the **real** `sensors` command, or read the new
`temp1_input` under `/sys/class/hwmon/`. The bridge must be running; otherwise
the sensor returns a read error. Stop the bridge before `sudo rmmod hwmon_virt`.

For persistent installation with DKMS installed:

```sh
sudo install -d /usr/src/hwmon-virt-0.1.0
sudo install -m644 hwmon_virt.c hwmon_virt_internal.h protocol.h Kbuild Makefile dkms.conf \
  /usr/src/hwmon-virt-0.1.0/
sudo dkms add -m hwmon-virt -v 0.1.0
sudo dkms build -m hwmon-virt -v 0.1.0
sudo dkms install -m hwmon-virt -v 0.1.0
sudo make install-tools
sudo install -Dm644 config/hwmon-virt.conf /etc/modprobe.d/hwmon-virt.conf
sudo install -Dm644 systemd/hwmon-virt-guest.service \
  /etc/systemd/system/hwmon-virt-guest.service
sudo systemctl daemon-reload
sudo systemctl enable --now hwmon-virt-guest
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
options hwmon_virt sensor_name=k10temp labels=Host_CPU,Host_CCD1 cache_ms=100 timeout_ms=1000
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
named `hwmon_virt`; its default channel label is `Host CPU`.
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
are saved under `/var/backups/hwmon-virt/`; changed configuration and service
files get adjacent `.bak.*` copies. Generated installations of the bridge, DKMS
sources, and build records are replaced without creating backup copies.
The VM is never stopped or started by the installer.

Both installers stop on errors; completed changes and their backups remain
in place so you can correct the error and rerun them. The guest installer
preserves existing modprobe options and rebuilds DKMS sources when they change.
It leaves a loaded module in place until you reload it or reboot.

## Troubleshooting

- Missing virtio port: check the QEMU arguments and fully stop/start the VM.
- `ENODATA` before the bridge starts is expected. Check `systemctl status
  hwmon-virt-guest` and `journalctl -u hwmon-virt-guest`.
- Host source errors: run `hwmon-virt list` on the host and check
  `journalctl -u hwmon-virt-host@101`. `--verbose` before the subcommand adds
  details about missing sources and expired requests.
- `EBUSY` opening the bridge: another bridge process already has it open.
- Resources stays at N/A: restart it after the bridge is available, confirm the
  `k10temp` name and a readable temperature file, and check sandbox access to sysfs.

For removal, use the [uninstall scripts](uninstall.md).
