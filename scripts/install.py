#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Shared implementation of the host/guest installers; Python 3.9+ only."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from k10temp_virt import SensorStore  # noqa: E402

PROGRAM = "/usr/local/libexec/k10temp-virt"
PORT = "/dev/virtio-ports/org.k10temp-virt.0"
MODULE_FILES = ["k10temp_virt.c", "protocol.h", "Kbuild", "Makefile", "dkms.conf", "LICENSE",
                "COPYING", "LICENSES/exceptions/Linux-syscall-note"]


class System:
    """Centralize mutations so a dry run is read-only and tests can use a fixture."""

    def __init__(self, dry_run=False, root=Path("/")):
        self.dry_run = dry_run
        self.root = root

    def path(self, name):
        return self.root / name.lstrip("/")

    def run(self, *args, read_only=False):
        print("+ " + shlex.join(args), flush=True)
        if self.dry_run and not read_only:
            return ""
        result = subprocess.run(args, check=True, text=True, capture_output=read_only)
        return result.stdout if read_only else ""

    def require(self, *commands):
        missing = [cmd for cmd in commands if not shutil.which(cmd)]
        if missing:
            raise ValueError("missing required commands: " + ", ".join(missing))
        if not self.path("/run/systemd/system").is_dir():
            raise ValueError("run this installer on the target machine with systemd running")

    def backup(self, path, data=None, directory=None):
        directory = directory or path.parent
        print(f"Back up {path} to {directory}/{path.name}.bak.*", flush=True)
        if self.dry_run:
            return
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=path.name + ".bak.", dir=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(path.read_bytes() if data is None else data)
        print(f"Backup: {name}", flush=True)

    def write(self, name, data, mode=0o644, preserve=False):
        path = self.path(name)
        if path.is_symlink():
            raise ValueError(f"refusing to replace symlink: {path}")
        if path.exists():
            if preserve:
                print(f"Keep existing {path}", flush=True)
                return False
            if path.read_bytes() == data and path.stat().st_mode & 0o777 == mode:
                return False
            self.backup(path)
        print(f"Install {path} (mode {mode:04o})", flush=True)
        if self.dry_run:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                os.fchmod(stream.fileno(), mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True


def channel_args(vmid):
    return [
        "-device", "virtio-serial-pci,id=ktvserial",
        "-chardev", f"socket,id=ktvchar,path=/run/qemu-server/{vmid}.k10temp-virt.sock,"
        "server=on,wait=off",
        "-device", "virtserialport,bus=ktvserial.0,nr=1,chardev=ktvchar,"
        "name=org.k10temp-virt.0",
    ]


def merge_args(existing, vmid):
    """Keep original quoting; accept our complete channel or reject conflicts."""
    tokens = shlex.split(existing)
    desired = channel_args(vmid)
    found = 0
    for offset in range(0, len(desired), 2):
        pair = desired[offset:offset + 2]
        for index in range(len(tokens) - 1):
            if tokens[index:index + 2] == pair:
                del tokens[index:index + 2]
                found += 1
                break
    reserved = ("ktvserial", "ktvchar", "org.k10temp-virt.0", ".k10temp-virt.sock")
    if found not in (0, 3) or any(part in token for token in tokens for part in reserved):
        raise ValueError("existing QEMU args contain a partial, duplicate, or customized "
                         "k10temp-virt channel; reconcile it manually before installing")
    if found == 3:
        return existing
    return existing + (" " if existing else "") + shlex.join(desired)


def read_sources(names):
    # Read all project inputs before making any system changes.
    return {name: (PROJECT / name).read_bytes() for name in names}


def install_service(system, sources, name):
    system.write(PROGRAM, sources["k10temp_virt.py"], 0o755)
    system.write("/etc/systemd/system/" + name, sources["systemd/" + name])


def install_host(system, args):
    system.require("qm", "systemctl")
    unit = "k10temp-virt-host@.service"
    sources = read_sources(["k10temp_virt.py", "systemd/" + unit])
    vmid = str(args.vmid)
    vm_config = system.path(f"/etc/pve/qemu-server/{vmid}.conf")
    original = vm_config.read_bytes()  # also verifies the VM belongs to this node
    # Proxmox hashes the whole raw file, including snapshots and pending changes.
    digest = hashlib.sha1(original).hexdigest()
    config = system.run("qm", "config", vmid, read_only=True)
    fields = dict(line.split(": ", 1) for line in config.splitlines() if ": " in line)
    if fields.get("lock"):
        raise ValueError(f"VM {vmid} is locked: {fields['lock']}")
    if fields.get("template") == "1":
        raise ValueError("choose a VM, not a template")
    # qm config includes pending changes by default; don't discard pending args.
    existing = fields.get("args", "")
    merged = merge_args(existing, vmid)
    config_name = f"/etc/k10temp-virt/host-{vmid}.json"
    config_path = system.path(config_name)
    if args.sensor is not None:
        sensors = args.sensor
    elif config_path.exists():
        saved = json.loads(config_path.read_text())
        if not isinstance(saved, dict) or not isinstance(saved.get("sensors"), list):
            raise ValueError(f"{config_path}: expected an object with a sensors array")
        sensors = saved["sensors"]
    else:
        raise ValueError("first install requires --sensor CHIP:LABEL; "
                         "list choices with: python3 k10temp_virt.py list")
    store = SensorStore(sensors, system.path("/sys/class/hwmon"))
    for channel, selector in enumerate(sensors):
        try:
            value = store.read(channel)
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read {selector!r}: {exc}; "
                             "list choices with: python3 k10temp_virt.py list") from exc
        print(f"Channel {channel + 1}: {selector} = {value / 1000:g} °C", flush=True)

    install_service(system, sources, unit)
    data = (json.dumps({"sensors": sensors}, indent=2) + "\n").encode()
    system.write(config_name, data, preserve=args.sensor is None)
    if merged != existing:
        system.backup(vm_config, original, system.path("/var/backups/k10temp-virt"))
        system.run("qm", "set", vmid, "--digest", digest, "--args", merged)
    else:
        print("QEMU channel is already configured.", flush=True)
    if not system.dry_run:
        system.run("qm", "showcmd", vmid, "--pretty", read_only=True)
    system.run("systemctl", "daemon-reload")
    instance = f"k10temp-virt-host@{vmid}.service"
    system.run("systemctl", "enable", instance)
    system.run("systemctl", "restart", instance)
    print("\nDry run complete; no changes made." if system.dry_run else "\nHost setup complete.")
    print(f"Exported channels: {len(sensors)}; guest labels must match their count/order.")
    print("The installer does not stop or start the VM. To activate a newly added channel,\n"
          "run these on the Proxmox host when ready (your guest session will disconnect):\n"
          f"  qm shutdown {vmid} --timeout 120\n"
          f"  qm status {vmid}  # confirm stopped before the next command\n"
          f"  qm start {vmid}\n"
          "A reboot within the guest does not recreate the QEMU channel.")


def install_guest(system, _args):
    system.require("dkms", "make", "cc", "modprobe", "systemctl")
    kernel = os.uname().release
    if not system.path(f"/lib/modules/{kernel}/build/Makefile").is_file():
        raise ValueError(f"missing build headers for {kernel}; on Ubuntu/Debian install "
                         f"the matching linux-headers-{kernel} package")
    unit = "k10temp-virt-guest.service"
    sources = read_sources(MODULE_FILES + ["k10temp_virt.py", "systemd/" + unit,
                                          "config/k10temp-virt.conf"])
    settings = dict(re.findall(r'^PACKAGE_(NAME|VERSION)="([A-Za-z0-9_.+-]+)"$',
                               sources["dkms.conf"].decode(), re.MULTILINE))
    if set(settings) != {"NAME", "VERSION"}:
        raise ValueError("cannot read package name/version from dkms.conf")
    name, version = settings["NAME"], settings["VERSION"]
    loaded = system.path("/sys/module/k10temp_virt").exists()
    fingerprint = hashlib.sha256()
    for filename in MODULE_FILES:
        fingerprint.update(filename.encode() + b"\0" + sources[filename])
    stamp = f"/var/lib/k10temp-virt/{name}-{version}-{kernel}.sha256"
    signature = (fingerprint.hexdigest() + "\n").encode()
    changed = not system.path(stamp).exists() or system.path(stamp).read_bytes() != signature
    for filename in MODULE_FILES:
        system.write(f"/usr/src/{name}-{version}/{filename}", sources[filename])
    # build auto-adds an unregistered module. Force rebuilds handle source changes
    # within the same version; installation never unloads a running module.
    force = ["--force"] if changed else []
    system.run("dkms", "build", "-m", name, "-v", version, "-k", kernel, *force)
    system.run("dkms", "install", "-m", name, "-v", version, "-k", kernel, *force)
    # Record success only after installation, so interrupted updates rebuild on retry.
    system.write(stamp, signature)
    install_service(system, sources, unit)
    system.write("/etc/modprobe.d/k10temp-virt.conf", sources["config/k10temp-virt.conf"],
                 preserve=True)
    system.run("systemctl", "daemon-reload")
    system.run("systemctl", "enable", unit)
    if system.path(PORT).exists():
        system.run("systemctl", "restart", unit)
    else:
        print(f"{PORT} is not present yet; service enabled for the next boot.", flush=True)
    print("\nDry run complete; no changes made." if system.dry_run else "\nGuest setup complete.")
    if loaded and changed:
        print("The currently loaded module is retained. Reboot to use the newly built module.")
    print("After host setup and a full VM stop/start, check:\n"
          "  systemctl status k10temp-virt-guest\n"
          "  /usr/bin/sensors\n"
          "Restart Resources so it discovers the new CPU sensor.")


def vm_id(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("VM ID must be an integer") from exc
    if not 100 <= number <= 999999999:
        raise argparse.ArgumentTypeError("VM ID must be between 100 and 999999999")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    host = sub.add_parser("host", prog="install-host.sh",
                          help="install on the Proxmox node that owns the VM")
    host.add_argument("--vmid", type=vm_id, required=True)
    host.add_argument("--sensor", action="append", metavar="CHIP:LABEL",
                      help="repeat for multiple sources; omit to retain an existing config")
    guest = sub.add_parser("guest", prog="install-guest.sh", help="install inside the Linux guest")
    for command in (host, guest):
        command.add_argument("--dry-run", action="store_true",
                             help="check prerequisites and print actions without changing anything")
    args = parser.parse_args(argv)
    if os.geteuid() != 0 and not args.dry_run:
        parser.error("run with sudo (or use --dry-run to preview)")
    try:
        system = System(args.dry_run)
        (install_host if args.mode == "host" else install_guest)(system, args)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Installation stopped: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.rstrip(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
