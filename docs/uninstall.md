# Uninstalling

Run the scripts from the project directory on each machine. Remove the guest
installation first, then the host service for that VM. These examples use VM
**101**; substitute your VM's ID. Host commands run as root.

## Guest

```sh
sudo ./uninstall-guest.sh --dry-run
sudo ./uninstall-guest.sh
```

The script stops the guest service, unloads `hwmon_virt`, removes all registered
versions of its DKMS package for all kernels, and removes the installed service,
module source files, and build records. It removes the shared program unless
the same machine also uses it to serve temperatures to other VMs.

`/etc/modprobe.d/hwmon-virt.conf` is kept. To remove it as well:

```sh
sudo ./uninstall-guest.sh --purge
```

If unloading reports that the module is busy, close temperature readers and
retry. The script never forces unloading and leaves installed files and DKMS
records intact when unloading fails. To resume temperature sharing instead:

```sh
sudo systemctl start hwmon-virt-guest
```

## Host

```sh
./uninstall-host.sh --vmid 101 --dry-run
./uninstall-host.sh --vmid 101
```

The script removes the selected VM's temperature channel from its QEMU
configuration and stops and disables its host service. Unrelated QEMU arguments,
including pending changes, are preserved. Before editing, it saves the complete
VM configuration under `/var/backups/hwmon-virt/`. A concurrent configuration
change causes the edit to fail instead of overwriting that change.

`/etc/hwmon-virt/host-101.json` is kept. Add `--purge` to remove that file too.
Shared service files and the program are retained if another VM has a sensor
configuration or an enabled or loaded host service. They are also retained as
needed when this machine runs the guest service.

The script does not shut down or start the VM. The channel disappears from a
running VM after its next full stop/start. A reboot from inside the guest is
not sufficient to remove the device.

For a temporary uninstall before reinstalling, keep the existing channel:

```sh
./uninstall-host.sh --vmid 101 --keep-channel
```

Reinstalling with this channel still active does not require a VM stop/start.

If the channel was customized manually, the script stops before changing the
configuration or service. Review the VM's `args` setting and remove only the
temperature channel's options, or use `--keep-channel` to leave the entire
QEMU configuration alone. A deleted VM's host service can still be removed.

## Files that remain

Neither script removes the project checkout, system packages such as DKMS or
Python, configuration backups, journal history, or unexpected files in the
module source directory. An empty source directory is removed; one containing
extra files is kept for you to review. Files reached through symlinks are not
deleted.

`--dry-run` only reads state and prints planned actions; it cannot predict
whether a module will unload or a service command will succeed. Both scripts
can be rerun after an interrupted removal. Fix any reported error before retrying.

After removal, check the guest with `dkms status` and `lsmod`, and check the
host with `qm config 101`. The project should no longer appear in those results,
except for a channel deliberately retained with `--keep-channel`.
