# SPDX-License-Identifier: GPL-2.0-only
"""Full VM -> virtio-serial -> host -> hwmon tests using known fixture values."""
import json
import os
from pathlib import Path
import subprocess
import unittest


@unittest.skipUnless(os.environ.get("K10TEMP_VIRT_TRANSPORT_TESTS") == "1",
                     "requires the disposable VM and real virtio-serial channel")
class TransportTests(unittest.TestCase):
    def test_native_hwmon_reads(self):
        hwmon = next(p for p in Path("/sys/class/hwmon").glob("hwmon*")
                     if "k10temp-virt" in str(p.resolve()))
        self.assertEqual(int((hwmon / "temp1_input").read_text()), 42625)
        self.assertEqual(int((hwmon / "temp2_input").read_text()), -5000)

    def test_real_lm_sensors_discovers_device(self):
        data = json.loads(subprocess.check_output(["sensors", "-j"], text=True))
        chip = next(value for name, value in data.items() if name.startswith("k10temp-"))
        readings = {key: value for group in chip.values() if isinstance(group, dict)
                    for key, value in group.items() if key.endswith("_input")}
        self.assertEqual(readings["temp1_input"], 42.625)
        self.assertEqual(readings["temp2_input"], -5.0)


if __name__ == "__main__":
    unittest.main()
