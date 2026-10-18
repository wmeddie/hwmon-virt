# SPDX-License-Identifier: GPL-2.0-only
"""Full VM -> virtio-serial -> host -> hwmon tests using known fixture values."""
import errno
import json
import os
from pathlib import Path
import subprocess
import select
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.environ.get("K10TEMP_VIRT_TRANSPORT_TESTS") == "1",
                     "requires the disposable VM and real virtio-serial channel")
class TransportTests(unittest.TestCase):
    def setUp(self):
        self.hwmon = next(p for p in Path("/sys/class/hwmon").glob("hwmon*")
                          if "k10temp-virt" in str(p.resolve()))
        self.bridge = None
        self.log = tempfile.TemporaryFile(mode="w+")
        self.addCleanup(self.log.close)
        self.addCleanup(self.stop_bridge)
        self.start_bridge()

    def start_bridge(self):
        program = Path(__file__).resolve().parent.parent / "k10temp_virt.py"
        self.bridge = subprocess.Popen([sys.executable, str(program), "guest", "--port", "/dev/vport0p1"],
                                       stdout=subprocess.DEVNULL, stderr=self.log)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.assertIsNone(self.bridge.poll(), "guest bridge exited")
            try:
                fd = os.open("/dev/k10temp-virt", os.O_RDWR | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno == errno.EBUSY:
                    return  # bridge owns both devices and is ready for reads
                raise
            else:
                os.close(fd)
            time.sleep(0.02)
        self.fail("guest bridge did not open the kernel device")

    def stop_bridge(self):
        if self.bridge is not None:
            self.bridge.terminate()
            try:
                self.bridge.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.bridge.kill()
                self.bridge.wait()
            self.bridge = None

    def test_host_disconnect_fails_fast_and_recovers(self):
        self.assertEqual(int((self.hwmon / "temp1_input").read_text()), 42625)
        self.stop_bridge()
        print("\nKTV_STOP_HOST", flush=True)
        try:
            # Synchronize with the real virtio host-close event, not a guessed sleep.
            fd = os.open("/dev/vport0p1", os.O_RDWR | os.O_NONBLOCK)
            try:
                poller = select.poll()
                poller.register(fd, 0)
                self.assertTrue(any(flags & select.POLLHUP for _, flags in poller.poll(5000)))
            finally:
                os.close(fd)
            self.start_bridge()  # opens successfully even though the host is absent
            durations = []
            for attempt in range(3):
                if attempt:
                    time.sleep(2.1)  # exercise repeated reconnects during one outage
                for channel in (1, 2):
                    start = time.monotonic()
                    with self.assertRaises(OSError) as caught:
                        (self.hwmon / f"temp{channel}_input").read_text()
                    durations.append(time.monotonic() - start)
                    self.assertIn(caught.exception.errno, (errno.ENOTCONN, errno.ENODATA))
                    self.assertLess(durations[-1], 0.3, "read stalled at the 0.5-second bridge timeout")
            self.log.seek(0)
            self.assertEqual(self.log.read().count("WARNING bridge unavailable:"), 1)
            print(f"Disconnected read maximum: {max(durations):.4f}s", flush=True)
        finally:
            print("\nKTV_START_HOST", flush=True)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            try:
                value = int((self.hwmon / "temp1_input").read_text())
            except OSError:
                time.sleep(0.05)
                continue
            self.assertEqual(value, 42625)
            break
        else:
            self.fail("readings did not recover after the real host responder restarted")

    def test_native_hwmon_reads(self):
        self.assertEqual(int((self.hwmon / "temp1_input").read_text()), 42625)
        self.assertEqual(int((self.hwmon / "temp2_input").read_text()), -5000)

    def test_real_lm_sensors_discovers_device(self):
        data = json.loads(subprocess.check_output(["sensors", "-j"], text=True))
        chip = next(value for name, value in data.items() if name.startswith("k10temp-"))
        readings = {key: value for group in chip.values() if isinstance(group, dict)
                    for key, value in group.items() if key.endswith("_input")}
        self.assertEqual(readings["temp1_input"], 42.625)
        self.assertEqual(readings["temp2_input"], -5.0)


if __name__ == "__main__":
    unittest.main()
