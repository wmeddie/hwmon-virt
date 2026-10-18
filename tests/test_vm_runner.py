# SPDX-License-Identifier: GPL-2.0-only
import contextlib
import io
import sys
import unittest
from unittest import mock

import run_vm


class RunnerTests(unittest.TestCase):
    def test_missing_tools_have_clear_errors_before_setup(self):
        for missing in ("busybox", "sensors", "cpio", "qemu-system-x86_64", "ldd"):
            with self.subTest(missing=missing):
                error = io.StringIO()
                with mock.patch.object(run_vm.shutil, "which",
                                       side_effect=lambda name: None if name == missing else "/bin/" + name), \
                     mock.patch.object(sys, "argv", ["run_vm.py", "/unused/kernel"]), \
                     contextlib.redirect_stderr(error), \
                     self.assertRaises(SystemExit) as caught:
                    run_vm.main()
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("missing required test tools: " + missing, error.getvalue())
