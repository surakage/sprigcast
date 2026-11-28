"""Notices a server that has stopped answering while a stream to it is quiet.

A busy stream finds a dead connection by itself: its send buffer fills within a second or two and the stall is
seen (shared_stream.StallWatch, SEND_TIMEOUT). A quiet one doesn't: a still picture with no sound sent 23 kbps,
and with every packet to the server silently dropped for 240 s all six places still said "live" (sim PC,
2026-11-28), because so little data takes minutes to fill anything.

So, only while a live place sends less than QUIET_KBPS, its server is asked every PROBE_S whether it still
answers: one small connection, opened and closed, no data. After FAILS in a row with no answer at all, the quiet
places on that server are told the link is lost and connect again the usual way. Nothing extra is ever sent on
the stream itself, and a place sending normally is never probed.

Cost: one sleeping thread while live; one connect per quiet server every PROBE_S.
"""

from __future__ import annotations

import errno
import logging
import socket
import threading
import time
import weakref
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

PROBE_S = 5.0  # how often a quiet place's server is asked
PROBE_TIMEOUT = 4.0  # how long one ask may take
FAILS = 3  # asks in a row with no answer before the link is called lost (15 to 27 s)
QUIET_KBPS = 800  # a live place sending less than this can't notice a dead connection by itself in time
# After an alarm a server isn't asked again for a while, twice as long each time: a server that takes the stream
# but never answers the asks can then only cost a few reconnects, not one every 15 s. Once it has answered for
# STEADY_S the wait starts small again, so a second and third real outage are noticed as fast as the first.
# (It used to stop asking for the whole run after 2 alarms that ended soon; two real outages did that, and a
# third went unnoticed for its full 120 s, sim PC 2026-11-28.)
REST_FIRST_S = 20.0
REST_MOST_S = 600.0
STEADY_S = 60.0
PORTS = {"rtmp": 1935, "rtmps": 443}
_UNREACHABLE = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.ETIMEDOUT}


def server_of(url: str) -> tuple[str, int] | None:
    """(host, port) of an RTMP(S) stream URL, or None for anything else."""
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in PORTS:
            return None  # RTMP and RTMPS only: both answer a plain connection
        port = parts.port or PORTS[parts.scheme.lower()]
        return (parts.hostname, port) if parts.hostname and port else None
    except ValueError:
        return None


def answers(server: tuple[str, int], timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether the server still answers at all. A refusal is an answer (it is reachable); only silence, an
    unreachable network or a name that no longer resolves count as no answer."""
    try:
        socket.create_connection(server, timeout=timeout).close()
        return True
    except (TimeoutError, socket.gaierror):
        return False
    except OSError as exc:
        return exc.errno not in _UNREACHABLE


class LinkWatch:
    def __init__(self, ask=answers, clock=time.monotonic):
        self._ask, self._clock = ask, clock
        self._links: weakref.WeakSet = weakref.WeakSet()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._sent: dict[int, tuple[int, float]] = {}  # id(link) -> (bytes_out, when), for its rate
        self._fails: dict[tuple[str, int], int] = {}
        self._rest: dict[tuple[str, int], tuple[float, float]] = {}  # server -> (not asked before, next wait)
        self._steady: dict[tuple[str, int], float] = {}  # server -> answering without a miss since
        self.enabled = True  # the tests switch the shared one off: they make streams to servers that don't exist

    def add(self, link) -> None:
        """Watch a stream connection: anything with connected, bytes_out, _url, name and link_lost(reason)."""
        if not self.enabled:
            return
        with self._lock:
            self._links.add(link)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="sprigcast-link-watch", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        idle = 0
        while idle < 3:  # ends by itself a little after the last stream has gone
            time.sleep(PROBE_S)
            with self._lock:
                links = list(self._links)
            idle = 0 if links else idle + 1
            try:
                self.check(links)
            except Exception:
                log.exception("link watch: check failed")
        with self._lock:
            self._thread = None

    def quiet(self, link, now: float) -> bool:
        """Whether this live link has sent less than QUIET_KBPS since it was last looked at."""
        sent = int(getattr(link, "bytes_out", 0) or 0)
        before = self._sent.get(id(link))
        self._sent[id(link)] = (sent, now)
        if before is None or now <= before[1] or sent < before[0]:
            return False  # first look, or a new connection: wait for the next one
        return (sent - before[0]) * 8 / 1000 / (now - before[1]) < QUIET_KBPS

    def check(self, links) -> list:
        """One round: ask each quiet live link's server, and tell the links of a server lost FAILS times in a
        row. Returns the links told."""
        now = self._clock()
        quiet: dict[tuple[str, int], list] = {}
        seen = set()
        for link in links:
            seen.add(id(link))
            if not getattr(link, "connected", False):
                self._sent.pop(id(link), None)
                continue
            server = server_of(getattr(link, "_url", "") or "")
            if server is not None and self.quiet(link, now) and now >= self._rest.get(server, (0.0, 0.0))[0]:
                quiet.setdefault(server, []).append(link)
        for key in [key for key in self._sent if key not in seen]:
            del self._sent[key]
        for server in [s for s in self._fails if s not in quiet]:
            del self._fails[server]  # busy again, or gone: start counting afresh
        told = []
        for server, members in quiet.items():
            if self._ask(server):
                self._fails.pop(server, None)
                if server in self._rest and now - self._steady.setdefault(server, now) >= STEADY_S:
                    del self._rest[server]  # answering steadily again: the next outage is a first one
                continue
            self._steady.pop(server, None)
            self._fails[server] = self._fails.get(server, 0) + 1
            if self._fails[server] < FAILS:
                continue
            self._fails.pop(server, None)
            wait = self._rest.get(server, (0.0, REST_FIRST_S))[1]
            self._rest[server] = (now + wait, min(wait * 2, REST_MOST_S))
            reason = (f"{server[0]} stopped answering for about {FAILS * PROBE_S:.0f} s while little was being "
                      "sent; connecting again")
            for link in members:
                log.warning("stream %s: %s", getattr(link, "name", "?"), reason)
                link.link_lost(reason)
                told.append(link)
        return told


WATCH = LinkWatch()
