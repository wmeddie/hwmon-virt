# SPDX-License-Identifier: GPL-2.0-only
"""Deterministic C tests for the module's completion policy; no kernel hooks."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class CompletionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cc"), "requires a C compiler")
    def test_completion_timeout_and_interruption(self):
        with tempfile.TemporaryDirectory(prefix="hmv-logic-") as directory:
            binary = str(Path(directory) / "kernel-logic")
            subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                            str(Path(__file__).with_name("kernel_logic.c")), "-o", binary], check=True)
            subprocess.run([binary], check=True)
