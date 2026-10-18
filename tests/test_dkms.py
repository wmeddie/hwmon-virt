# SPDX-License-Identifier: GPL-2.0-only
"""Opt-in build test using real DKMS with temporary source/build directories."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.environ.get("K10TEMP_VIRT_DKMS_TESTS") == "1",
                     "set K10TEMP_VIRT_DKMS_TESTS=1 to run a real isolated DKMS build")
class DKMSBuildTests(unittest.TestCase):
    def test_installer_sources_build_through_dkms(self):
        from scripts.install import MODULE_FILES, PROJECT

        self.assertNotEqual(os.geteuid(), 0, "run the isolated build test without sudo")
        kernel = os.uname().release
        headers = Path("/lib/modules") / kernel / "build"
        package = dict(re.findall(r'^PACKAGE_(NAME|VERSION)="([A-Za-z0-9_.+-]+)"$',
                                  (PROJECT / "dkms.conf").read_text(), re.MULTILINE))
        with tempfile.TemporaryDirectory(prefix="ktv-dkms-") as directory:
            root = Path(directory)
            source = root / "src" / f"{package['NAME']}-{package['VERSION']}"
            for filename in MODULE_FILES:
                target = source / filename
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(PROJECT / filename, target)
            (root / "dkms").mkdir()
            (root / "modules").mkdir()
            # Keep compiler/config detection, but omit signing in this build-only
            # test so DKMS never accesses or generates system signing keys.
            config = headers / "include/config/auto.conf"
            if not config.exists():
                config = headers / ".config"
            test_config = root / "kernel.config"
            test_config.write_text("\n".join(line for line in config.read_text().splitlines()
                                           if not line.startswith("CONFIG_MODULE_SIG_HASH=")) + "\n")
            command = ["dkms", "build", "-m", package["NAME"], "-v", package["VERSION"],
                       "-k", kernel, "--force", "--dkmstree", str(root / "dkms"),
                       "--sourcetree", str(root / "src"), "--installtree", str(root / "modules"),
                       "--kernelsourcedir", str(headers), "--config", str(test_config)]
            result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=180)
            logs = "\n".join(path.read_text() for path in (root / "dkms").rglob("make.log"))
            self.assertEqual(result.returncode, 0, result.stdout + "\n" + logs)
            modules = list((root / "dkms" / package["NAME"] / package["VERSION"] / kernel)
                           .glob("*/module/k10temp_virt.ko*"))
            self.assertEqual(len(modules), 1, result.stdout)
            name = subprocess.check_output(["modinfo", "-F", "name", str(modules[0])], text=True)
            vermagic = subprocess.check_output(["modinfo", "-F", "vermagic", str(modules[0])], text=True)
            self.assertEqual(name.strip(), "k10temp_virt")
            self.assertEqual(vermagic.split()[0], kernel)


if __name__ == "__main__":
    unittest.main()
