"""The capture helper: display, window and game captures run in a small program of their own.

Every capture Windows starts keeps about one picture of graphics memory until the program that started it ends
(PC 2026-11-19, the same in OBS). So SprigCastCapture.exe (native/core/tools/capture_helper.cpp) starts them: it
captures on the engine's graphics card and shares each picture with the engine there (as a browser page's are), and
SprigCast ends it once nothing is captured, which gives all of that memory back while SprigCast runs on.

sources/capture.py asks for a capture with :meth:`CaptureHelper.start` and gets a :class:`Feed`: the name of the
shared memory block the engine takes the pictures from (render/native_bridge.py attach_media), which also says how
the capture is doing. The helper going away for any reason ends its feeds; the source then asks again, which starts
a new helper, and keeps the picture it has meanwhile.

SPRIGCAST_CAPTURE_IN_ENGINE=1 turns the helper off: captures run inside SprigCast's engine, as before.
"""

from __future__ import annotations

import atexit
import itertools
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from sprigcast import sprigcore

log = logging.getLogger(__name__)

ENV_OFF = "SPRIGCAST_CAPTURE_IN_ENGINE"
EXE_NAME = "SprigCastCapture.exe"
READY_S = 5.0  # a helper that hasn't said "ready" in this long didn't start
LINGER_S = 2.0  # with nothing left to capture the helper ends after this (a bloom switch stops one, starts another)
BROKEN_S = 60.0  # a helper that didn't start isn't tried again for this long (captures run in the engine meanwhile)
_LEVELS = {10: logging.DEBUG, 20: logging.INFO, 30: logging.WARNING, 40: logging.ERROR}
_numbers = itertools.count(1)


def helper_command() -> list[str] | None:
    """The helper program, next to sprigcore.dll; None when it isn't there (not Windows, an older build)."""
    if sys.platform != "win32":
        return None
    path = Path(sprigcore.dll_path()).with_name(EXE_NAME)
    return [str(path)] if path.is_file() else None


class Feed(str):
    """One capture in the helper. The string is the name of its shared memory block, which is what the engine is
    told (a browser page's block is a plain string): the render thread links it once the engine takes it."""

    holds = True  # a freeze keeps it attached, held (render/engine.py)

    def __new__(cls, name: str, number: int = 0, proc=None):
        self = super().__new__(cls, name)
        self.number, self.proc = number, proc
        self._state = (sprigcore.SCREEN_NONE, 0, 0, 0)
        self._link = None
        return self

    @property
    def told(self) -> bool:
        """The helper has said something about it: its block is there, so the engine may be told its name (the
        engine opens a block once, and never looks again for one that was missing)."""
        return self._state[0] != sprigcore.SCREEN_NONE

    def link(self, engine, source_id: str = "") -> None:
        self._link = (engine, source_id) if engine is not None else None

    @property
    def linked(self) -> bool:
        return self._link is not None

    def state(self) -> tuple[int, int, int, int]:
        """(SCREEN_*, width, height, pictures shared); SCREEN_NONE until the helper has started it."""
        return self._state

    def read(self):
        """The newest picture the engine took, as BGRA rows, or None: for a frame Python draws itself."""
        link = self._link
        if link is None:
            return None
        try:
            return link[0].capture_read(link[1])
        except sprigcore.NativeError:
            return None


class CaptureHelper:
    def __init__(self, command: list[str] | None = None):
        self._command = command
        self.card: tuple[int, int] | None = None  # the engine's graphics card (its LUID)
        self._lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self._ready: threading.Event | None = None
        self._feeds: dict[int, Feed] = {}
        self._linger: threading.Timer | None = None
        self._broken_at: float | None = None
        self._shut = False
        self._starting = 0  # start() calls under way: the helper isn't ended under them
        self.starts = 0  # helpers started
        self.captures = 0  # captures the running helper has started (each keeps a picture until it ends)

    # ---- who may use it ----------------------------------------------------------------------------------------

    def command(self) -> list[str] | None:
        return self._command if self._command is not None else helper_command()

    def wanted(self) -> bool:
        """Captures should run in the helper on this PC (it is there, and not turned off)."""
        return not self._shut and not os.environ.get(ENV_OFF) and self.command() is not None

    def usable(self) -> bool:
        """Wanted, the engine's card is known, and the helper didn't just fail to start."""
        if not self.wanted() or self.card is None:
            return False
        return self._broken_at is None or time.monotonic() - self._broken_at >= BROKEN_S

    def use_card(self, luid: tuple[int, int] | None) -> None:
        """The card the native engine draws on (render/native_bridge.py): the helper captures on the same one."""
        self.card = (int(luid[0]), int(luid[1])) if luid is not None else None

    # ---- captures ----------------------------------------------------------------------------------------------

    def start(self, window: int, monitor: int, flags: int, interval_ms: int) -> Feed | None:
        """Start capturing a window or a display. None when the helper can't be used (capture in the engine).
        Waits for a helper that is starting, so never call it on the render or the UI thread."""
        for _attempt in range(2):  # a helper that ended just as it was asked is started again, once
            if not self.usable():
                return None
            with self._lock:
                proc, ready = self._ensure()
                if proc is None or ready is None:
                    return None
                self._starting += 1  # it has something to capture: it doesn't end for having nothing
            try:
                feed = self._start_in(proc, ready, window, monitor, flags, interval_ms)
            finally:
                with self._lock:
                    self._starting -= 1
                    self._linger_if_idle()
            if feed is not None or self._broken_at is not None or self.proc is proc:
                return feed
        return None

    def _start_in(self, proc: subprocess.Popen, ready: threading.Event, window: int, monitor: int, flags: int,
                  interval_ms: int) -> Feed | None:
        if not ready.wait(READY_S) or proc.poll() is not None:
            if ready.is_set():
                return None  # it started and has ended since (nothing was captured, or by itself): start() asks again
            log.warning("capture helper: it didn't start; captures run in the engine")
            self._broken_at = time.monotonic()
            self._end(proc, asked=True)
            return None
        self._broken_at = None
        card = self.card
        if card is None:
            return None
        number = next(_numbers)
        feed = Feed(f"sprigcast-capture-{os.getpid()}-{number}", number, proc)
        with self._lock:
            if self.proc is not proc:
                return None  # it ended meanwhile: start() asks again
            if self._linger is not None:
                self._linger.cancel()
                self._linger = None
            self._feeds[number] = feed
            self.captures += 1
            mask = 0xFFFFFFFFFFFFFFFF
            sent = self._send(proc, f"start {number} {card[0] & 0xFFFFFFFF} {card[1]} {int(window) & mask} "
                                    f"{int(monitor) & mask} {int(flags)} {int(interval_ms)} {feed}")
            if not sent:
                self._feeds.pop(number, None)
                return None
        return feed

    def stop(self, feed: Feed | None) -> None:
        """End a capture. With none left the helper ends a moment later, and Windows frees what its captures kept."""
        if feed is None:
            return
        with self._lock:
            if self._feeds.pop(feed.number, None) is None:
                return
            if feed.proc is self.proc and self.proc is not None:
                self._send(self.proc, f"stop {feed.number}")
            self._linger_if_idle()

    def _linger_if_idle(self) -> None:  # `_lock` held
        if not self._feeds and not self._starting and self.proc is not None and self._linger is None:
            self._linger = threading.Timer(LINGER_S, self._idle, args=(self.proc,))
            self._linger.daemon = True
            self._linger.start()

    def shutdown(self) -> None:
        with self._lock:
            self._shut = True
            proc = self.proc
        if proc is not None:
            self._end(proc)

    # ---- the program -------------------------------------------------------------------------------------------

    def _ensure(self):  # `_lock` held
        if self._shut:
            return None, None
        if self.proc is not None and self.proc.poll() is None:
            return self.proc, self._ready
        command = self.command()
        if command is None:
            return None, None
        creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, creationflags=creation, close_fds=True)
        except OSError as exc:
            log.warning("capture helper: it didn't start (%s); captures run in the engine", exc)
            self._broken_at = time.monotonic()
            return None, None
        self.proc, self._ready = proc, threading.Event()
        self.starts += 1
        self.captures = 0
        log.info("capture helper started (pid %s)", proc.pid)
        threading.Thread(target=self._read, args=(proc, self._ready), name="sprigcast-capture-pipe",
                         daemon=True).start()
        return proc, self._ready

    def _send(self, proc: subprocess.Popen, line: str) -> bool:  # `_lock` held
        try:
            proc.stdin.write(line.encode("ascii", "replace") + b"\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError, AttributeError):
            return False

    def _idle(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._linger = None
            if self._feeds or self._starting or self.proc is not proc:
                return  # a capture being started keeps it (start() sets the next wait if that comes to nothing)
            self.proc = None
            captures = self.captures
        log.info("capture helper ended (nothing to capture; Windows frees what its %d capture%s kept)", captures,
                 "" if captures == 1 else "s")
        self._end(proc, asked=True)

    def _end(self, proc: subprocess.Popen, asked: bool = False) -> None:
        with self._lock:
            if self.proc is proc:
                self.proc = None
            if asked:
                proc.sprigcast_asked = True  # type: ignore[attr-defined]
            self._send(proc, "quit")
        try:
            proc.stdin.close()
        except (OSError, ValueError, AttributeError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

    def _read(self, proc: subprocess.Popen, ready: threading.Event) -> None:
        try:
            for raw in proc.stdout:
                self._on_line(proc, ready, raw.decode("utf-8", "replace").strip())
        except (OSError, ValueError):
            pass
        code = proc.wait()
        for pipe in (proc.stdout, proc.stdin):
            try:
                pipe.close()
            except (OSError, ValueError, AttributeError):
                pass
        with self._lock:
            mine = [feed for feed in self._feeds.values() if feed.proc is proc]
            for feed in mine:  # their source sees "ended" and asks again: a new helper starts
                self._feeds.pop(feed.number, None)
                feed._state = (sprigcore.SCREEN_ENDED, *feed._state[1:])
            if self.proc is proc:
                self.proc = None
        if not (getattr(proc, "sprigcast_asked", False) or self._shut):
            log.warning("capture helper ended by itself (exit code %s) with %d capture%s running: started again when "
                        "one is asked for", code, len(mine), "" if len(mine) == 1 else "s")

    def _on_line(self, proc: subprocess.Popen, ready: threading.Event, line: str) -> None:
        what, _, rest = line.partition(" ")
        if what == "ready":
            ready.set()
        elif what == "state":
            try:
                number, state, width, height, pictures = (int(part) for part in rest.split())
            except ValueError:
                return
            feed = self._feeds.get(number)
            if feed is not None and feed.proc is proc:
                feed._state = (state, width, height, pictures)
        elif what == "log":
            level, _, message = rest.partition(" ")
            log.log(_LEVELS.get(int(level) if level.isdigit() else 20, logging.INFO), "capture helper: %s", message)


HELPER = CaptureHelper()
atexit.register(HELPER.shutdown)
