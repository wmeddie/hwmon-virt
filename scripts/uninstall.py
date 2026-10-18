#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Remove a host or guest installation; preserve configuration unless --purge."""

import argparse
import errno
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.install import MODULE_FILES, System, split_channel, vm_id  # noqa: E402


def check_path(system, path):
    """Do not follow symlinks while removing installer-owned files."""
    path.relative_to(system.root)
    for parent in (path, *path.parents):
        if parent == system.root:
            break
        if parent.is_symlink():
            raise ValueError(f"refusing to remove through symlink: {parent}")
    if path.exists() and not path.is_file():
        raise ValueError(f"expected a regular installed file: {path}")


def remove_files(system, paths):
    for path in paths:
        check_path(system, path)
        if path.exists():
            print(f"Remove {path}", flush=True)
            if not system.dry_run:
                path.unlink()


def remove_empty_dirs(system, paths):
    for path in sorted(set(paths), key=lambda p: len(p.parts), reverse=True):
        if not path.exists() or path.is_symlink():
            continue
        if system.dry_run:
            print(f"Remove directory if empty: {path}", flush=True)
            continue
        try:
            path.rmdir()
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.ENOENT):
                raise


def service_state(system, unit):
    output = system.run("systemctl", "show", unit, "--property=LoadState",
                        "--property=ActiveState", read_only=True)
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if not fields.get("LoadState") or not fields.get("ActiveState"):
        raise ValueError(f"cannot determine service state: {unit}")
    return fields


def stop_service(system, unit, state):
    if state["LoadState"] != "not-found" or state["ActiveState"] not in ("inactive", "failed"):
        system.run("systemctl", "stop", unit)


def disable_service(system, unit, state):
    if state["LoadState"] != "not-found":
        system.run("systemctl", "disable", unit)


def other_hosts(system, name, excluded=None):
    """Conservatively retain shared files for configured, enabled or loaded VMs."""
    configs = system.path(f"/etc/{name}").glob("host-*.json")
    if any(p.name != f"host-{excluded}.json" for p in configs):
        return True
    pattern = f"{name}-host@*.service"
    for command in ("list-units", "list-unit-files"):
        # Some systemd versions return 1 when list-unit-files matches nothing.
        # List files without a pattern and filter below, so real failures still abort.
        selection = (pattern,) if command == "list-units" else ()
        output = system.run("systemctl", command, "--all", "--no-legend",
                            "--no-pager", "--plain", *selection, read_only=True)
        for line in output.splitlines():
            unit = line.split()[0] if line.split() else ""
            if (unit.startswith(name + "-host@") and unit.endswith(".service") and
                    unit not in (f"{name}-host@.service", f"{name}-host@{excluded}.service")):
                return True
    return False


def uninstall_host(system, args):
    system.require("qm", "systemctl")
    name = "hwmon-virt"
    vmid = str(args.vmid)
    unit = f"{name}-host@{vmid}.service"
    state = service_state(system, unit)
    shared = other_hosts(system, name, vmid)
    guest = service_state(system, f"{name}-guest.service")
    files = []
    if args.purge:
        files.append(system.path(f"/etc/{name}/host-{vmid}.json"))
    if not shared:
        files.append(system.path(f"/etc/systemd/system/{name}-host@.service"))
        if guest["LoadState"] == "not-found" and guest["ActiveState"] in ("inactive", "failed"):
            files.append(system.path(f"/usr/local/libexec/{name}"))
    for path in files:
        check_path(system, path)

    config = system.path(f"/etc/pve/qemu-server/{vmid}.conf")
    changed = False
    if not args.keep_channel and config.exists():
        original = config.read_bytes()
        output = system.run("qm", "config", vmid, read_only=True)
        fields = dict(line.split(": ", 1) for line in output.splitlines() if ": " in line)
        if fields.get("lock") or fields.get("template") == "1":
            raise ValueError("cannot edit a locked VM or a template")
        remaining, changed = split_channel(fields.get("args", ""), vmid)
        if changed:
            system.backup(config, original, system.path(f"/var/backups/{name}"))
            update = ("--args", shlex.join(remaining)) if remaining else ("--delete", "args")
            # Failure leaves the running service and installed files intact.
            system.run("qm", "set", vmid, "--digest", hashlib.sha1(original).hexdigest(), *update)
    stop_service(system, unit, state)
    disable_service(system, unit, state)
    remove_files(system, files)
    remove_empty_dirs(system, [system.path(f"/etc/{name}")])
    system.run("systemctl", "daemon-reload")
    if shared:
        print("Kept shared host files for other VMs.")
    if args.keep_channel:
        print("Kept the VM channel for reinstallation with the same names.")
    elif changed:
        print("Fully stop/start the VM when convenient to remove its active channel.\n"
              "No VM was stopped or started by this script.")


def installed_versions(system, name):
    output = system.run("dkms", "status", read_only=True)
    versions = set()
    for line in output.splitlines():
        # DKMS 2.x used 'name, version'; current versions use 'name/version'.
        if not re.match(re.escape(name) + r"(?:/|,)", line):
            continue
        match = re.match(re.escape(name) + r"(?:/|,\s*)([0-9][A-Za-z0-9_.+-]*)(?=[:,])", line)
        if not match:
            raise ValueError(f"unrecognized DKMS status: {line}")
        versions.add(match.group(1))
    return sorted(versions)


def guest_files(system, name, purge):
    files = [system.path(f"/etc/systemd/system/{name}-guest.service")]
    dirs = [system.path(f"/var/lib/{name}")]
    if purge:
        files.append(system.path(f"/etc/modprobe.d/{name}.conf"))
    for source in system.path("/usr/src").glob(name + "-*"):
        if not re.fullmatch(re.escape(name) + r"-[0-9][A-Za-z0-9_.+-]*", source.name):
            continue
        for filename in MODULE_FILES:
            path = source / filename
            files.append(path)
            dirs.extend(p for p in path.parents if p == source or source in p.parents)
    files.extend(system.path(f"/var/lib/{name}").glob(name + "-*.sha256"))
    return files, dirs


def uninstall_guest(system, args):
    system.require("dkms", "modprobe", "systemctl")
    name, module = "hwmon-virt", "hwmon_virt"
    unit = f"{name}-guest.service"
    state = service_state(system, unit)
    versions = installed_versions(system, name)
    files, dirs = guest_files(system, name, args.purge)
    if not other_hosts(system, name):
        files.append(system.path(f"/usr/local/libexec/{name}"))
    for path in files:
        check_path(system, path)
    stop_service(system, unit, state)
    if system.path(f"/sys/module/{module}").exists():
        # Never force removal. A busy module leaves files and DKMS intact.
        system.run("modprobe", "-r", module)
    disable_service(system, unit, state)
    for version in versions:
        system.run("dkms", "remove", "-m", name, "-v", version, "--all")
    remove_files(system, files)
    remove_empty_dirs(system, dirs)
    system.run("systemctl", "daemon-reload")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    host = sub.add_parser("host", prog="uninstall-host.sh")
    host.add_argument("--vmid", required=True, type=vm_id)
    host.add_argument("--keep-channel", action="store_true",
                      help="leave QEMU args for reinstallation using the same channel name")
    guest = sub.add_parser("guest", prog="uninstall-guest.sh")
    for command in (host, guest):
        command.add_argument("--dry-run", action="store_true", help="preview without making changes")
        command.add_argument("--purge", action="store_true", help="also remove this installation's configuration")
    args = parser.parse_args(argv)
    if os.geteuid() != 0 and not args.dry_run:
        parser.error("run with sudo (or use --dry-run to preview)")
    try:
        system = System(args.dry_run)
        (uninstall_host if args.mode == "host" else uninstall_guest)(system, args)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Uninstall stopped: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.rstrip(), file=sys.stderr)
        print("Fix the error and rerun.", file=sys.stderr)
        if args.mode == "guest":
            print("If the module is busy, close readers and retry; to resume readings instead,\n"
                  "start the guest service again.", file=sys.stderr)
        return 1
    print("Dry run complete; no changes made." if args.dry_run else "Uninstall complete.")
    if not args.purge:
        print("Configuration was kept. Use --purge to remove it as well.")
    print("Source checkouts, backups, and system packages are retained.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
