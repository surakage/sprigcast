"""Pair a device: a short code instead of copying the long password.

Settings → Connections → Pair a device shows a 6-digit code for CODE_SECONDS. An app sends it once
(SprigCast API: {"pair": "123456", "name": "Stream Deck"} instead of the password answer; web links:
/api/pair?code=123456&name=Streamer.bot) and gets its own key back. From then on the app uses that key
wherever the password goes. Each device has its own key, listed in Settings → Connections, where it can be
removed without changing the password for everything else.

No Qt here: the app saves to_json() (encrypted, see secret_store) and loads it with from_json().
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

CODE_SECONDS = 120
MOST_TRIES = 5  # wrong codes within TRY_WINDOW before pairing locks (and the code is cancelled)
TRY_WINDOW = 60.0
LOCK_SECONDS = 60.0  # no tries are even looked at while locked (soak 2026-11-16: ~100 guesses a second got answers)
MOST_DEVICES = 50
SEEN_SAVE_SECONDS = 60.0  # a device's last_seen is saved at most this often (web links sign in on every press)


@dataclass
class Device:
    id: str
    name: str
    key: str
    added: str  # YYYY-MM-DD
    last_seen: str = ""  # when its key last signed in or disconnected, UTC "2026-11-19T15:29:50Z" ("" = not yet)


def _utc(when: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


class Pairing:
    def __init__(self, devices: list[Device] | None = None, clock: Callable[[], float] = time.monotonic,
                 on_change: Callable[[], None] = lambda: None, wall: Callable[[], float] = time.time):
        self.devices: list[Device] = list(devices or [])
        self.removed: set[str] = set()  # ids removed here (so a merge with the saved list doesn't bring them back)
        self._clock = clock
        self.on_change = on_change
        self.on_seen: Callable[[], None] = lambda: None  # a last_seen is worth saving (see seen)
        self._wall = wall
        self._seen_saved: dict[str, float] = {}  # device id -> when (monotonic) its last_seen was last handed on
        self.code = ""
        self._until = 0.0
        self._wrong: list[float] = []  # when the recent wrong tries came (any code, or none showing)
        self._locked_until = 0.0

    # The code ----------------------------------------------------------------------------

    def start(self) -> str:
        """A new code (any old one stops working)."""
        self.code = f"{secrets.randbelow(1_000_000):06d}"
        self._until = self._clock() + CODE_SECONDS
        return self.code

    def cancel(self) -> None:
        self.code = ""

    def seconds_left(self) -> int:
        if not self.code:
            return 0
        left = self._until - self._clock()
        if left <= 0:
            self.code = ""
            return 0
        return int(left + 0.999)

    def locked_seconds(self) -> int:
        """Seconds left of a lock after too many wrong codes (0 = not locked)."""
        left = self._locked_until - self._clock()
        return int(left + 0.999) if left > 0 else 0

    def _wrong_try(self) -> None:
        now = self._clock()
        self._wrong = [t for t in self._wrong if now - t < TRY_WINDOW] + [now]
        if len(self._wrong) >= MOST_TRIES:
            self._locked_until = now + LOCK_SECONDS
            self._wrong.clear()
            self.code = ""  # a fresh code is needed after a lock, so guesses never carry over

    def redeem(self, code: object, name: object = "") -> Device | None:
        """The new device for a right code (the code is then used up), else None. MOST_TRIES wrong codes within
        TRY_WINDOW lock pairing for LOCK_SECONDS: tries are refused unread, and the code is cancelled."""
        if self.locked_seconds():
            return None
        given = "".join(ch for ch in str(code) if ch.isdigit())
        if not self.seconds_left() or not secrets.compare_digest(given.encode(), self.code.encode()):
            self._wrong_try()
            return None
        self.code = ""
        self._wrong.clear()
        label = " ".join(str(name or "").split())[:40] or "Device"
        device = Device(id=secrets.token_hex(4), name=label, key="sc_" + secrets.token_urlsafe(24),
                        added=time.strftime("%Y-%m-%d"), last_seen=_utc(self._wall()))
        self._seen_saved[device.id] = self._clock()
        self.devices.append(device)
        del self.devices[:-MOST_DEVICES]
        self.on_change()
        return device

    # Devices -----------------------------------------------------------------------------

    def keys(self) -> list[str]:
        return [d.key for d in self.devices]

    def seen(self, key: str) -> Device | None:
        """The device with this key signed in or disconnected: its last_seen is now. on_seen() (save) runs at most
        once a SEEN_SAVE_SECONDS per device, so a button pressed again and again doesn't write each time; the time
        in memory is always current, and closing SprigCast saves it."""
        device = next((d for d in self.devices if key and secrets.compare_digest(d.key.encode(), key.encode())), None)
        if device is None:
            return None
        device.last_seen = _utc(self._wall())
        now = self._clock()
        last = self._seen_saved.get(device.id)
        if last is None or now - last >= SEEN_SAVE_SECONDS:
            self._seen_saved[device.id] = now
            self.on_seen()
        return device

    def remove(self, device_id: str) -> bool:
        before = len(self.devices)
        self.devices = [d for d in self.devices if d.id != device_id]
        if len(self.devices) != before:
            self.removed.add(device_id)
            self.on_change()
            return True
        return False

    def merged(self, stored: list[Device], seen: set[str]) -> list[Device]:
        """This list joined with the saved one, which another SprigCast (a second copy, a test tool) may have
        changed since: theirs are added, ones removed here stay removed, and ones this copy saw saved before but
        that are gone now were removed elsewhere. Devices paired here and not saved yet are kept. Each keeps the newest
        last_seen of the two."""
        there = {d.id: d for d in stored}
        mine = [d for d in self.devices if d.id in there or d.id not in seen]
        for d in mine:  # the newest last_seen, whichever copy saw it
            theirs = there.get(d.id)
            if theirs is not None and theirs.last_seen > d.last_seen:
                d.last_seen = theirs.last_seen
        have = {d.id for d in mine}
        return mine + [d for d in stored if d.id not in have and d.id not in self.removed]

    def to_json(self) -> str:
        return json.dumps([asdict(d) for d in self.devices])

    @staticmethod
    def devices_from_json(text: str) -> list[Device]:
        try:
            rows = json.loads(text or "[]")
        except ValueError:
            return []
        out = []
        for row in rows if isinstance(rows, list) else []:
            try:
                out.append(Device(id=str(row["id"]), name=str(row["name"]), key=str(row["key"]),
                                  added=str(row.get("added", "")), last_seen=str(row.get("last_seen", "") or "")))
            except (KeyError, TypeError):
                continue
        return out


def matches(given: str, keys: list[str]) -> bool:
    """Whether `given` is one of the keys (each compared in constant time)."""
    ok = False
    for key in keys:
        if key and secrets.compare_digest(given.encode(), key.encode()):
            ok = True
    return ok
