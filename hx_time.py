"""MapleJet time: the time server (SNTP), this PC's local time and daylight-saving changes.

Printers are set from the company time server at every Send and by Set clocks, so the
printed dates follow one clock for the whole plant. sntp_query asks one server over UDP
(standard library only); TimeSource keeps the answer as an offset from this PC's clock,
falls back to the PC's own time when no server answers, and never raises.

The network work blocks, so only worker threads call TimeSource.now(). The poller only
calls cached(), which never waits on the network.

A daylight-saving change is found from the time alone (recent_dst_change), so a PC that
started after the change still sees it.
"""

from __future__ import annotations

import math
import socket
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from hx_core import SYSTEM_CLOCK, Clock, Msg
from hx_model import TimeServerError
from i18n import N_, add_spanish

SNTP_PORT = 123
NTP_EPOCH_OFFSET = 2_208_988_800                  # seconds from 1900 to 1970

_EPOCH = datetime(1970, 1, 1)
_PACKET = 48
# NTP era 0 ends in Feb 2036; a stamp with its top bit clear is taken as era 1 (RFC 4330 §3)
_ERA = 2 ** 32

ES = {
    "{server} didn't answer the time request.": "{server} no respondió a la solicitud de hora.",
    "{server} sent a time answer that doesn't make sense.":
        "{server} envió una respuesta de hora que no tiene sentido.",
}
add_spanish(ES)


@dataclass(frozen=True, kw_only=True)
class TimeReading:
    utc: float                   # epoch seconds
    local: datetime              # naive local time, by this PC's time-zone rules
    source: str                  # "server" | "pc"
    server: str = ""             # the host that answered
    offset_s: float = 0.0        # server minus this PC's clock; 0.0 for "pc"


# --------------------------------------------------------------------------
# SNTP
# --------------------------------------------------------------------------

def _no_answer(host):
    return TimeServerError(Msg(N_("{server} didn't answer the time request."), {"server": host}))


def _nonsense(host):
    return TimeServerError(Msg(N_("{server} sent a time answer that doesn't make sense."), {"server": host}))


def _to_ntp(t):
    seconds = math.floor(t)
    fraction = min(int((t - seconds) * 2 ** 32), 2 ** 32 - 1)
    return struct.pack("!II", (seconds + NTP_EPOCH_OFFSET) % _ERA, fraction)


def _from_ntp(stamp):
    seconds, fraction = struct.unpack("!II", stamp)
    if not seconds & 0x8000_0000:
        seconds += _ERA          # after Feb 2036
    return seconds - NTP_EPOCH_OFFSET + fraction / 2 ** 32


def sntp_query(host, *, port=SNTP_PORT, timeout_s=2.0):
    """One SNTP request over UDP (RFC 4330); the server's UTC epoch seconds at the moment its answer
    arrived. Raises TimeServerError. Name resolution isn't bound by timeout_s."""
    try:
        family, type_, proto, _, address = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
    except (OSError, UnicodeError, IndexError):
        raise _no_answer(host) from None
    t1 = time.time()
    sent = _to_ntp(t1)
    request = bytes([0x23]) + bytes(39) + sent       # leap 0, version 4, mode 3 (client)
    deadline = time.monotonic() + timeout_s
    try:
        sock = socket.socket(family, type_, proto)
    except OSError:
        raise _no_answer(host) from None
    with sock:
        try:
            sock.sendto(request, address)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise _no_answer(host)
                sock.settimeout(left)
                try:
                    reply, sender = sock.recvfrom(1024)
                except socket.timeout:
                    raise _no_answer(host) from None
                t4 = time.time()
                if tuple(sender[:2]) == tuple(address[:2]):
                    break
                # a datagram from somewhere else: ignored
        except TimeServerError:
            raise
        except OSError:
            # Windows reports an unreachable port as a reset on the next receive
            raise _no_answer(host) from None
    if len(reply) < _PACKET:
        raise _nonsense(host)
    first = reply[0]
    mode, version, leap, stratum = first & 7, (first >> 3) & 7, first >> 6, reply[1]
    if (mode != 4 or version not in (3, 4) or leap == 3 or not 1 <= stratum <= 15
            or reply[24:32] != sent or reply[40:48] == bytes(8)):
        raise _nonsense(host)
    t2 = _from_ntp(reply[32:40])
    t3 = _from_ntp(reply[40:48])
    offset = ((t2 - t1) + (t3 - t4)) / 2
    return t4 + offset


# --------------------------------------------------------------------------
# Local time
# --------------------------------------------------------------------------

def local_utc_offset(utc):
    """Seconds east of UTC at that moment, by this PC's own time-zone rules."""
    return float(time.localtime(utc).tm_gmtoff)


def to_local(utc, *, offset_fn=local_utc_offset):
    """The naive local time of utc."""
    return _EPOCH + timedelta(seconds=utc + offset_fn(utc))


def recent_dst_change(utc, *, within_s=86_400.0, offset_fn=local_utc_offset):
    """(old offset, new offset, the UTC second of the change) when the offset changed within the last
    within_s, else None. The second found is the first whole second with the new offset, the same
    on every PC whatever utc it was called with. Only one change within within_s is assumed."""
    old = offset_fn(utc - within_s)
    new = offset_fn(utc)
    if old == new:
        return None
    lo, hi = utc - within_s, utc
    while hi - lo > 0.5:
        mid = (lo + hi) / 2
        if offset_fn(mid) == old:
            lo = mid
        else:
            hi = mid
    return (old, new, float(math.floor(hi)))


def parse_host(host):
    """"time.example.local" -> ("time.example.local", 123); "127.0.0.1:4123" -> ("127.0.0.1", 4123).
    Only ASCII digits count as a port: "²".isdigit() is True but int("²") raises."""
    if host.count(":") == 1:
        name, port = host.split(":")
        if port.isascii() and port.isdigit():
            return name, int(port)
    return host, SNTP_PORT


# --------------------------------------------------------------------------
# TimeSource
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Answer:
    host: str                    # as configured
    offset_s: float
    at: float                    # clock.monotonic() when it was answered


class TimeSource:
    """The time server's time, as an offset from this PC's clock. Thread-safe; never raises."""

    timeout_s: float = 2.0       # per host

    def __init__(self, hosts, *, clock: Clock = SYSTEM_CLOCK, query=sntp_query,
                 offset_fn=local_utc_offset, cache_s=600.0):
        self.clock = clock
        self.query = query
        self.offset_fn: Callable[[float], float] = offset_fn
        self.cache_s = cache_s
        self._lock = threading.Lock()
        self._hosts = []
        self._answer = None      # the last good answer
        self._failed_at = None   # clock.monotonic() when every host last failed
        self.set_hosts(hosts)

    def set_hosts(self, hosts):
        """The non-empty hosts, in order (Company.time_server, then time_server_fallback)."""
        kept = [h.strip() for h in hosts if isinstance(h, str) and h.strip()]
        with self._lock:
            if kept != self._hosts:
                # New servers are asked at once, not after the old ones' failure wait
                self._failed_at = None
            self._hosts = kept

    def now(self, *, fresh=False):
        """The time now. fresh=True asks the server(s); otherwise an answer younger than cache_s is
        used, and a failure is remembered for cache_s (nobody is asked meanwhile). No answer gives
        the PC's own time (source "pc"). Blocks up to timeout_s per host: worker threads only."""
        with self._lock:
            mono = self.clock.monotonic()
            answer = self._answer
            if not fresh:
                if answer is not None and mono - answer.at < self.cache_s:
                    return self._reading(answer)
                if answer is None and self._failed_at is not None and mono - self._failed_at < self.cache_s:
                    return self._pc_reading()
            hosts = list(self._hosts)
        # The queries run outside the lock
        for host in hosts:
            try:
                name, port = parse_host(host)
                utc = self.query(name, port=port, timeout_s=self.timeout_s)
                offset = float(utc) - self.clock.time()
            except Exception:
                continue         # TimeServerError, or anything else a host or query may raise: next host
            answer = _Answer(host=host, offset_s=offset, at=self.clock.monotonic())
            with self._lock:
                self._answer = answer
                self._failed_at = None
            return self._reading(answer)
        with self._lock:
            self._answer = None
            self._failed_at = self.clock.monotonic()
        return self._pc_reading()

    def now_from(self, reading):
        """The same source, server and offset as reading, at clock.time() now. No request."""
        utc = self.clock.time() + reading.offset_s
        return TimeReading(utc=utc, local=to_local(utc, offset_fn=self.offset_fn), source=reading.source,
                           server=reading.server, offset_s=reading.offset_s)

    def cached(self):
        """Never queries and never waits on the network: the server reading when an answer was
        cached within the last 2 x cache_s, else the PC's own time."""
        with self._lock:
            answer = self._answer
            if answer is not None and self.clock.monotonic() - answer.at < 2 * self.cache_s:
                return self._reading(answer)
        return self._pc_reading()

    def _reading(self, answer):
        utc = self.clock.time() + answer.offset_s
        return TimeReading(utc=utc, local=to_local(utc, offset_fn=self.offset_fn), source="server",
                           server=answer.host, offset_s=answer.offset_s)

    def _pc_reading(self):
        utc = self.clock.time()
        return TimeReading(utc=utc, local=to_local(utc, offset_fn=self.offset_fn), source="pc")
