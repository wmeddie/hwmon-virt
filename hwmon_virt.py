#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""hwmon-virt: on-demand host/guest temperatures. Python standard library only."""

import argparse
import contextlib
import errno
import json
import logging
import math
import os
from pathlib import Path
import re
import select
import signal
import socket
import struct
import time

LOG = logging.getLogger("hwmon-virt")
VERSION = "0.1.0"
MAGIC = b"HMV1"
PACKET = struct.Struct("<4sQIii")
MIN_TEMP, MAX_TEMP = -273150, 1000000
MAX_CHANNELS = 16
REPLY_STATUSES = {0, -errno.EIO, -errno.ENODATA, -errno.EPROTO, -errno.ETIMEDOUT, -errno.ENOTCONN}


class ConnectionLog:
    """Report transitions after communication, not merely after opening an fd."""

    def __init__(self):
        self.online = None

    def connected(self):
        if self.online is not True:
            LOG.info("bridge communication established")
        self.online = True

    def disconnected(self, error):
        log = LOG.debug if self.online is False else LOG.warning
        log("bridge unavailable: %s; retrying in 2 seconds", error)
        self.online = False


class Frame:
    def __init__(self, sequence, channel, status=0, value=0):
        self.sequence = sequence
        self.channel = channel
        self.status = status
        self.value = value

    def pack(self):
        return PACKET.pack(MAGIC, self.sequence, self.channel, self.status, self.value)

    @classmethod
    def unpack(cls, data):
        magic, sequence, channel, status, value = PACKET.unpack(data)
        if magic != MAGIC:
            raise ValueError("invalid protocol magic/version")
        return cls(sequence, channel, status, value)


class FramedFD:
    """Fixed-size frames with stream fragmentation and reconnect resynchronization."""

    def __init__(self, fd):
        self.fd = fd
        self.buffer = bytearray()
        os.set_blocking(fd, False)
        self.poller = select.poll()
        self.poller.register(fd, 0)

    def wait(self, writing, deadline):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError("bridge response deadline expired")
        wanted = select.POLLOUT if writing else select.POLLIN
        self.poller.modify(self.fd, wanted)
        events = self.poller.poll(None if remaining is None else math.ceil(remaining * 1000))
        if not events:
            raise TimeoutError("bridge response deadline expired")
        flags = events[0][1]
        if flags & select.POLLNVAL:
            raise OSError(errno.EBADF, "invalid bridge descriptor")
        # POLLIN may accompany HUP: drain the final reply before returning EOF.
        if not writing and flags & select.POLLIN:
            return
        if flags & select.POLLHUP:
            raise EOFError("bridge peer disconnected")
        if flags & select.POLLERR or not flags & wanted:
            raise OSError(errno.EIO, "bridge descriptor error")

    def receive(self, deadline=None):
        while True:
            start = self.buffer.find(MAGIC)
            if start < 0:
                # Preserve a possible fragmented magic prefix; bound junk buffering.
                self.buffer[:] = self.buffer[-(len(MAGIC) - 1):]
            else:
                del self.buffer[:start]
                if len(self.buffer) >= PACKET.size:
                    data = bytes(self.buffer[:PACKET.size])
                    del self.buffer[:PACKET.size]
                    return Frame.unpack(data)
            self.wait(False, deadline)
            try:
                data = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            if not data:
                raise EOFError("bridge peer disconnected")
            self.buffer.extend(data)

    def send(self, frame, deadline):
        data = memoryview(frame.pack())
        while data:
            self.wait(True, deadline)
            try:
                written = os.write(self.fd, data)
            except BlockingIOError:
                continue
            if not written:
                raise EOFError("bridge peer disconnected")
            data = data[written:]


def discover(root):
    """Yield (chip name, label or attribute name, canonical input path)."""
    for chip in sorted(Path(root).glob("hwmon*")):
        try:
            name = (chip / "name").read_text().strip()
        except OSError:
            continue
        inputs = sorted(
            (p for p in chip.glob("temp*_input") if re.fullmatch(r"temp\d+_input", p.name)),
            key=lambda p: int(p.name[4:-6]),
        )
        for path in inputs:
            try:
                label = path.with_name(path.name.replace("_input", "_label")).read_text().strip()
            except OSError as exc:
                LOG.debug("label unavailable for %s: %s", path, exc)
                label = path.name
            yield name, label, path.resolve()


class SensorStore:
    """Only explicitly configured sources can be requested by the guest."""

    def __init__(self, sensors, root="/sys/class/hwmon"):
        if not 1 <= len(sensors) <= MAX_CHANNELS:
            raise ValueError("configure 1-16 temperature sources")
        if any(not isinstance(s, str) or not s or (":" not in s and not s.startswith("/"))
               for s in sensors):
            raise ValueError("each sensor must be CHIP:LABEL or an absolute input path")
        self.sensors = sensors
        self.root = root
        self.paths = {}

    def resolve(self, channel):
        spec = self.sensors[channel]
        if spec.startswith("/"):
            return Path(spec)
        chip, label = spec.split(":", 1)
        matches = [path for name, found_label, path in discover(self.root)
                   if name == chip and (found_label == label or path.name == label)]
        if len(matches) != 1:
            raise OSError(errno.ENODEV, f"{spec!r}: expected one sensor, found {len(matches)}")
        return matches[0]

    def read(self, channel):
        if not 0 <= channel < len(self.sensors):
            raise OSError(errno.ENODEV, "unconfigured channel")
        if channel not in self.paths:
            self.paths[channel] = self.resolve(channel)
        try:
            value = int(self.paths[channel].read_text().strip())
        except OSError:
            self.paths.pop(channel, None)  # rediscover after removal/re-enumeration
            raise
        if not MIN_TEMP <= value <= MAX_TEMP:
            raise ValueError("temperature outside protocol range")
        return value


def serve_host(stream, sensors, connected=None):
    while True:
        request = stream.receive()  # no timer and no sensor reads while idle
        if request.status or request.value:
            reply = Frame(request.sequence, request.channel, -errno.EPROTO)
        else:
            try:
                value = sensors.read(request.channel)
                reply = Frame(request.sequence, request.channel, value=value)
            except (OSError, ValueError) as exc:
                LOG.debug("source unavailable: %s", exc)
                reply = Frame(request.sequence, request.channel, -errno.ENODATA)
        stream.send(reply, time.monotonic() + 1.0)
        if connected:
            connected()


def transact(stream, request, timeout):
    deadline = time.monotonic() + timeout
    stream.send(request, deadline)
    while True:
        reply = stream.receive(deadline)
        if (reply.sequence, reply.channel) != (request.sequence, request.channel):
            continue  # expired requests can still have replies in flight
        if not -4095 <= reply.status <= 0 or (
            reply.status == 0 and not MIN_TEMP <= reply.value <= MAX_TEMP
        ):
            return Frame(request.sequence, request.channel, -errno.EPROTO)
        if reply.status not in REPLY_STATUSES:
            return Frame(request.sequence, request.channel, -errno.EIO)
        return reply


def serve_guest(device, port, timeout, connected=None):
    while True:
        request = device.receive()  # kernel wakes us only on an uncached hwmon read
        disconnected = False
        try:
            reply = transact(port, request, timeout)
            if connected:
                connected()
        except TimeoutError:
            reply = Frame(request.sequence, request.channel, -errno.ETIMEDOUT)
        except (EOFError, OSError):
            reply = Frame(request.sequence, request.channel, -errno.ENOTCONN)
            disconnected = True
        try:
            device.send(reply, time.monotonic() + timeout)
        except OSError as exc:
            if exc.errno != errno.ESTALE:
                raise
            LOG.debug("kernel request %s already expired", request.sequence)
        if disconnected:
            raise EOFError("host channel disconnected")


@contextlib.contextmanager
def open_device(path):
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        yield FramedFD(fd)
    finally:
        os.close(fd)


def positive_seconds(value):
    value = float(value)
    if not 0 < value <= 60:
        raise argparse.ArgumentTypeError("must be greater than 0 and at most 60 seconds")
    return value


def stop_service(*_):
    raise SystemExit(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"hwmon-virt {VERSION}")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="mode", required=True)
    listing = sub.add_parser("list", help="list local hwmon temperature sources")
    listing.add_argument("--sysfs-root", default="/sys/class/hwmon")
    host = sub.add_parser("host", help="answer guest requests using local sensors")
    host.add_argument("--socket", required=True, help="QEMU's listening Unix socket")
    source = host.add_mutually_exclusive_group(required=True)
    source.add_argument("--sensor", action="append", help="CHIP:LABEL or absolute path; repeat for channels")
    source.add_argument("--config", type=Path, help='JSON file containing {"sensors": [...]}')
    host.add_argument("--sysfs-root", default="/sys/class/hwmon")
    guest = sub.add_parser("guest", help="bridge the kernel device to a virtio-serial port")
    guest.add_argument("--device", default="/dev/hwmon-virt")
    guest.add_argument("--port", default="/dev/virtio-ports/org.hwmon-virt.0")
    guest.add_argument("--timeout", type=positive_seconds, default=0.5,
                       help="response timeout, below module timeout_ms (default 0.5 seconds)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stop_service)
    if args.mode == "list":
        for name, label, path in discover(args.sysfs_root):
            print(f"{name}:{label}\t{path}")
        return
    if args.mode == "host":
        try:
            sensors = args.sensor
            if args.config:
                config = json.loads(args.config.read_text())
                if not isinstance(config, dict) or set(config) != {"sensors"}:
                    raise ValueError("config must contain only the 'sensors' array")
                sensors = config["sensors"]
            if not isinstance(sensors, list):
                raise ValueError("sensors must be an array")
            store = SensorStore(sensors, args.sysfs_root)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    connection = ConnectionLog()
    while True:
        try:
            if args.mode == "host":
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1.0)
                    sock.connect(args.socket)
                    LOG.debug("opened connection to %s", args.socket)
                    serve_host(FramedFD(sock.fileno()), store, connection.connected)
            else:
                with open_device(args.port) as port, open_device(args.device) as device:
                    LOG.debug("opened %s and %s", args.port, args.device)
                    serve_guest(device, port, args.timeout, connection.connected)
        except (OSError, EOFError) as exc:
            connection.disconnected(exc)
            time.sleep(2)  # connection recovery only; never polls sensor values


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
