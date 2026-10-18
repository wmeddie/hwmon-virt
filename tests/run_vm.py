#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build a disposable initramfs and run real module tests under QEMU/TCG.

Usage: python3 tests/run_vm.py /path/to/readable/matching/vmlinuz
Requires Linux, QEMU, static busybox, cpio, sensors and a built k10temp_virt.ko.
Only writes a temporary build directory; never installs/loads anything on the host.
"""
import argparse
import gzip
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel", type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="ktv-vm-") as work:
        work = Path(work)
        root = work / "root"
        root.mkdir()

        def copy(source, target=None):
            source = Path(source)
            dest = root / str(target or source).lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)

        copy(shutil.which("busybox"), "/bin/busybox")
        for name in ("sh", "mount", "insmod", "rmmod", "poweroff", "sleep"):
            (root / "bin" / name).symlink_to("busybox")
        executable = Path(sys.executable).resolve()
        copy(executable, "/usr/bin/python3")
        sensors = Path(shutil.which("sensors"))
        copy(sensors, "/usr/bin/sensors")
        if Path("/etc/sensors3.conf").exists():
            copy("/etc/sensors3.conf")
        stdlib = Path(os.__file__).parent
        shutil.copytree(stdlib, root / str(stdlib).lstrip("/"),
                        ignore=shutil.ignore_patterns("__pycache__", "site-packages", "test",
                                                     "tests", "idlelib", "tkinter", "ensurepip"))
        libraries = set()
        for binary in [executable, sensors, *stdlib.glob("lib-dynload/*.so")]:
            output = subprocess.check_output(["ldd", str(binary)], text=True)
            libraries.update(re.findall(r"(?:=>\s+|^\s*)(/\S+)", output, re.MULTILINE))
        for library in libraries:
            copy(library)
        copy(project / "k10temp_virt.ko", "/k10temp_virt.ko")
        copy(project / "k10temp_virt.py", "/project/k10temp_virt.py")
        for test in (project / "tests").glob("test_*.py"):
            if test.name == "test_install.py":
                continue  # Installer fixtures run via make test, outside the initramfs.
            copy(test, "/project/tests/" + test.name)
        for directory in ("proc", "sys", "dev", "tmp", "run"):
            (root / directory).mkdir(exist_ok=True)
        (root / "init").write_text("""#!/bin/sh
export PATH=/bin:/usr/bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
cd /project
if insmod /k10temp_virt.ko cache_ms=500 timeout_ms=250 labels=Host_CPU,Second; then
    if K10TEMP_VIRT_KERNEL_TESTS=1 python3 -m unittest discover -s tests -v; then
        python3 k10temp_virt.py guest --port /dev/vport0p1 --timeout 0.15 &
        bridge_pid=$!
        sleep 1
        K10TEMP_VIRT_TRANSPORT_TESTS=1 python3 -m unittest discover -s tests -p test_transport.py -v
        transport_result=$?
        kill "$bridge_pid"
        wait "$bridge_pid"
        if rmmod k10temp_virt && [ "$transport_result" -eq 0 ]; then
            echo KTV_VM_TESTS_PASSED
        fi
    fi
fi
poweroff -f
""")
        (root / "init").chmod(0o755)
        names = [".", *sorted(str(p.relative_to(root)) for p in root.rglob("*"))]
        archive = subprocess.run(["cpio", "-o", "-H", "newc", "--null", "--owner=0:0"],
                                 cwd=root, input=("\0".join(names) + "\0").encode(),
                                 stdout=subprocess.PIPE, check=True).stdout
        initrd = work / "initrd.gz"
        with gzip.open(initrd, "wb", compresslevel=1) as output:
            output.write(archive)
        command = ["qemu-system-x86_64", "-accel", "tcg", "-m", "768", "-smp", "2",
                   "-nodefaults", "-display", "none", "-serial", "stdio", "-no-reboot",
                   "-kernel", str(args.kernel.resolve()), "-initrd", str(initrd),
                   "-append", "console=ttyS0 rdinit=/init panic=-1 quiet",
                   "-device", "virtio-serial-pci,id=ktvserial",
                   "-chardev", f"socket,id=ktvchar,path={work}/host.sock,server=on,wait=off",
                   "-device", "virtserialport,bus=ktvserial.0,nr=1,chardev=ktvchar,name=org.k10temp-virt.0"]
        (work / "temp1").write_text("42625\n")
        (work / "temp2").write_text("-5000\n")
        host = subprocess.Popen([sys.executable, str(project / "k10temp_virt.py"),
                                 "host", "--socket", str(work / "host.sock"),
                                 "--sensor", str(work / "temp1"), "--sensor", str(work / "temp2")],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=180, text=True)
        finally:
            host.terminate()
            _, host_log = host.communicate(timeout=5)
            print(host_log)
        print(result.stdout)
        if result.returncode or "KTV_VM_TESTS_PASSED" not in result.stdout:
            raise SystemExit("isolated VM tests failed")


if __name__ == "__main__":
    main()
