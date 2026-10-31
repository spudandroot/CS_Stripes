"""Tests for hx_time: the SNTP client, TimeSource and daylight-saving changes.
Run: python -m unittest -v"""

import socket
import struct
import threading
import time
import unittest
from datetime import datetime
from unittest import mock

import hx_time
from hx_core import FakeClock, Msg
from hx_fake import FakeSntp
from hx_model import TimeServerError
from hx_time import TimeSource, local_utc_offset, parse_host, recent_dst_change, sntp_query, to_local

CHANGE = 1_793_512_800.0         # 1 Nov 2026 06:00:00 UTC: Eastern daylight time ends


def eastern(utc):
    """§9.3 item 4: EDT before the autumn 2026 change, EST after it."""
    return -14_400.0 if utc < CHANGE else -18_000.0


class BadSntp:
    """A UDP server on 127.0.0.1 that answers with whatever answer(request) builds. With decoy set, it
    first sends a good-looking answer from another socket (another address), which must be ignored."""

    def __init__(self, answer, *, decoy=None):
        self.answer = answer
        self.decoy = decoy
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopping.is_set():
            try:
                data, addr = self.sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if self.decoy is not None:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as other:
                    other.sendto(self.decoy(data), addr)
            self.sock.sendto(self.answer(data), addr)

    def stop(self):
        self.stopping.set()
        self.thread.join(timeout=2.0)
        self.sock.close()


def ntp(t):
    seconds = int(t)
    return struct.pack("!II", seconds + hx_time.NTP_EPOCH_OFFSET, int((t - seconds) * 2 ** 32))


def answer(request, *, first=None, stratum=2, originate=None, transmit=None, offset_s=0.0):
    """A server answer to request, as FakeSntp makes it, with one part changed."""
    stamp = ntp(time.time() + offset_s)
    head = bytes([first if first is not None else (4 << 3) | 4, stratum, 6, 0xEC]) + bytes(8) + b"TEST"
    return (head + stamp + (originate if originate is not None else request[40:48]) + stamp
            + (transmit if transmit is not None else stamp))


class SntpTests(unittest.TestCase):
    def test_fake_server(self):
        server = FakeSntp(offset_s=3600).start()
        self.addCleanup(server.stop)
        utc = sntp_query("127.0.0.1", port=server.port, timeout_s=0.5)
        self.assertLess(abs(utc - (time.time() + 3600)), 0.5)
        self.assertEqual(server.requests, 1)

    def test_request(self):
        seen = []

        def capture(request):
            seen.append(request)
            return answer(request)
        server = BadSntp(capture)
        self.addCleanup(server.stop)
        before = time.time()
        sntp_query("127.0.0.1", port=server.port, timeout_s=0.5)
        (request,) = seen
        self.assertEqual(len(request), 48)
        self.assertEqual(request[0], 0x23)                  # leap 0, version 4, mode 3
        self.assertEqual(request[1:40], bytes(39))
        sent = struct.unpack("!I", request[40:44])[0] - hx_time.NTP_EPOCH_OFFSET
        self.assertLess(abs(sent - before), 2)

    def test_silent(self):
        server = FakeSntp(silent=True).start()
        self.addCleanup(server.stop)
        start = time.monotonic()
        with self.assertRaises(TimeServerError) as cm:
            sntp_query("127.0.0.1", port=server.port, timeout_s=0.2)
        self.assertGreaterEqual(time.monotonic() - start, 0.18)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(str(cm.exception), "127.0.0.1 didn't answer the time request.")
        self.assertEqual(cm.exception.msg.values, {"server": "127.0.0.1"})

    def test_bad_answers(self):
        bad = {
            "mode 3": lambda r: answer(r, first=(4 << 3) | 3),
            "version 2": lambda r: answer(r, first=(2 << 3) | 4),
            "wrong originate": lambda r: answer(r, originate=bytes(8)),
            "stratum 0": lambda r: answer(r, stratum=0),
            "stratum 16": lambda r: answer(r, stratum=16),
            "leap indicator 3": lambda r: answer(r, first=(3 << 6) | (4 << 3) | 4),
            "zero transmit": lambda r: answer(r, transmit=bytes(8)),
            "too short": lambda r: answer(r)[:47],
        }
        for name, build in bad.items():
            with self.subTest(name):
                server = BadSntp(build)
                try:
                    with self.assertRaises(TimeServerError) as cm:
                        sntp_query("127.0.0.1", port=server.port, timeout_s=0.5)
                    self.assertEqual(cm.exception.msg.text, "{server} sent a time answer that doesn't make sense.")
                finally:
                    server.stop()
        # Version 3 and a leap second warning are fine
        for first in ((3 << 3) | 4, (1 << 6) | (4 << 3) | 4):
            server = BadSntp(lambda r, first=first: answer(r, first=first, offset_s=-60))
            try:
                self.assertLess(abs(sntp_query("127.0.0.1", port=server.port, timeout_s=0.5)
                                    - (time.time() - 60)), 0.5)
            finally:
                server.stop()

    def test_other_address_ignored(self):
        # A datagram from another socket arrives first and would say an hour ahead
        server = BadSntp(lambda r: answer(r), decoy=lambda r: answer(r, offset_s=3600))
        self.addCleanup(server.stop)
        self.assertLess(abs(sntp_query("127.0.0.1", port=server.port, timeout_s=0.5) - time.time()), 0.5)

    def test_name_not_found(self):
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror("no such name")):
            with self.assertRaises(TimeServerError) as cm:
                sntp_query("time.example.local", timeout_s=0.2)
        self.assertEqual(str(cm.exception), "time.example.local didn't answer the time request.")


class Stub:
    """A query function: answers[host] is a UTC value or an exception to raise."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, host, **kw):
        self.calls.append((host, kw))
        result = self.answers[host]
        if isinstance(result, BaseException):
            raise result
        return result


def failure(host="x"):
    return TimeServerError(Msg("{server} didn't answer the time request.", {"server": host}))


class TimeSourceTests(unittest.TestCase):
    def test_hosts(self):
        clock = FakeClock()
        stub = Stub({"time.example.local": failure(), "192.0.2.123": clock.time() + 12.5})
        source = TimeSource(["time.example.local", "", "192.0.2.123"], clock=clock, query=stub, offset_fn=eastern)
        reading = source.now()
        self.assertEqual((reading.source, reading.server), ("server", "192.0.2.123"))
        self.assertAlmostEqual(reading.offset_s, 12.5)
        self.assertAlmostEqual(reading.utc, clock.time() + 12.5)
        self.assertEqual(reading.local, to_local(reading.utc, offset_fn=eastern))
        self.assertEqual(stub.calls, [("time.example.local", {"port": 123, "timeout_s": 2.0}),
                                      ("192.0.2.123", {"port": 123, "timeout_s": 2.0})])
        self.assertEqual(parse_host("time.example.local"), ("time.example.local", 123))
        self.assertEqual(parse_host("192.0.2.123"), ("192.0.2.123", 123))
        self.assertEqual(parse_host("127.0.0.1:4123"), ("127.0.0.1", 4123))
        self.assertEqual(parse_host("127.0.0.1:"), ("127.0.0.1:", 123))
        self.assertEqual(parse_host("fe80::1"), ("fe80::1", 123))

    def test_host_with_port(self):
        clock = FakeClock()
        stub = Stub({"127.0.0.1": clock.time()})
        source = TimeSource(["127.0.0.1:4123"], clock=clock, query=stub)
        source.timeout_s = 0.5
        self.assertEqual(source.now().server, "127.0.0.1:4123")
        self.assertEqual(stub.calls, [("127.0.0.1", {"port": 4123, "timeout_s": 0.5})])

    def test_cache(self):
        clock = FakeClock()
        stub = Stub({"time.example.local": clock.time() + 3.0})
        source = TimeSource(["time.example.local"], clock=clock, query=stub)
        self.assertEqual(source.cache_s, 600.0)
        self.assertEqual(source.now().source, "server")
        clock.advance(599)
        self.assertAlmostEqual(source.now().offset_s, 3.0)
        self.assertEqual(len(stub.calls), 1)
        clock.advance(1)
        source.now()
        self.assertEqual(len(stub.calls), 2)
        source.now(fresh=True)
        source.now(fresh=True)
        self.assertEqual(len(stub.calls), 4)
        # now_from keeps the offset and moves with the clock
        reading = source.now()
        clock.advance(42)
        later = source.now_from(reading)
        self.assertEqual((later.source, later.server, later.offset_s),
                         (reading.source, reading.server, reading.offset_s))
        self.assertAlmostEqual(later.utc - reading.utc, 42)
        self.assertEqual(len(stub.calls), 4)
        # Every host failing clears the cache
        stub.answers["time.example.local"] = failure()
        reading = source.now(fresh=True)
        self.assertEqual((reading.source, reading.server, reading.offset_s), ("pc", "", 0.0))
        self.assertEqual(reading.utc, clock.time())
        self.assertEqual(source.cached().source, "pc")

    def test_no_hosts(self):
        clock = FakeClock()
        stub = Stub({})
        source = TimeSource(["", ""], clock=clock, query=stub)
        reading = source.now(fresh=True)
        self.assertEqual((reading.source, reading.offset_s), ("pc", 0.0))
        self.assertEqual(stub.calls, [])
        # New hosts are asked at once, not after the failure wait
        stub.answers["time.example.local"] = clock.time() + 1.0
        source.set_hosts(["time.example.local", "", "  "])
        self.assertEqual(source.now().source, "server")
        self.assertEqual(len(stub.calls), 1)

    def test_never_raises(self):
        clock = FakeClock()
        source = TimeSource(["a", "b"], clock=clock, query=Stub({"a": RuntimeError("boom"), "b": OSError("no")}))
        self.assertEqual(source.now().source, "pc")

    def test_now_never_raises_on_an_odd_port(self):
        # "²".isdigit() is True but int("²") raises: only ASCII digits are a port, so the whole entry
        # is the host name, which no server answers (§9.2 items 1, 3 and 7; ² is a key on AZERTY)
        for host in ("time.example.local:²", "x:²", "127.0.0.1:①", "127.0.0.1:12³", "127.0.0.1:４１２３"):
            self.assertEqual(parse_host(host), (host, 123))
        clock = FakeClock()
        stub = Stub({"time.example.local:²": failure(), "x:²": failure(),
                     "time.example.local": clock.time() + 5.0, "192.0.2.123": clock.time() + 12.5})
        # The probe: an odd port alone gives the PC's time
        source = TimeSource(["time.example.local:²"], clock=clock, query=stub)
        self.assertEqual(source.now().source, "pc")
        self.assertEqual(stub.calls, [("time.example.local:²", {"port": 123, "timeout_s": 2.0})])
        # A good fallback after it is still asked
        source.set_hosts(["time.example.local:²", "192.0.2.123"])
        reading = source.now(fresh=True)
        self.assertEqual((reading.source, reading.server), ("server", "192.0.2.123"))
        self.assertAlmostEqual(reading.offset_s, 12.5)
        # A good primary that later fails, with an odd fallback: the PC's time
        source.set_hosts(["time.example.local", "x:²"])
        self.assertEqual(source.now(fresh=True).server, "time.example.local")
        stub.answers["time.example.local"] = failure()
        self.assertEqual(source.now(fresh=True).source, "pc")
        self.assertEqual([host for host, _ in stub.calls[-2:]], ["time.example.local", "x:²"])

    def test_now_never_raises_on_a_host_it_cannot_read(self):
        # Backstop for §9.2 item 7: whatever parse_host raises on an entry, the next host is asked
        clock = FakeClock()
        stub = Stub({"192.0.2.123": clock.time() + 1.0})
        real = hx_time.parse_host

        def parse(host):
            if host == "bad":
                raise ValueError("bad entry")
            return real(host)
        source = TimeSource(["bad", "192.0.2.123"], clock=clock, query=stub)
        with mock.patch.object(hx_time, "parse_host", side_effect=parse):
            self.assertEqual(source.now().server, "192.0.2.123")
            source.set_hosts(["bad"])
            self.assertEqual(source.now(fresh=True).source, "pc")
        self.assertEqual([host for host, _ in stub.calls], ["192.0.2.123"])

    def test_failure_memory_and_cached(self):
        clock = FakeClock()
        stub = Stub({"time.example.local": failure()})
        source = TimeSource(["time.example.local"], clock=clock, query=stub, offset_fn=eastern)
        self.assertEqual(source.offset_fn, eastern)
        self.assertEqual(source.cached().source, "pc")
        self.assertEqual(source.now().source, "pc")
        self.assertEqual(len(stub.calls), 1)
        clock.advance(300)
        self.assertEqual(source.now().source, "pc")
        clock.advance(299)
        source.now()
        self.assertEqual(len(stub.calls), 1)          # nobody asked within cache_s of the failure
        clock.advance(1)
        source.now()
        self.assertEqual(len(stub.calls), 2)
        source.now(fresh=True)
        source.now(fresh=True)
        self.assertEqual(len(stub.calls), 4)          # fresh always asks
        # cached() never asks: the server reading after an answer, until 2 x cache_s
        stub.answers["time.example.local"] = clock.time() + 7.0
        source.now(fresh=True)
        calls = len(stub.calls)
        reading = source.cached()
        self.assertEqual((reading.source, reading.server), ("server", "time.example.local"))
        self.assertAlmostEqual(reading.offset_s, 7.0)
        self.assertEqual(reading.local, to_local(clock.time() + 7.0, offset_fn=eastern))
        clock.advance(1199)
        self.assertEqual(source.cached().source, "server")
        clock.advance(1)
        self.assertEqual(source.cached().source, "pc")
        self.assertEqual(len(stub.calls), calls)

    def test_thread_safe(self):
        clock = FakeClock()
        source = TimeSource(["a"], clock=clock, query=Stub({"a": clock.time() + 1.0}))
        errors = []

        def work():
            try:
                for _ in range(200):
                    source.now(fresh=True)
                    source.cached()
            except Exception as e:      # pragma: no cover - reported below
                errors.append(e)
        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_fake_world_host(self):
        # The fake time server, through a real TimeSource on the system clock
        server = FakeSntp(offset_s=-30).start()
        self.addCleanup(server.stop)
        source = TimeSource([f"127.0.0.1:{server.port}"])
        source.timeout_s = 0.5
        reading = source.now(fresh=True)
        self.assertEqual(reading.source, "server")
        self.assertLess(abs(reading.offset_s + 30), 0.5)

    def test_local(self):
        self.assertEqual(to_local(1_793_275_200.0, offset_fn=lambda u: -14_400.0), datetime(2026, 10, 29, 8, 0))
        self.assertEqual(local_utc_offset(1_793_275_200.0), float(time.localtime(1_793_275_200.0).tm_gmtoff))
        self.assertIsInstance(local_utc_offset(0.0), float)


class DstTests(unittest.TestCase):
    def test_constant(self):
        for utc in (0.0, CHANGE, CHANGE + 3600, 2_000_000_000.0):
            self.assertIsNone(recent_dst_change(utc, offset_fn=lambda u: -18_000.0))

    def test_recent_change(self):
        for after in (1, 2 * 3600, 23 * 3600):
            self.assertEqual(recent_dst_change(CHANGE + after, offset_fn=eastern), (-14_400.0, -18_000.0, CHANGE))
        self.assertIsNone(recent_dst_change(CHANGE + 25 * 3600, offset_fn=eastern))
        self.assertIsNone(recent_dst_change(CHANGE - 1, offset_fn=eastern))
        self.assertEqual(recent_dst_change(CHANGE, offset_fn=eastern), (-14_400.0, -18_000.0, CHANGE))
        self.assertEqual(recent_dst_change(CHANGE + 600, within_s=3600, offset_fn=eastern),
                         (-14_400.0, -18_000.0, CHANGE))
        self.assertIsNone(recent_dst_change(CHANGE + 7200, within_s=3600, offset_fn=eastern))


if __name__ == "__main__":
    unittest.main()
