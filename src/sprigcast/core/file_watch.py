"""Text files followed live: a death counter a Streamer.bot command bumps shows on the canvas within half a second.

One small thread for every followed file in the app. Four times a second it looks at each file's size and change time
(a stat, a few microseconds) and reads the file only when one of those changed. Nothing here runs on the render
thread: a text source asks `text(key)` for the newest words and `version(key)` to know whether they changed.

    follow("text-3", "C:/counters/deaths.txt")   # starts (or moves) following; the first words come within a tick
    version("text-3"), text("text-3")             # a number that goes up when the words change, and the words
    forget("text-3")                              # the source closed

Writers differ, and each is handled:
- Saved in place (Notepad, Streamer.bot's File Write): size or change time moves, the file is read again.
- Replaced whole (VS Code, many editors write a temp file and rename it over): the stat moves the same way.
- Caught half written (Windows says the file is in use, or it's empty for a moment while being rewritten):
  the last words stay and it's read again on the next tick.
- Gone (deleted, a drive unplugged): the last words stay and `missing(key)` turns True until it's back.
Text is UTF-8 (a BOM is dropped); a file in another code page shows with replacement marks rather than failing.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

TICK_S = 0.25  # how often each file is looked at: 4 stats a second each, nothing else unless it changed
SETTLE_S = 0.15  # an empty read this soon after a write is likely a writer mid-way: look again next tick
MAX_BYTES = 64 * 1024  # a text on a canvas: the start of a huge file is plenty (and a log can't stall the thread)


@dataclass
class _Followed:
    path: str
    stamp: tuple | None = None  # (size, mtime_ns) last read
    words: str = ""
    version: int = 0
    missing: bool = False
    read_once: bool = False


_lock = threading.Lock()
_followed: dict[str, _Followed] = {}
_wake = threading.Event()
_thread: threading.Thread | None = None


def decode(data: bytes) -> str:
    """A text file's words: UTF-8 with or without a BOM; UTF-16 if it starts with one (PowerShell's Out-File)."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def read_words(path: str) -> str:
    """The file's words now, as a text source shows them (line ends made \\n, spaces trimmed at both ends).
    Raises OSError when it can't be read."""
    with open(path, "rb") as f:
        data = f.read(MAX_BYTES)
    return decode(data).replace("\r\n", "\n").replace("\r", "\n").strip()


def follow(key: str, path: str) -> None:
    """Follow `path` for `key` (a source id). Following the same path again does nothing."""
    global _thread
    with _lock:
        current = _followed.get(key)
        if current is not None and current.path == path:
            return
        _followed[key] = _Followed(path, version=(current.version + 1) if current else 0)
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_run, name="text file watch", daemon=True)
            _thread.start()
    _wake.set()  # read it now, not on the next tick


def forget(key: str) -> None:
    with _lock:
        _followed.pop(key, None)


def following(key: str) -> str | None:
    with _lock:
        entry = _followed.get(key)
        return entry.path if entry else None


def version(key: str) -> int:
    """Goes up each time the words change (or the file goes missing or comes back). -1 when not followed."""
    entry = _followed.get(key)  # a dict lookup: safe without the lock, and this runs on the render thread
    return entry.version if entry is not None else -1


def text(key: str) -> str:
    entry = _followed.get(key)
    return entry.words if entry is not None else ""


def missing(key: str) -> bool:
    entry = _followed.get(key)
    return bool(entry and entry.missing)


def read_once(key: str) -> bool:
    """True once the file has been looked at (read, or found missing) at least once."""
    entry = _followed.get(key)
    return bool(entry and entry.read_once)


def wait_read(key: str, timeout: float = 1.0) -> bool:
    """Wait (off the render thread only) until the file was looked at once. For a first drawing and tests."""
    end = time.monotonic() + timeout
    while not read_once(key):
        if time.monotonic() >= end:
            return False
        _wake.set()
        time.sleep(0.005)
    return True


def check_now() -> None:
    """Look at every followed file once, on the calling thread (tests)."""
    _check_all()


def _run() -> None:
    while True:
        _wake.wait(TICK_S)
        _wake.clear()
        with _lock:
            if not _followed:
                globals()["_thread"] = None  # nothing left to follow: the thread ends; follow() starts another
                return
        try:
            _check_all()
        except Exception:  # never let one odd file stop every counter
            log.exception("text file watch")


def _check_all() -> None:
    with _lock:
        entries = list(_followed.items())
    for key, entry in entries:
        _check(key, entry)


def _check(key: str, entry: _Followed) -> None:
    try:
        st = os.stat(entry.path)
    except OSError:
        if not entry.missing:
            entry.missing = True
            entry.stamp = None
            entry.version += 1  # the words stay; the source shows it's missing
            log.info("text file %s: not found, keeping the last text", entry.path)
        entry.read_once = True
        return
    stamp = (st.st_size, st.st_mtime_ns)
    if stamp == entry.stamp and not entry.missing:
        return
    try:
        words = read_words(entry.path)
    except OSError:  # in use by its writer this instant: look again next tick
        entry.read_once = entry.read_once or entry.stamp is not None
        return
    if not words and entry.words and time.time() - st.st_mtime_ns / 1e9 < SETTLE_S:
        return  # emptied a moment ago, probably mid-rewrite: wait a tick before showing nothing
    came_back = entry.missing
    entry.missing = False
    entry.stamp = stamp
    if words != entry.words or came_back or not entry.read_once:
        entry.words = words
        entry.version += 1
    entry.read_once = True
