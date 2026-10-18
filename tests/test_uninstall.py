# SPDX-License-Identifier: GPL-2.0-only
import argparse
import contextlib
import hashlib
import io
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import install, uninstall


class UninstallerTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.system = install.System(root=Path(work.name))
        self.commands = []
        self.args = argparse.Namespace(vmid=101, purge=False, keep_channel=False)
        self.status = "hwmon-virt/0.1.0, kernel-a, x86_64: installed\n"
        self.unit_lists = {"list-units": "", "list-unit-files": ""}
        self.failure = None
        self.original = "name: Example\n\n[snapshot]\nargs: -no-reboot\n"
        self.qm_config = "args: " + install.merge_args("-smbios 'type=0,vendor=Pending Host'", 101) + "\n"
        self.put("/etc/pve/qemu-server/101.conf", self.original)
        self.system.path("/run/systemd/system").mkdir(parents=True)
        self.output = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.output))
        stack.enter_context(mock.patch.object(install.shutil, "which", side_effect=lambda cmd: cmd))
        stack.enter_context(mock.patch.object(install.subprocess, "run", side_effect=self.run_command))

    def put(self, name, content="fixture\n"):
        path = self.system.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def host(self):
        self.put("/etc/systemd/system/hwmon-virt-host@.service")
        self.put("/etc/hwmon-virt/host-101.json", '{"sensors": ["coretemp:Package id 0"]}\n')
        self.put(install.PROGRAM)

    def guest(self):
        self.put("/etc/systemd/system/hwmon-virt-guest.service")
        self.put("/etc/modprobe.d/hwmon-virt.conf", "options hwmon_virt cache_ms=200\n")
        self.put("/var/lib/hwmon-virt/hwmon-virt-0.1.0-kernel-a.sha256")
        self.system.path("/sys/module/hwmon_virt").mkdir(parents=True)
        self.put(install.PROGRAM)
        for filename in install.MODULE_FILES:
            self.put("/usr/src/hwmon-virt-0.1.0/" + filename)

    def snapshot(self):
        return {str(p.relative_to(self.system.root)): p.read_bytes()
                for p in self.system.root.rglob("*") if p.is_file()}

    def run_command(self, command, **_kwargs):
        self.commands.append(command)
        if self.failure and command[:len(self.failure)] == self.failure:
            raise subprocess.CalledProcessError(1, command, stderr="fixture failure")
        output = ""
        if command[:2] == ("systemctl", "show"):
            unit = command[2]
            if "@" in unit:
                unit = unit.split("@", 1)[0] + "@.service"
            exists = self.system.path("/etc/systemd/system/" + unit).exists()
            output = "LoadState=" + ("loaded" if exists else "not-found") + "\nActiveState=inactive\n"
        elif command[0] == "systemctl" and command[1] in self.unit_lists:
            if command[1] == "list-unit-files" and any("*" in arg for arg in command):
                raise subprocess.CalledProcessError(1, command, stdout="", stderr="")
            output = self.unit_lists[command[1]]
        elif command == ("dkms", "status"):
            output = self.status
        elif command[:2] == ("dkms", "remove"):
            version = command[command.index("-v") + 1]
            self.status = "\n".join(line for line in self.status.splitlines()
                                    if not line.startswith("hwmon-virt/" + version))
        elif command[:2] == ("modprobe", "-r"):
            self.system.path("/sys/module/" + command[2]).rmdir()
        elif command[:2] == ("qm", "config"):
            output = self.qm_config
        elif command[:2] == ("qm", "set"):
            digest = hashlib.sha1(self.system.path("/etc/pve/qemu-server/101.conf").read_bytes()).hexdigest()
            self.assertEqual(command[command.index("--digest") + 1], digest)
            self.qm_config = "name: Example\n"
            if "--args" in command:
                self.qm_config += "args: " + command[-1] + "\n"
            self.put("/etc/pve/qemu-server/101.conf", self.qm_config)
        return subprocess.CompletedProcess(command, 0, output, "")

    def test_host_removes_only_channel_and_keeps_config_and_raw_backup(self):
        self.host()
        uninstall.uninstall_host(self.system, self.args)
        update = next(cmd for cmd in self.commands if cmd[:2] == ("qm", "set"))
        self.assertEqual(shlex.split(update[-1]), ["-smbios", "type=0,vendor=Pending Host"])
        backup = next(self.system.path("/var/backups/hwmon-virt").glob("101.conf.bak.*"))
        self.assertEqual(backup.read_text(), self.original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.system.path("/etc/hwmon-virt/host-101.json").exists())
        self.assertFalse(self.system.path(install.PROGRAM).exists())
        self.assertIn(("systemctl", "disable", "hwmon-virt-host@101.service"), self.commands)
        self.assertFalse(any(cmd[:2] in (("qm", "stop"), ("qm", "start"), ("qm", "shutdown"))
                             for cmd in self.commands))

    def test_host_deletes_empty_args_property_and_purges_selected_config(self):
        self.host()
        self.qm_config = "args: " + install.merge_args("", 101) + "\n"
        self.args.purge = True
        uninstall.uninstall_host(self.system, self.args)
        update = next(cmd for cmd in self.commands if cmd[:2] == ("qm", "set"))
        self.assertEqual(update[-2:], ("--delete", "args"))
        self.assertFalse(self.system.path("/etc/hwmon-virt/host-101.json").exists())

    def test_keep_channel_never_reads_or_changes_vm_config(self):
        self.host()
        self.args.keep_channel = True
        uninstall.uninstall_host(self.system, self.args)
        self.assertFalse(any(cmd[0] == "qm" for cmd in self.commands))
        self.assertEqual(self.system.path("/etc/pve/qemu-server/101.conf").read_text(), self.original)

    def test_other_vm_configuration_keeps_shared_files(self):
        self.host()
        other = self.put("/etc/hwmon-virt/host-102.json", "other configuration\n")
        self.args.purge = True
        uninstall.uninstall_host(self.system, self.args)
        self.assertTrue(self.system.path(install.PROGRAM).exists())
        self.assertTrue(self.system.path("/etc/systemd/system/hwmon-virt-host@.service").exists())
        self.assertEqual(other.read_text(), "other configuration\n")
        self.assertFalse(any("hwmon-virt-host@102.service" in cmd for cmd in self.commands))

    def test_loaded_or_enabled_other_vm_keeps_shared_files_without_config(self):
        for command in self.unit_lists:
            with self.subTest(command=command):
                self.host()
                self.unit_lists[command] = "hwmon-virt-host@102.service enabled enabled\n"
                uninstall.uninstall_host(self.system, self.args)
                self.assertTrue(self.system.path(install.PROGRAM).exists())
                self.unit_lists[command] = ""

    def test_host_on_nested_guest_keeps_shared_program(self):
        self.host()
        self.guest()
        uninstall.uninstall_host(self.system, self.args)
        self.assertTrue(self.system.path(install.PROGRAM).exists())

    def test_host_rejects_partial_duplicate_customized_or_locked_config_before_changes(self):
        self.host()
        channel = install.merge_args("", 101)
        for config in ("args: -device virtio-serial-pci,id=hmvserial\n", "lock: backup\n",
                       "template: 1\n", "args: " + channel + " " + channel + "\n",
                       "args: " + channel.replace("101.hwmon", "102.hwmon") + "\n"):
            before = self.snapshot()
            self.qm_config = config
            self.commands.clear()
            with self.assertRaises(ValueError):
                uninstall.uninstall_host(self.system, self.args)
            self.assertEqual(self.snapshot(), before)
            self.assertFalse(any(cmd[:2] == ("systemctl", "stop") for cmd in self.commands))

    def test_failed_qm_update_leaves_services_and_files_intact(self):
        self.host()
        self.failure = ("qm", "set")
        with self.assertRaises(subprocess.CalledProcessError):
            uninstall.uninstall_host(self.system, self.args)
        self.assertTrue(self.system.path(install.PROGRAM).exists())
        self.assertFalse(any(cmd[:2] == ("systemctl", "stop") for cmd in self.commands))

    def test_deleted_vm_can_still_remove_service(self):
        self.host()
        self.system.path("/etc/pve/qemu-server/101.conf").unlink()
        uninstall.uninstall_host(self.system, self.args)
        self.assertFalse(any(cmd[0] == "qm" for cmd in self.commands))
        self.assertFalse(self.system.path(install.PROGRAM).exists())

    def test_guest_removes_all_registered_versions_but_no_unrelated_module(self):
        self.guest()
        self.status += ("hwmon-virt/0.1.0, kernel-b, x86_64: installed\n"
                        "hwmon-virt/0.2.0: added\nother/1.0, kernel-a: installed\n")
        unrelated = self.put("/usr/src/other-1.0/source.c")
        uninstall.uninstall_guest(self.system, self.args)
        removes = [cmd for cmd in self.commands if cmd[:2] == ("dkms", "remove")]
        self.assertEqual(removes, [("dkms", "remove", "-m", "hwmon-virt", "-v", v, "--all")
                                  for v in ("0.1.0", "0.2.0")])
        self.assertTrue(unrelated.exists())
        self.assertFalse(self.system.path("/usr/src/hwmon-virt-0.1.0").exists())
        self.assertFalse(self.system.path("/var/lib/hwmon-virt").exists())
        self.assertTrue(self.system.path("/etc/modprobe.d/hwmon-virt.conf").exists())

    def test_guest_purge_keeps_unknown_files_backups_and_source_checkout(self):
        self.guest()
        backup = self.put("/usr/src/hwmon-virt-0.1.0/Makefile.bak.saved")
        unknown = self.put("/usr/src/hwmon-virt-0.1.0/my-notes.txt")
        self.args.purge = True
        uninstall.uninstall_guest(self.system, self.args)
        self.assertTrue(backup.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue((install.PROJECT / "hwmon_virt.c").exists())
        self.assertFalse(self.system.path("/etc/modprobe.d/hwmon-virt.conf").exists())

    def test_busy_guest_module_stops_before_disabling_or_removing_any_files(self):
        self.guest()
        before = self.snapshot()
        self.failure = ("modprobe", "-r")
        with self.assertRaises(subprocess.CalledProcessError):
            uninstall.uninstall_guest(self.system, self.args)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(cmd[:2] in (("systemctl", "disable"), ("dkms", "remove"))
                             for cmd in self.commands))

    def test_failed_dkms_removal_retains_sources_and_service_file_for_retry(self):
        self.guest()
        before = self.snapshot()
        self.failure = ("dkms", "remove")
        with self.assertRaises(subprocess.CalledProcessError):
            uninstall.uninstall_guest(self.system, self.args)
        self.assertEqual(self.snapshot(), before)

    def test_failed_stop_does_not_remove_files_or_unload_module(self):
        self.guest()
        before = self.snapshot()
        self.failure = ("systemctl", "stop")
        with self.assertRaises(subprocess.CalledProcessError):
            uninstall.uninstall_guest(self.system, self.args)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(cmd[0] == "modprobe" for cmd in self.commands))

    def test_guest_with_host_consumers_keeps_shared_program(self):
        self.guest()
        self.host()
        uninstall.uninstall_guest(self.system, self.args)
        self.assertTrue(self.system.path(install.PROGRAM).exists())
        self.assertTrue(self.system.path("/etc/systemd/system/hwmon-virt-host@.service").exists())

    def test_dry_runs_do_not_change_files_or_execute_mutations(self):
        self.guest()
        self.host()
        before = self.snapshot()
        self.system.dry_run = True
        self.args.purge = True
        uninstall.uninstall_guest(self.system, self.args)
        uninstall.uninstall_host(self.system, self.args)
        self.assertEqual(self.snapshot(), before)
        allowed = {("systemctl", "show"), ("systemctl", "list-units"),
                   ("systemctl", "list-unit-files"), ("qm", "config"), ("dkms", "status")}
        self.assertTrue(all(cmd[:2] in allowed for cmd in self.commands))

    def test_repeat_runs_and_absent_installations_succeed(self):
        self.host()
        self.guest()
        self.args.purge = True
        for _ in range(2):
            uninstall.uninstall_guest(self.system, self.args)
            uninstall.uninstall_host(self.system, self.args)
        self.assertEqual(sum(cmd[:2] == ("modprobe", "-r") for cmd in self.commands), 1)
        self.assertEqual(sum(cmd[:2] == ("qm", "set") for cmd in self.commands), 1)

    def test_dkms_status_understands_older_format_and_rejects_malformed_own_records(self):
        self.status = "hwmon-virt, 0.1.0, kernel-a, x86_64: installed\nother/1: added\n"
        self.assertEqual(uninstall.installed_versions(self.system, "hwmon-virt"), ["0.1.0"])
        self.status = "hwmon-virt/../bad: added\n"
        with self.assertRaisesRegex(ValueError, "unrecognized DKMS"):
            uninstall.installed_versions(self.system, "hwmon-virt")

    def test_symlinked_source_tree_is_rejected_before_stopping_guest(self):
        self.guest()
        target = self.system.path("/usr/src/hwmon-virt-0.1.0")
        self.system.path("/usr/src/hwmon-virt-0.2.0").symlink_to(target, target_is_directory=True)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "symlink"):
            uninstall.uninstall_guest(self.system, self.args)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(any(cmd[:2] == ("systemctl", "stop") for cmd in self.commands))

    def test_symlinked_program_is_rejected_before_editing_vm(self):
        self.host()
        program = self.system.path(install.PROGRAM)
        program.unlink()
        program.symlink_to(self.put("/usr/local/libexec/something-else"))
        with self.assertRaisesRegex(ValueError, "symlink"):
            uninstall.uninstall_host(self.system, self.args)
        self.assertFalse(any(cmd[:2] == ("qm", "set") for cmd in self.commands))


if __name__ == "__main__":
    unittest.main()
