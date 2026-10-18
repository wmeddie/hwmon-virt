# SPDX-License-Identifier: GPL-2.0-only
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import install as installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.system = installer.System(root=self.root)
        self.output = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.output))
        stack.enter_context(mock.patch.object(installer.shutil, "which", side_effect=lambda cmd: cmd))
        self.commands = []
        stack.enter_context(mock.patch.object(installer.subprocess, "run", side_effect=self.run_command))
        self.system.path("/run/systemd/system").mkdir(parents=True)
        self.put("/sys/class/hwmon/hwmon7/name", "k10temp\n")
        self.put("/sys/class/hwmon/hwmon7/temp1_label", "Tctl\n")
        self.put("/sys/class/hwmon/hwmon7/temp1_input", "56500\n")
        self.config_path = "/etc/pve/qemu-server/101.conf"
        self.original = "args: -smbios 'type=0,vendor=Example Host'\n\n[snapshot]\nargs: -no-reboot\n"
        self.put(self.config_path, self.original)
        # qm config returns pending args, not necessarily the current raw args.
        self.qm_config = "name: Ubuntu\nargs: -smbios 'type=0,vendor=Pending Host'\n"
        self.args = argparse.Namespace(vmid=101, sensor=["k10temp:Tctl"])
        self.kernel = os.uname().release
        self.put(f"/lib/modules/{self.kernel}/build/Makefile", "fixture")

    def put(self, name, text):
        path = self.system.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_command(self, command, **_kwargs):
        self.commands.append(command)
        result = ""
        if command[:2] == ("qm", "config"):
            result = self.qm_config
        elif command[:2] == ("qm", "set"):
            digest = command[command.index("--digest") + 1]
            self.assertEqual(digest, hashlib.sha1(self.system.path(self.config_path).read_bytes()).hexdigest())
            value = command[command.index("--args") + 1]
            self.qm_config = "name: Ubuntu\nargs: " + value + "\n"
            self.put(self.config_path, self.qm_config)
        return subprocess.CompletedProcess(command, 0, result, "")

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def test_host_preserves_pending_args_backs_up_full_config_and_is_repeatable(self):
        installer.install_host(self.system, self.args)
        updated = next(cmd[-1] for cmd in self.commands if cmd[:2] == ("qm", "set"))
        self.assertTrue(updated.startswith("-smbios 'type=0,vendor=Pending Host' "))
        self.assertEqual(shlex.split(updated).count("virtio-serial-pci,id=ktvserial"), 1)
        backups = list(self.system.path("/var/backups/k10temp-virt").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), self.original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        config_path = self.system.path("/etc/k10temp-virt/host-101.json")
        self.assertEqual(json.loads(config_path.read_text()), {"sensors": ["k10temp:Tctl"]})
        # A repeated install without selectors preserves existing formatting too.
        saved = '{"sensors": ["k10temp:Tctl"]}\n'
        config_path.write_text(saved)
        self.args.sensor = None
        self.commands.clear()
        installer.install_host(self.system, self.args)
        self.assertEqual(config_path.read_text(), saved)
        self.assertFalse(any(cmd[:2] == ("qm", "set") for cmd in self.commands))
        self.assertFalse(any(cmd[:2] in (("qm", "shutdown"), ("qm", "start"), ("qm", "stop"))
                             for cmd in self.commands))
        self.assertIn(("systemctl", "restart", "k10temp-virt-host@101.service"), self.commands)

    def test_host_dry_run_does_not_write_or_run_mutating_commands(self):
        before = self.snapshot()
        self.system.dry_run = True
        installer.install_host(self.system, self.args)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.commands, [("qm", "config", "101")])
        self.assertIn("qm set 101 --digest", self.output.getvalue())
        self.assertFalse(self.system.path("/var/backups/k10temp-virt").exists())

    def test_host_rejects_bad_sensor_before_writing(self):
        before = self.snapshot()
        for sensor in ("k10temp:missing", "", "not-a-selector"):
            self.args.sensor = [sensor]
            with self.assertRaises(ValueError):
                installer.install_host(self.system, self.args)
            self.assertEqual(self.snapshot(), before)
        self.assertTrue(all(cmd[:2] == ("qm", "config") for cmd in self.commands))

    def test_host_conflicts_and_locked_vm_leave_system_untouched(self):
        before = self.snapshot()
        for config in ("args: -device virtio-serial-pci,id=ktvserial\n", "lock: backup\n",
                       "template: 1\n"):
            self.qm_config = config
            with self.assertRaises(ValueError):
                installer.install_host(self.system, self.args)
            self.assertEqual(self.snapshot(), before)

    def test_host_requires_explicit_first_source_and_valid_saved_config(self):
        self.args.sensor = None
        with self.assertRaisesRegex(ValueError, "first install"):
            installer.install_host(self.system, self.args)
        for saved in ('[]', '{"sensors": "k10temp:Tctl"}', '{"sensors": []}'):
            self.put("/etc/k10temp-virt/host-101.json", saved)
            with self.assertRaises(ValueError):
                installer.install_host(self.system, self.args)

    def test_config_update_failure_does_not_enable_service(self):
        def fail_update(command, **kwargs):
            if command[:2] == ("qm", "set"):
                raise subprocess.CalledProcessError(1, command, stderr="checksum mismatch")
            return self.run_command(command, **kwargs)

        with mock.patch.object(installer.subprocess, "run", side_effect=fail_update):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install_host(self.system, self.args)
        self.assertFalse(any(cmd[0] == "systemctl" for cmd in self.commands))
        backups = list(self.system.path("/var/backups/k10temp-virt").iterdir())
        self.assertEqual(backups[0].read_text(), self.original)

    def test_guest_builds_for_running_kernel_and_preserves_options(self):
        options = "options k10temp_virt labels=CPU,CCD cache_ms=200\n"
        config_path = self.put("/etc/modprobe.d/k10temp-virt.conf", options)
        for _ in range(2):
            installer.install_guest(self.system, self.args)
        self.assertEqual(config_path.read_text(), options)
        self.assertIn(("dkms", "build", "-m", "k10temp-virt", "-v", "0.1.0",
                       "-k", self.kernel, "--force"), self.commands)
        self.assertIn(("dkms", "install", "-m", "k10temp-virt", "-v", "0.1.0",
                       "-k", self.kernel, "--force"), self.commands)
        self.assertIn(("dkms", "build", "-m", "k10temp-virt", "-v", "0.1.0",
                       "-k", self.kernel), self.commands)
        self.assertNotIn(("systemctl", "restart", "k10temp-virt-guest.service"), self.commands)
        self.assertEqual(self.system.path(installer.PROGRAM).stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.system.path("/usr/src/k10temp-virt-0.1.0/Kbuild").read_bytes(),
                         (installer.PROJECT / "Kbuild").read_bytes())

    def test_guest_starts_if_port_exists_without_unloading_module(self):
        self.put(installer.PORT, "")
        self.system.path("/sys/module/k10temp_virt").mkdir(parents=True)
        installer.install_guest(self.system, self.args)
        self.assertIn(("systemctl", "restart", "k10temp-virt-guest.service"), self.commands)
        self.assertFalse(any(cmd[0] in ("modprobe", "rmmod") for cmd in self.commands))
        self.assertIn("currently loaded module is retained", self.output.getvalue())

    def test_guest_retries_an_interrupted_update_even_when_sources_match(self):
        installer.install_guest(self.system, self.args)
        stamp = self.system.path(f"/var/lib/k10temp-virt/k10temp-virt-0.1.0-{self.kernel}.sha256")
        stamp.write_text("previous successful build\n")
        self.commands.clear()
        installer.install_guest(self.system, self.args)
        builds = [cmd for cmd in self.commands if cmd[:2] == ("dkms", "build")]
        self.assertIn("--force", builds[0])
        self.assertNotEqual(stamp.read_text(), "previous successful build\n")

    def test_derived_files_are_replaced_without_backup_clutter(self):
        source = self.put("/usr/src/k10temp-virt-0.1.0/k10temp_virt.c", "old source\n")
        self.put(installer.PROGRAM, "old bridge\n")
        installer.install_guest(self.system, self.args)
        self.assertEqual(source.read_bytes(), (installer.PROJECT / "k10temp_virt.c").read_bytes())
        self.assertFalse(list(source.parent.glob("*.bak.*")))
        self.assertFalse(list(self.system.path(installer.PROGRAM).parent.glob("*.bak.*")))

    def test_qemu_generation_failure_stops_before_starting_service(self):
        def fail_showcmd(command, **kwargs):
            if command[:2] == ("qm", "showcmd"):
                raise subprocess.CalledProcessError(1, command, stderr="invalid QEMU configuration")
            return self.run_command(command, **kwargs)

        with mock.patch.object(installer.subprocess, "run", side_effect=fail_showcmd):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install_host(self.system, self.args)
        self.assertFalse(any(cmd[0] == "systemctl" for cmd in self.commands))

    def test_guest_dry_run_and_missing_headers_do_not_write(self):
        self.system.dry_run = True
        before = self.snapshot()
        installer.install_guest(self.system, self.args)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.commands, [])
        self.system.path(f"/lib/modules/{self.kernel}/build/Makefile").unlink()
        with self.assertRaisesRegex(ValueError, "missing build headers"):
            installer.install_guest(self.system, self.args)

    def test_guest_build_failure_does_not_install_or_enable_service(self):
        with mock.patch.object(installer.subprocess, "run",
                               side_effect=subprocess.CalledProcessError(1, "dkms")):
            with self.assertRaises(subprocess.CalledProcessError):
                installer.install_guest(self.system, self.args)
        self.assertFalse(self.system.path(installer.PROGRAM).exists())
        self.assertFalse(self.system.path("/etc/systemd/system/k10temp-virt-guest.service").exists())

    def test_file_updates_have_backups_and_reject_symlinks(self):
        path = self.put("/etc/example.conf", "before\n")
        self.system.write("/etc/example.conf", b"after\n")
        self.assertEqual(path.read_bytes(), b"after\n")
        self.assertEqual(next(path.parent.glob("example.conf.bak.*")).read_text(), "before\n")
        link = self.system.path("/etc/link.conf")
        link.symlink_to(path)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.system.write("/etc/link.conf", b"bad\n")
        self.assertEqual(path.read_bytes(), b"after\n")


class ArgumentTests(unittest.TestCase):
    def test_release_versions_match_and_cli_reports_version(self):
        from hwmon_virt import VERSION

        _, package_version = installer.package_identity((installer.PROJECT / "dkms.conf").read_text())
        module_version = re.search(r'MODULE_VERSION\("([^"]+)"\)',
                                   (installer.PROJECT / "k10temp_virt.c").read_text()).group(1)
        self.assertEqual(package_version, VERSION)
        self.assertEqual(module_version, VERSION)
        output = subprocess.check_output([sys.executable, str(installer.PROJECT / "hwmon_virt.py"),
                                          "--version"], text=True)
        self.assertEqual(output.strip(), f"hwmon-virt {VERSION}")

    def test_package_metadata_is_parsed_without_executing_shell(self):
        self.assertEqual(installer.package_identity('PACKAGE_NAME="example"\nPACKAGE_VERSION="1.2"\n'),
                         ("example", "1.2"))
        for config in ('PACKAGE_NAME="missing-version"',
                       'PACKAGE_NAME="$(touch /tmp/not-executed)"\nPACKAGE_VERSION="1.2"'):
            with self.assertRaises(ValueError):
                installer.package_identity(config)

    def test_existing_channel_is_not_duplicated(self):
        args = installer.merge_args("-no-reboot", 101)
        self.assertEqual(installer.merge_args(args, 101), args)
        # Equivalent shell quoting is recognized without rewriting it.
        quoted = args.replace("virtio-serial-pci,id=ktvserial", "'virtio-serial-pci,id=ktvserial'")
        self.assertEqual(installer.merge_args(quoted, 101), quoted)

    def test_partial_duplicate_or_other_vmid_channel_is_rejected(self):
        args = installer.merge_args("", 101)
        for existing in (args + " " + args, args.replace("101.k10temp", "222.k10temp"),
                         "-chardev socket,id=ktvchar,path=/elsewhere", args + " -name 'broken"):
            with self.subTest(existing=existing):
                with self.assertRaises(ValueError):
                    installer.merge_args(existing, 101)

    def test_vmid_validation(self):
        self.assertEqual(installer.vm_id("101"), 101)
        for value in ("99", "1000000000", "../101", "abc"):
            with self.assertRaises(argparse.ArgumentTypeError):
                installer.vm_id(value)


if __name__ == "__main__":
    unittest.main()
