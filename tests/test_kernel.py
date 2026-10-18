# SPDX-License-Identifier: GPL-2.0-only
"""Opt-in tests for a disposable VM, never load a module in the caller's kernel."""
import errno
import os
from pathlib import Path
import queue
import select
import stat
import subprocess
import threading
import time
import unittest

import hwmon_virt as kv


@unittest.skipUnless(os.environ.get("K10TEMP_VIRT_KERNEL_TESTS") == "1",
                     "requires the disposable test VM")
class KernelTests(unittest.TestCase):
    def setUp(self):
        self.hwmon = next(p for p in Path("/sys/class/hwmon").glob("hwmon*")
                          if "k10temp-virt" in str(p.resolve()))
        self.fd = os.open("/dev/k10temp-virt", os.O_RDWR | os.O_NONBLOCK)
        self.stream = kv.FramedFD(self.fd)

    def tearDown(self):
        if self.fd is not None:
            os.close(self.fd)

    def read_async(self, channel=1):
        result = queue.Queue()

        def read():
            try:
                result.put(int((self.hwmon / f"temp{channel}_input").read_text()))
            except OSError as exc:
                result.put(exc)

        thread = threading.Thread(target=read)
        thread.start()
        self.addCleanup(thread.join, 2)
        return result

    def respond(self, value=52000, status=0):
        request = self.stream.receive(time.monotonic() + 1)
        reply = kv.Frame(request.sequence, request.channel, status, value)
        self.stream.send(reply, time.monotonic() + 1)
        return request

    def test_exclusive_bridge_and_permissions(self):
        with self.assertRaises(OSError) as caught:
            os.open("/dev/k10temp-virt", os.O_RDWR)
        self.assertEqual(caught.exception.errno, errno.EBUSY)
        self.assertEqual(stat.S_IMODE(os.stat("/dev/k10temp-virt").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.hwmon / "temp1_input").stat().st_mode), 0o444)
        self.assertEqual((self.hwmon / "name").read_text().strip(), "k10temp")

    def test_idle_and_label_reads_do_not_request(self):
        self.assertEqual((self.hwmon / "temp1_label").read_text().strip(), "Host_CPU")
        self.assertEqual(select.select([self.fd], [], [], 0.03)[0], [])

    def test_cache_hit_expiry_and_separate_channels(self):
        result = self.read_async()
        self.respond()
        self.assertEqual(result.get(timeout=2), 52000)
        self.assertEqual(int((self.hwmon / "temp1_input").read_text()), 52000)
        self.assertEqual(select.select([self.fd], [], [], 0)[0], [])
        result = self.read_async(2)
        self.assertEqual(self.respond(61000).channel, 1)
        self.assertEqual(result.get(timeout=2), 61000)
        time.sleep(0.55)
        result = self.read_async()
        self.respond(53000)
        self.assertEqual(result.get(timeout=2), 53000)

    def test_concurrent_readers_share_cache(self):
        results = [self.read_async() for _ in range(4)]
        self.respond(43000)
        self.assertEqual([r.get(timeout=2) for r in results], [43000] * 4)
        self.assertEqual(select.select([self.fd], [], [], 0)[0], [])

    def test_timeout_and_late_reply_rejection(self):
        result = self.read_async()
        request = self.stream.receive(time.monotonic() + 1)
        error = result.get(timeout=2)
        self.assertIsInstance(error, OSError)
        self.assertEqual(error.errno, errno.ETIMEDOUT)
        with self.assertRaises(OSError) as caught:
            os.write(self.fd, kv.Frame(request.sequence, 0, value=99000).pack())
        self.assertEqual(caught.exception.errno, errno.ESTALE)
        result = self.read_async()
        self.respond(44000)
        self.assertEqual(result.get(timeout=2), 44000)

    def test_wrong_id_channel_and_invalid_values(self):
        result = self.read_async()
        request = self.stream.receive(time.monotonic() + 1)
        for reply, expected in (
            (kv.Frame(request.sequence + 1, 0, value=99000), errno.ESTALE),
            (kv.Frame(request.sequence, 1, value=99000), errno.ESTALE),
            (kv.Frame(request.sequence, 0, value=1000001), errno.EINVAL),
            (kv.Frame(request.sequence, 0, status=1), errno.EINVAL),
        ):
            with self.assertRaises(OSError) as caught:
                os.write(self.fd, reply.pack())
            self.assertEqual(caught.exception.errno, expected)
        os.write(self.fd, kv.Frame(request.sequence, 0, value=-5000).pack())
        self.assertEqual(result.get(timeout=2), -5000)

    def test_disconnect_wakes_reader_and_reopen_invalidates_cache(self):
        result = self.read_async()
        self.stream.receive(time.monotonic() + 1)
        os.close(self.fd)
        self.fd = None
        error = result.get(timeout=2)
        self.assertEqual(error.errno, errno.ENOTCONN)
        with self.assertRaises(OSError) as caught:
            (self.hwmon / "temp1_input").read_text()
        self.assertEqual(caught.exception.errno, errno.ENODATA)
        self.fd = os.open("/dev/k10temp-virt", os.O_RDWR | os.O_NONBLOCK)
        self.stream = kv.FramedFD(self.fd)
        result = self.read_async()
        self.respond(45000)
        self.assertEqual(result.get(timeout=2), 45000)

    def test_error_is_not_cached(self):
        result = self.read_async()
        self.respond(status=-errno.ENODATA)
        self.assertEqual(result.get(timeout=2).errno, errno.ENODATA)
        result = self.read_async()
        self.respond(46000)
        self.assertEqual(result.get(timeout=2), 46000)

    def test_peer_errors_are_filtered_at_the_kernel_boundary(self):
        for status in (-512, -513, -514, -516, -517, -529, -errno.EACCES, -4095):
            with self.subTest(status=status):
                result = self.read_async()
                self.respond(status=status)
                self.assertEqual(result.get(timeout=2).errno, errno.EIO)
        for status in (-errno.EIO, -errno.ENODATA, -errno.EPROTO, -errno.ETIMEDOUT, -errno.ENOTCONN):
            result = self.read_async()
            self.respond(status=status)
            self.assertEqual(result.get(timeout=2).errno, -status)


@unittest.skipUnless(os.environ.get("K10TEMP_VIRT_KERNEL_TESTS") == "1",
                     "requires the disposable test VM")
class ModuleParameterTests(unittest.TestCase):
    def setUp(self):
        subprocess.run(["rmmod", "k10temp_virt"], check=True)

    def tearDown(self):
        if Path("/sys/module/k10temp_virt").exists():
            subprocess.run(["rmmod", "k10temp_virt"], check=True)
        subprocess.run(["insmod", "/k10temp_virt.ko", "cache_ms=500", "timeout_ms=250",
                        "labels=Host_CPU,Second"], check=True)

    def test_invalid_names_fail_without_leaving_a_device(self):
        for name in ("", "host-temp", "host*temp", "host temp", "host\ttemp", "host\ntemp",
                     "host\rtemp", "host\vtemp", "host\ftemp"):
            with self.subTest(name=name):
                result = subprocess.run(["insmod", "/k10temp_virt.ko", f'sensor_name="{name}"'],
                                        capture_output=True, text=True)
                try:
                    self.assertNotEqual(result.returncode, 0, "invalid name was accepted")
                    self.assertFalse(Path("/dev/k10temp-virt").exists())
                finally:
                    if Path("/sys/module/k10temp_virt").exists():
                        subprocess.run(["rmmod", "k10temp_virt"], check=True)

    def test_valid_custom_name_loads(self):
        subprocess.run(["insmod", "/k10temp_virt.ko", "sensor_name=host_temp1"], check=True)
        names = [p.read_text().strip() for p in Path("/sys/class/hwmon").glob("hwmon*/name")]
        self.assertIn("host_temp1", names)


if __name__ == "__main__":
    unittest.main()
