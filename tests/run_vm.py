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
import selectors
import shutil
import subprocess
import sys
import tempfile
import time


def required_tools():
    names = ("busybox", "cpio", "sensors", "qemu-system-x86_64", "ldd")
    found = {name: shutil.which(name) for name in names}
    missing = [name for name, path in found.items() if path is None]
    if missing:
        raise ValueError("missing required test tools: " + ", ".join(missing))
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kernel", type=Path)
    args = parser.parse_args()
    try:
        binaries = required_tools()
    except ValueError as exc:
        parser.error(str(exc))
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

        copy(binaries["busybox"], "/bin/busybox")
        for name in ("sh", "mount", "insmod", "rmmod", "poweroff", "sleep"):
            (root / "bin" / name).symlink_to("busybox")
        executable = Path(sys.executable).resolve()
        copy(executable, "/usr/bin/python3")
        sensors = Path(binaries["sensors"])
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
            if test.name in ("test_install.py", "test_vm_runner.py"):
                continue  # Host-side tool/installer fixtures run via make test.
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
        if rmmod k10temp_virt && insmod /k10temp_virt.ko cache_ms=100 labels=Host_CPU,Second; then
            K10TEMP_VIRT_TRANSPORT_TESTS=1 python3 -m unittest discover -s tests -p test_transport.py -v
            transport_result=$?
            if rmmod k10temp_virt && [ "$transport_result" -eq 0 ]; then
                echo KTV_VM_TESTS_PASSED
            fi
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
        host = None
        host_logs = []

        def start_host():
            nonlocal host
            if host is not None:
                raise RuntimeError("test host is already running")
            host = subprocess.Popen([sys.executable, str(project / "k10temp_virt.py"),
                                     "host", "--socket", str(work / "host.sock"),
                                     "--sensor", str(work / "temp1"), "--sensor", str(work / "temp2")],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        def stop_host():
            nonlocal host
            if host is not None:
                host.terminate()
                try:
                    _, log = host.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    host.kill()
                    _, log = host.communicate()
                host_logs.append(log)
                host = None

        output = bytearray()
        vm = None
        try:
            start_host()
            vm = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            # The disposable guest emits fixed control markers on its serial
            # console to stop/restart only this runner's fixture host process.
            with selectors.DefaultSelector() as selector:
                selector.register(vm.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 180
                pending = b""
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError("isolated VM tests exceeded 180 seconds")
                    data = os.read(vm.stdout.fileno(), 65536)
                    if not data:
                        break
                    output.extend(data)
                    pending += data
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if line.strip() == b"KTV_STOP_HOST":
                            stop_host()
                        elif line.strip() == b"KTV_START_HOST":
                            start_host()
            result = vm.wait(timeout=5)
        finally:
            if vm is not None:
                if vm.poll() is None:
                    vm.kill()
                vm.wait()
                vm.stdout.close()
            stop_host()
            print("".join(host_logs))
            print(output.decode(errors="replace"))
        if result or b"KTV_VM_TESTS_PASSED" not in output:
            raise SystemExit("isolated VM tests failed")


if __name__ == "__main__":
    main()
