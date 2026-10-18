# SPDX-License-Identifier: GPL-2.0-only
import errno
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import hwmon_virt as kv


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)

    def pair(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        return left, right

    def start(self, target, *args):
        errors = []

        def run():
            try:
                target(*args)
            except (EOFError, BrokenPipeError):
                pass
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, errors

    def sensor(self, directory="hwmon3", value="52000", label="Tctl", name="k10temp"):
        chip = self.root / directory
        chip.mkdir(exist_ok=True)
        (chip / "name").write_text(name + "\n")
        (chip / "temp1_label").write_text(label + "\n")
        source = chip / "temp1_input"
        source.write_text(value + "\n")
        return source

    def test_wire_layout_matches_c_header(self):
        frame = kv.Frame(0x1122334455667788, 2, -errno.ENODATA, -5000)
        expected = struct.pack("<IIIIii", 0x3156544B, 0x55667788, 0x11223344,
                               2, -errno.ENODATA, -5000)
        self.assertEqual(frame.pack(), expected)
        self.assertEqual(len(expected), 24)
        self.assertEqual(kv.Frame.unpack(expected).value, -5000)

    def test_fragmentation_and_coalesced_frames(self):
        left, right = self.pair()
        receiver = kv.FramedFD(right.fileno())
        data = kv.Frame(1, 0, value=-4000).pack() + kv.Frame(2, 1, value=61000).pack()

        def write_fragments():
            for byte in data:
                os.write(left.fileno(), bytes([byte]))

        thread, errors = self.start(write_fragments)
        self.assertEqual(receiver.receive(time.monotonic() + 1).value, -4000)
        self.assertEqual(receiver.receive(time.monotonic() + 1).value, 61000)
        thread.join(1)
        self.assertFalse(errors)

    def test_resynchronizes_after_partial_old_frame(self):
        left, right = self.pair()
        receiver = kv.FramedFD(right.fileno())
        os.write(left.fileno(), b"noise" + kv.Frame(1, 0).pack()[:9])
        with self.assertRaises(TimeoutError):
            receiver.receive(time.monotonic() + 0.02)
        os.write(left.fileno(), kv.Frame(2, 0, value=42000).pack() + kv.Frame(3, 0, value=43000).pack())
        # A torn frame may be discarded as stale; subsequent framing must recover.
        found = [receiver.receive(time.monotonic() + 1) for _ in range(2)]
        self.assertEqual(found[-1].sequence, 3)
        self.assertEqual(found[-1].value, 43000)

    def test_input_deadline_and_disconnect(self):
        left, right = self.pair()
        stream = kv.FramedFD(right.fileno())
        with self.assertRaises(TimeoutError):
            stream.receive(time.monotonic() + 0.02)
        left.close()
        with self.assertRaises(EOFError):
            stream.receive(time.monotonic() + 1)

    def test_hangup_without_writability_fails_fast(self):
        # A pipe with its writer gone has the same HUP-without-POLLOUT shape
        # as a disconnected virtio port; no socket-specific writability here.
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        os.close(writer)
        start = time.monotonic()
        with self.assertRaises(EOFError):
            kv.FramedFD(reader).send(kv.Frame(1, 0), start + 0.5)
        self.assertLess(time.monotonic() - start, 0.2)

    def test_buffered_reply_is_delivered_before_hangup(self):
        left, right = self.pair()
        os.write(left.fileno(), kv.Frame(8, 0, value=45625).pack())
        left.close()
        stream = kv.FramedFD(right.fileno())
        self.assertEqual(stream.receive(time.monotonic() + 0.5).value, 45625)
        with self.assertRaises(EOFError):
            stream.receive(time.monotonic() + 0.5)

    def test_poll_errors_and_invalid_fd_are_reported(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, writer)
        os.close(reader)
        with self.assertRaises(OSError):
            kv.FramedFD(writer).wait(True, time.monotonic() + 0.5)
        left, right = self.pair()
        stream = kv.FramedFD(left.fileno())
        left.close()
        with self.assertRaises(OSError) as caught:
            stream.wait(False, time.monotonic() + 0.5)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_sources_use_labels_and_read_fresh_values(self):
        source = self.sensor()
        store = kv.SensorStore(["k10temp:Tctl"], self.root)
        self.assertEqual(store.read(0), 52000)
        source.write_text("53500\n")
        self.assertEqual(store.read(0), 53500)
        self.assertEqual(kv.SensorStore([str(source)]).read(0), 53500)
        self.assertEqual(kv.SensorStore(["k10temp:temp1_input"], self.root).read(0), 53500)

    def test_ambiguous_missing_and_invalid_sources_fail(self):
        source = self.sensor()
        self.sensor("hwmon4")
        with self.assertRaises(OSError):
            kv.SensorStore(["k10temp:Tctl"], self.root).read(0)
        with self.assertRaises(OSError):
            kv.SensorStore(["coretemp:Package id 0"], self.root).read(0)
        for value in ("N/A", "1000001", "-273151"):
            source.write_text(value)
            with self.assertRaises(ValueError):
                kv.SensorStore([str(source)]).read(0)

    def test_source_reenumeration(self):
        source = self.sensor()
        store = kv.SensorStore(["k10temp:Tctl"], self.root)
        self.assertEqual(store.read(0), 52000)
        source.unlink()
        (source.parent / "name").unlink()
        self.sensor("hwmon9", value="57000")
        with self.assertRaises(OSError):
            store.read(0)
        self.assertEqual(store.read(0), 57000)

    def test_bad_label_does_not_break_unrelated_discovery(self):
        self.sensor()
        bad = self.sensor("hwmon4", name="unrelated")
        original = Path.read_text
        for error in (errno.EIO, errno.EACCES):
            def read(path, *args, **kwargs):
                if path == bad.with_name("temp1_label"):
                    raise OSError(error, "unreadable label")
                return original(path, *args, **kwargs)

            with self.subTest(error=error), mock.patch.object(Path, "read_text", read):
                self.assertEqual(kv.SensorStore(["k10temp:Tctl"], self.root).read(0), 52000)
                self.assertEqual(kv.SensorStore(["unrelated:temp1_input"], self.root).read(0), 52000)
                self.assertIn(("unrelated", "temp1_input", bad), list(kv.discover(self.root)))

    def test_no_reads_until_requested_and_explicit_channels_only(self):
        source = self.sensor()
        store = kv.SensorStore([str(source)])
        count = []
        original = store.read

        def record(channel):
            count.append(channel)
            return original(channel)

        store.read = record
        left, right = self.pair()
        thread, errors = self.start(kv.serve_host, kv.FramedFD(right.fileno()), store)
        stream = kv.FramedFD(left.fileno())
        time.sleep(0.03)
        self.assertEqual(count, [])
        reply = kv.transact(stream, kv.Frame(1, 0), 0.5)
        self.assertEqual((reply.status, reply.value), (0, 52000))
        self.assertEqual(count, [0])
        reply = kv.transact(stream, kv.Frame(2, 99), 0.5)
        self.assertEqual(reply.status, -errno.ENODATA)
        left.close()
        thread.join(1)
        self.assertFalse(errors)

    def test_late_wrong_channel_and_invalid_replies(self):
        left, right = self.pair()

        def responder():
            peer = kv.FramedFD(right.fileno())
            request = peer.receive(time.monotonic() + 1)
            deadline = time.monotonic() + 1
            peer.send(kv.Frame(request.sequence - 1, 0, value=99000), deadline)
            peer.send(kv.Frame(request.sequence, 1, value=88000), deadline)
            peer.send(kv.Frame(request.sequence, 0, value=47000), deadline)

        thread, errors = self.start(responder)
        reply = kv.transact(kv.FramedFD(left.fileno()), kv.Frame(42, 0), 0.5)
        self.assertEqual(reply.value, 47000)
        thread.join(1)
        self.assertFalse(errors)

        for status, value in ((1, 0), (-5000, 0), (0, 1000001)):
            left, right = self.pair()
            os.write(right.fileno(), kv.Frame(1, 0, status, value).pack())
            self.assertEqual(kv.transact(kv.FramedFD(left.fileno()), kv.Frame(1, 0), 0.5).status,
                             -errno.EPROTO)

    def test_full_guest_host_roundtrip_and_timeout(self):
        source = self.sensor()
        app, device = self.pair()
        port, host = self.pair()
        guest_thread, guest_errors = self.start(
            kv.serve_guest, kv.FramedFD(device.fileno()), kv.FramedFD(port.fileno()), 0.05)
        app_stream = kv.FramedFD(app.fileno())
        # Connected transport with a host that has not begun answering.
        reply = kv.transact(app_stream, kv.Frame(1, 0), 0.5)
        self.assertEqual(reply.status, -errno.ETIMEDOUT)
        host_thread, host_errors = self.start(
            kv.serve_host, kv.FramedFD(host.fileno()), kv.SensorStore([str(source)]))
        reply = kv.transact(app_stream, kv.Frame(2, 0), 0.5)
        self.assertEqual((reply.status, reply.value), (0, 52000))
        source.write_text("not a temperature")
        reply = kv.transact(app_stream, kv.Frame(3, 0), 0.5)
        self.assertEqual(reply.status, -errno.ENODATA)
        app.close()
        guest_thread.join(1)
        port.close()
        host_thread.join(1)
        self.assertFalse(guest_errors)
        self.assertFalse(host_errors)

    def test_internal_and_unrecognized_error_codes_are_normalized(self):
        for status in (-512, -513, -514, -516, -517, -529, -errno.EACCES, -4095):
            with self.subTest(status=status):
                left, right = self.pair()
                os.write(right.fileno(), kv.Frame(1, 0, status).pack())
                reply = kv.transact(kv.FramedFD(left.fileno()), kv.Frame(1, 0), 0.5)
                self.assertEqual(reply.status, -errno.EIO)
        for status in (-errno.ENODATA, -errno.EPROTO, -errno.ETIMEDOUT, -errno.ENOTCONN, -errno.EIO):
            left, right = self.pair()
            os.write(right.fileno(), kv.Frame(1, 0, status).pack())
            self.assertEqual(kv.transact(kv.FramedFD(left.fileno()), kv.Frame(1, 0), 0.5).status,
                             status)

    def test_retries_log_only_outage_and_real_recovery(self):
        source = self.sensor()
        # Opening succeeds on each attempt, but only the third connection carries
        # a request. Opening alone must not reset outage suppression.
        class Session:
            def __init__(self, requests):
                self.requests = iter(requests)

            def receive(self, *_):
                try:
                    return next(self.requests)
                except StopIteration:
                    raise EOFError("peer absent") from None

            def send(self, *_):
                pass

        class StopRetries(Exception):
            pass

        sessions = [Session([]), Session([]), Session([kv.Frame(1, 0)]), Session([])]
        with mock.patch.object(sys, "argv", ["bridge", "host", "--socket", "/unused",
                                               "--sensor", str(source)]), \
             mock.patch.object(kv.signal, "signal"), \
             mock.patch.object(kv.socket, "socket"), \
             mock.patch.object(kv, "FramedFD", side_effect=sessions), \
             mock.patch.object(kv.time, "sleep", side_effect=[None, None, None, StopRetries]), \
             self.assertLogs(kv.LOG, level="DEBUG") as logs:
            with self.assertRaises(StopRetries):
                kv.main()
        self.assertEqual(sum(r.levelname == "WARNING" for r in logs.records), 2)
        self.assertEqual(sum(r.levelname == "INFO" for r in logs.records), 1)


if __name__ == "__main__":
    unittest.main()
