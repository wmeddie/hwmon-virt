# hwmon-virt

Expose host temperatures as native Linux hwmon sensors inside virtual machines, on demand.

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

The repository is named **hwmon-virt**. The installed module, executable, services,
and transport retain their original `k10temp-virt` / `k10temp_virt` names so existing
installations can upgrade without changing their QEMU channel. These names do not
restrict the host CPU vendor.

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
for a typical one- or two-channel setup. If Resources still shows N/A after
setup, restart it to refresh its cached sensor discovery. A neutral name such as
`hosttemp` can be used when automatic CPU classification is not wanted.

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
sources force a rebuild; an already-loaded module stays loaded until you reload
it or reboot. See [Upgrading](#upgrading).
Secure Boot key enrollment, if required by your guest, follows your distribution's
normal DKMS procedure.

On the **Proxmox host**, choose a source. All examples use VM **101**; replace
it with the ID of your guest:

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
described in [Parameters and additional channels](docs/manual-install.md#parameters-and-additional-channels).
On subsequent host installs, omit `--sensor` to preserve the existing sensor
configuration. Only the selected VM's host service is restarted.
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
`systemctl status k10temp-virt-guest` in the guest. If Resources still shows
N/A, restart it. [Manual setup](docs/manual-install.md) explains the individual steps.

## Upgrading

Use the same new release of this project on both the host and guest. Run the
installers from the new source directory. Existing installations from before
the repository rename use the same commands and configuration paths.

On the **host**, omit `--sensor` to retain the existing source configuration:

```sh
sudo ./install-host.sh --vmid 101
```

In the **guest**:

```sh
sudo ./install-guest.sh
```

After the guest installer succeeds, activate an updated kernel module without
rebooting:

```sh
sudo systemctl stop k10temp-virt-guest &&
sudo modprobe -r k10temp_virt &&
sudo systemctl start k10temp-virt-guest
```

The service loads the new module before starting its bridge. If unloading reports
that the module is in use, start the service again and use a guest reboot to load
the update. Do not force-unload the module. A guest reboot is also a valid
alternative to the reload sequence.

An upgrade using an existing, active channel needs **no Proxmox host reboot or
VM stop/start**. Readings are briefly unavailable while the services/module
restart. Check the result in the guest:

```sh
systemctl is-active k10temp-virt-guest
/usr/bin/sensors
/usr/local/libexec/k10temp-virt --version
```

Restart a monitoring application only if its readings remain unavailable.

## Configuration and documentation

The guest defaults to one channel, the hwmon name `k10temp`, a 100 ms cache, and
a 1000 ms request timeout. The Python bridge uses a shorter 500 ms deadline.
Host source order must match guest channel order. Configure additional labels,
cache lifetime, and device name through the [module parameters](docs/manual-install.md#parameters-and-additional-channels).

- [Manual host and guest setup](docs/manual-install.md), including generic QEMU usage.
- [Building, testing, and compatibility](docs/testing.md).
- [Protocol and implementation](docs/protocol.md).
- [Contributing](CONTRIBUTING.md) and [changelog](CHANGELOG.md).

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
