"""Browser sources in their own process, so a page can never freeze SprigCast.

Copying a page's picture out of Qt WebEngine waits for the graphics card. On the window's thread that froze the
window for 0.3 s at a time in the 2026-11-17 heavy run, and with Python's lock held the render thread waited
too. OBS never copies on its window's thread: Chromium hands each finished frame over from its own thread.
Here the pages live in a helper process (this file's serve()), which does the copying on its own thread with
its own Python lock. SprigCast reads the newest frame from shared memory (a memory copy, no waiting) and the
page's sound from a pipe.

The helper starts with the first page and ends LINGER_S after the last one closes (a bloom switch back to it
doesn't start Chromium again). If it stops, it starts again and opens the same pages. Without a helper
(SPRIGCAST_BROWSER_IN_PROCESS, or it can't start) pages are drawn in SprigCast itself, as before.

Messages: SprigCast -> helper are JSON lines on its stdin ({"op": "open", "id": ..., "settings": ...}, close,
pause, reload, state, interact, memory (SprigCast reads a page's pictures itself for now), quit). Helper -> SprigCast are length-prefixed JSON on its stdout, sometimes
followed by bytes: "frames" (a page's shared memory block), "sound" (float32 stereo, 48 kHz), "desktop" (the
page's sound plays on the desktop instead, and why), "note" (the page said its video can't play), "log".
"""

from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

LINGER_S = 90.0  # the helper stays this long after its last page closed, then ends (its memory goes back)
RESTART_AFTER_S = 2.0  # a helper that stopped is started again this long after, with the same pages
CARD_ENV = "SPRIGCAST_BROWSER_CARD"  # "low,high": the native engine's card, pages are drawn on it (browser_quick)
MEMORY_ASK_S = 0.5  # while SprigCast reads a page drawn on the card itself, it asks for memory pictures this often
HEADER = 64  # bytes before the two picture slots: seq (u64), width, height, slot, texture number (u32 each)
SLOT_BYTES_AT = 32  # then, here, the size of one slot (u64): where the second one starts
ON_CARD = 2  # the slot of a picture drawn on the card (app/browser_quick.py), read by the engine there
SOUND_MS = 20  # the helper sends each page's sound this often


def _attach(name: str):
    """Open the helper's shared memory block by name, without Python's resource tracker owning it."""
    from multiprocessing import shared_memory

    shm = shared_memory.SharedMemory(name=name, create=False)
    if os.name != "nt":
        try:
            from multiprocessing import resource_tracker

            resource_tracker.unregister(shm._name, "shared_memory")  # the helper made it: it removes it
        except Exception:  # noqa: BLE001
            pass
    return shm


class FrameSlot:
    """One page's newest picture in shared memory: two slots, written in turn, with a counter.

    The helper writes the slot not being read, then the size, the slot number and last the counter. SprigCast
    reads the counter, copies the slot, and reads the counter again: a picture overwritten while it was copied
    (two newer ones in that time) is skipped and the next one taken."""

    def __init__(self, shm, owner: bool):
        self.shm, self.owner = shm, owner
        self.buf = shm.buf
        # The slot size its maker wrote, never worked out from the block's size: Windows gives whoever opens a
        # block its size rounded up to whole pages, the maker the size asked for. Halving each one's own size put
        # the second slot 2016 bytes apart for a 1920x1080 page, so every other picture read from memory came out
        # 504 pixels to the side and wrapped around (the user's recording, 2026-11-19).
        room = (len(self.buf) - HEADER) // 2
        said = struct.unpack_from("<Q", self.buf, SLOT_BYTES_AT)[0]
        self.capacity = said if 0 < said <= room else room
        self.seen = 0
        self.peeked = 0

    @classmethod
    def create(cls, name: str, pixels: int) -> "FrameSlot":
        from multiprocessing import shared_memory

        shm = shared_memory.SharedMemory(name=name, create=True, size=HEADER + 2 * pixels * 4)
        shm.buf[:HEADER] = bytes(HEADER)
        struct.pack_into("<Q", shm.buf, SLOT_BYTES_AT, pixels * 4)
        return cls(shm, owner=True)

    def write(self, frame: np.ndarray) -> bool:
        h, w = frame.shape[:2]
        size = w * h * 4
        if size > self.capacity:
            return False
        seq = struct.unpack_from("<Q", self.buf, 0)[0] + 1
        slot = seq % 2
        start = HEADER + slot * self.capacity
        self.buf[start:start + size] = np.ascontiguousarray(frame, dtype=np.uint8).reshape(-1).data
        struct.pack_into("<IIII", self.buf, 8, w, h, slot, 0)
        struct.pack_into("<Q", self.buf, 0, seq)
        return True

    def mark_on_card(self, w: int, h: int, texture: int) -> None:
        """A picture drawn on the card into texture number ``texture`` (shared as "<name>-tex<n>", see
        app/browser_quick.py): the engine copies it there; nothing is in the slots."""
        seq = struct.unpack_from("<Q", self.buf, 0)[0] + 1
        struct.pack_into("<IIII", self.buf, 8, w, h, ON_CARD, texture)
        struct.pack_into("<Q", self.buf, 0, seq)

    def on_card(self) -> bool:
        """The newest picture is on the card (the engine takes it there), not in the slots."""
        return struct.unpack_from("<Q", self.buf, 0)[0] != 0 and struct.unpack_from("<I", self.buf, 16)[0] == ON_CARD

    def painted(self) -> bool:
        """A picture newer than the last time this was asked (taken or not)."""
        seq = struct.unpack_from("<Q", self.buf, 0)[0]
        new, self.peeked = seq != self.peeked and seq != 0, seq
        return new

    def read(self) -> np.ndarray | None:
        """The newest picture if there's one SprigCast hasn't taken, else None."""
        seq = struct.unpack_from("<Q", self.buf, 0)[0]
        if seq == self.seen or seq == 0:
            return None
        w, h, slot, _ = struct.unpack_from("<IIII", self.buf, 8)
        size = w * h * 4
        if not w or not h or size > self.capacity or slot > 1:
            return None  # on the card (slot 2): not in memory; the helper sends one when asked (want_memory)
        start = HEADER + slot * self.capacity
        frame = np.frombuffer(self.buf, dtype=np.uint8, count=size, offset=start).reshape(h, w, 4).copy()
        if struct.unpack_from("<Q", self.buf, 0)[0] > seq + 1:
            return None  # overwritten while copied: the next one comes in a moment
        self.seen = seq
        return frame

    def close(self) -> None:
        self.buf = None
        try:
            self.shm.close()
        except BufferError:  # a view still out: the block goes when the process does
            pass
        if self.owner:
            try:
                self.shm.unlink()
            except FileNotFoundError:
                pass


def _write_message(stream, lock: threading.Lock, header: dict, payload: bytes = b"") -> None:
    data = json.dumps(header).encode()
    with lock:
        stream.write(struct.pack("<II", len(data), len(payload)) + data + payload)
        stream.flush()


def _read_message(stream) -> tuple[dict, bytes]:
    head = stream.read(8)
    if len(head) < 8:
        raise EOFError
    n, m = struct.unpack("<II", head)
    data = stream.read(n)
    payload = stream.read(m) if m else b""
    if len(data) < n or len(payload) < m:
        raise EOFError
    return json.loads(data), payload


def helper_command() -> list[str]:
    """This Python running serve(), with SprigCast's import paths (the installed app has no -m entry)."""
    code = ("import sys; sys.path[:0] = [p for p in sys.argv[1:] if p]; "
            "from sprigcast.app.browser_helper import serve; serve()")
    return [sys.executable, "-I", "-c", code, *[p for p in sys.path if p and Path(p).is_dir()]]


class RemoteHost:
    """The pages, in the helper. Same calls as app/browser_host.BrowserHost (sources/browser.Host); open, close,
    pause, take and read_audio may come from any thread."""

    def __init__(self, command: list[str] | None = None):
        self.command = command or helper_command()
        self.state_of = None  # callable: what pages see in window.sprigcast (set by the window)
        self._state: dict[str, Any] = {}
        self._wanted: dict[str, dict] = {}  # source id -> settings of each open page
        self._paused: dict[str, bool] = {}
        self._frames: dict[str, FrameSlot] = {}
        self._sounds: dict[str, Any] = {}  # source id -> PageSound (fed from the pipe, read by the mixer)
        self._notes: dict[str, str] = {}  # source id -> what its page said is wrong (a video that can't play)
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._outbox: list[tuple[Any, bytes]] = []  # (helper process, line) waiting for the sending thread
        self._outbox_ready = threading.Condition(self._send_lock)
        self._sender: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._empty_since: float | None = None
        self._linger_timer: threading.Timer | None = None
        self._shut = False
        self._timer = None
        self.starts = 0  # helpers started (a test, and the log, can tell a restart)
        self.card: tuple[int, int] | None = None  # the native engine's card (use_card): pages are drawn on it
        self._started_card: tuple[int, int] | None = None  # the card the running helper was given
        self._asked: dict[str, float] = {}  # source id -> when pictures in memory were last asked for
        from PySide6.QtCore import QCoreApplication, QTimer

        if QCoreApplication.instance() is not None and threading.current_thread() is threading.main_thread():
            # The window's state, looked at once a second (nothing to do while no page is open)
            self._timer = QTimer()
            self._timer.timeout.connect(self._check_state)
            self._timer.start(1000)

    # The helper ------------------------------------------------------------------------------

    def _ensure(self) -> bool:
        with self._lock:
            if self._shut:
                return False
            if self.proc is not None and self.proc.poll() is None:
                return True
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            env = dict(os.environ)
            env.pop(CARD_ENV, None)
            if self.card is not None:
                env[CARD_ENV] = f"{self.card[0]},{self.card[1]}"
            try:
                self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True, env=env)
            except OSError as exc:
                log.warning("browser helper didn't start: %s", exc)
                self.proc = None
                return False
            self.starts += 1
            self._started_card = self.card
            from sprigcast.core import pacing

            pacing.helper_processes["page helper"] = self._pid  # its memory goes in the log next to SprigCast's
            log.info("browser helper started (pid %s%s)", self.proc.pid,
                     ", pages drawn on the engine's card" if self.card is not None else "")
            self._reader = threading.Thread(target=self._read, args=(self.proc,), name="sprigcast-browser-pipe",
                                            daemon=True)
            self._reader.start()
            if self._state:
                self._send({"op": "state", "state": self._state})
            return True

    def _send(self, message: dict) -> None:
        """Queued for the sending thread: written here, a helper still starting (not reading yet) held the window
        on a full pipe ("the window didn't answer for 407 ms; browser_helper.py _send <- _check_state", PC
        2026-11-21)."""
        proc = self.proc
        if proc is None or proc.stdin is None:
            return
        line = (json.dumps(message) + "\n").encode()
        with self._outbox_ready:
            self._outbox.append((proc, line))
            if self._sender is None or not self._sender.is_alive():
                self._sender = threading.Thread(target=self._send_all, name="sprigcast-browser-send", daemon=True)
                self._sender.start()
            self._outbox_ready.notify()

    def _send_all(self) -> None:
        while True:
            with self._outbox_ready:
                if not self._outbox:
                    self._outbox_ready.wait(30.0)
                    if not self._outbox:
                        self._sender = None
                        return  # idle a while: a new one starts with the next message
                batch, self._outbox = self._outbox, []
            for proc, line in batch:
                try:
                    proc.stdin.write(line)
                    proc.stdin.flush()
                except (OSError, ValueError, AttributeError):
                    pass  # it stopped: the pipe reader starts it again

    def _pid(self) -> int | None:
        proc = self.proc
        return proc.pid if proc is not None and proc.poll() is None else None

    def _read(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout
        try:
            while True:
                header, payload = _read_message(stream)
                self._on_message(header, payload)
        except (EOFError, OSError, ValueError):
            pass
        proc.wait()
        for pipe in (proc.stdout, proc.stdin):
            try:
                pipe.close()
            except (OSError, ValueError, AttributeError):
                pass
        with self._lock:
            if self.proc is not proc:
                return
            self.proc = None
            for slot in self._frames.values():
                slot.close()
            self._frames.clear()
            again = bool(self._wanted) and not self._shut
        if again:
            log.warning("browser helper stopped (code %s): starting it again", proc.returncode)
            time.sleep(RESTART_AFTER_S)
            with self._lock:
                pages = list(self._wanted.items())
                if not pages or self._shut or not self._ensure():
                    return
                for source_id, settings in pages:
                    self._send({"op": "open", "id": source_id, "settings": settings})
                    if self._paused.get(source_id):
                        self._send({"op": "pause", "id": source_id, "paused": True})

    def _on_message(self, header: dict, payload: bytes) -> None:
        kind, source_id = header.get("kind"), header.get("id", "")
        if kind == "frames":
            try:
                slot = FrameSlot(_attach(header["name"]), owner=False)
            except (OSError, ValueError, KeyError) as exc:
                log.warning("browser source %s: can't read its pictures (%s)", source_id, exc)
                return
            with self._lock:
                old = self._frames.pop(source_id, None)
                if source_id in self._wanted:
                    self._frames[source_id] = slot
                else:
                    slot.close()
            if old is not None:
                old.close()
        elif kind == "sound":
            sound = self._sounds.get(source_id)
            if sound is not None and payload:
                sound.ring.write(np.frombuffer(payload, dtype=np.float32).reshape(-1, 2).copy())
        elif kind == "sound_on":
            self._set_sound(source_id, bool(header.get("on")))
        elif kind == "desktop":
            sound = self._sounds.get(source_id)
            if sound is not None:
                if not sound.on_desktop_reason:
                    log.info("browser source %s: its sound plays on desktop audio, so SprigCast can't mix or "
                             "measure it (%s)", source_id, header.get("reason", ""))
                sound.on_desktop_reason = str(header.get("reason", ""))
        elif kind == "note":
            with self._lock:
                if header.get("text"):
                    self._notes[source_id] = str(header["text"])
                else:
                    self._notes.pop(source_id, None)
        elif kind == "log":
            # The text as the message itself: the log's flood guard counts each kind of message (logbook.Floods), and
            # with one template every helper line was one kind, so busy pages crowded the rest out of today's log.
            log.log(int(header.get("level", logging.INFO)), "browser helper: " + str(header.get("text", "")).replace("%", "%%"))

    def use_card(self, luid: tuple[int, int] | None) -> None:
        """The native engine draws on this card (None: no engine): pages are drawn on it from now on. A helper
        already running without it starts again (in the background) with the same pages."""
        with self._lock:
            luid = tuple(luid) if luid is not None else None
            if luid == self.card:
                return
            self.card = luid
            proc = self.proc
            if luid is None or proc is None or proc.poll() is not None or self._started_card == luid:
                return
            self.proc = None  # its pipe reader sees it isn't the helper any more and doesn't start it again
            for slot in self._frames.values():
                slot.close()
            self._frames.clear()

        def again() -> None:
            self._quit(proc)  # first: two helpers can't share the saved profile
            with self._lock:
                if not self._wanted or self._shut or not self._ensure():
                    return
                for source_id, settings in list(self._wanted.items()):
                    self._send({"op": "open", "id": source_id, "settings": settings})
                    if self._paused.get(source_id):
                        self._send({"op": "pause", "id": source_id, "paused": True})

        log.info("browser helper: starting again to draw pages on the engine's card")
        threading.Thread(target=again, name="sprigcast-browser-card", daemon=True).start()

    def _set_sound(self, source_id: str, on: bool) -> None:
        if not on:
            self._sounds.pop(source_id, None)
            return
        from sprigcast.app.browser_audio import PageSound

        self._sounds[source_id] = PageSound()

    def _linger(self) -> None:
        """Called on every close: once no page is left for LINGER_S, the helper ends."""
        with self._lock:
            if self._wanted:
                self._empty_since = None
                return
            self._empty_since = self._empty_since or time.monotonic()
            if self._linger_timer is not None and self._linger_timer.is_alive():
                return  # one waiting thread at a time (bloom switches close and open pages often)
            self._linger_timer = threading.Timer(LINGER_S + 0.1, self._end_if_idle)
            self._linger_timer.daemon = True
            self._linger_timer.start()

    def _end_if_idle(self) -> None:
        with self._lock:
            self._linger_timer = None
            if self._wanted or self._empty_since is None:
                return
            left = LINGER_S - (time.monotonic() - self._empty_since)
            if left > 0:  # a page opened and closed again meanwhile: wait the rest
                self._linger_timer = threading.Timer(left + 0.1, self._end_if_idle)
                self._linger_timer.daemon = True
                self._linger_timer.start()
                return
            proc, self.proc = self.proc, None
            self._empty_since = None
        if proc is not None:
            log.info("browser helper ended (no pages for %.0f s)", LINGER_S)
            self._quit(proc)

    def _quit(self, proc: subprocess.Popen) -> None:
        try:
            proc.stdin.write(b'{"op": "quit"}\n')
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    # sources/browser.Host --------------------------------------------------------------------

    def open(self, source_id: str, settings: dict[str, Any]) -> None:
        with self._lock:
            self._wanted[source_id] = dict(settings)
            self._empty_since = None
            if not self._ensure():
                return
        self._send({"op": "open", "id": source_id, "settings": dict(settings)})

    def close(self, source_id: str) -> None:
        with self._lock:
            self._wanted.pop(source_id, None)
            self._paused.pop(source_id, None)
            self._sounds.pop(source_id, None)
            self._notes.pop(source_id, None)
            self._asked.pop(source_id, None)
            slot = self._frames.pop(source_id, None)
        if slot is not None:
            slot.close()
        self._send({"op": "close", "id": source_id})
        self._linger()

    def pause(self, source_id: str, paused: bool) -> None:
        self._paused[source_id] = bool(paused)
        self._send({"op": "pause", "id": source_id, "paused": bool(paused)})

    def shared_name(self, source_id: str) -> str | None:
        """The shared memory block a page's pictures are in (the native engine reads them there itself)."""
        slot = self._frames.get(source_id)
        return slot.shm.name if slot is not None and slot.buf is not None else None

    def painted(self, source_id: str) -> bool:
        """The page drew a picture since this was last asked (without taking it)."""
        slot = self._frames.get(source_id)
        try:
            return slot is not None and slot.painted()
        except (TypeError, ValueError):  # closed just now
            return False

    def take(self, source_id: str) -> np.ndarray | None:
        slot = self._frames.get(source_id)
        if slot is None:
            return None
        try:
            frame = slot.read()
            if frame is None and slot.on_card():
                self._ask_memory(source_id)
            return frame
        except (TypeError, ValueError):  # closed just now
            return None

    def _ask_memory(self, source_id: str) -> None:
        """SprigCast draws this page itself for now (the engine isn't taking it): pictures in memory, please."""
        now = time.monotonic()
        if now - self._asked.get(source_id, 0.0) >= MEMORY_ASK_S:
            self._asked[source_id] = now
            self._send({"op": "memory", "id": source_id})

    def read_audio(self, source_id: str, n: int) -> np.ndarray | None:
        sound = self._sounds.get(source_id)
        if sound is None or sound.on_desktop_reason:
            return None
        return sound.read(n)

    def sound_note(self, source_id: str) -> str:
        sound = self._sounds.get(source_id)
        return "plays on desktop audio" if sound is not None and sound.on_desktop_reason else ""

    def page_note(self, source_id: str) -> str:
        """What the page said is wrong (a video that can't play), for the source's status."""
        return self._notes.get(source_id, "")

    def reload(self, source_id: str) -> None:
        self._send({"op": "reload", "id": source_id})

    def interact(self, source_id: str, settings: dict[str, Any], title: str, parent=None):
        """The page on screen to click in. It's the helper's window, so Windows is told it may come forward."""
        if source_id not in self._wanted:
            self.open(source_id, settings)
        elif not self._ensure():
            return None
        if sys.platform == "win32" and self.proc is not None:
            try:
                import ctypes

                ctypes.windll.user32.AllowSetForegroundWindow(self.proc.pid)
            except Exception:  # noqa: BLE001
                pass
        self._send({"op": "interact", "id": source_id, "settings": dict(settings), "title": title})
        return None

    # What pages see in window.sprigcast ------------------------------------------------------

    def _check_state(self) -> None:
        if not self._wanted or self.state_of is None:
            return
        try:
            now = self.state_of()
        except Exception:  # noqa: BLE001
            log.debug("page state", exc_info=True)
            return
        if now != self._state:
            self._state = now
            self._send({"op": "state", "state": now})

    def shutdown(self) -> None:
        with self._lock:
            self._shut = True
            proc, self.proc = self.proc, None
            for slot in self._frames.values():
                slot.close()
            self._frames.clear()
            self._wanted.clear()
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._linger_timer is not None:
            self._linger_timer.cancel()
        if proc is not None:
            self._quit(proc)
        from sprigcast.sources import browser

        browser.set_host(None)


class SharedPageDevice:
    """The graphics device all web pages draw with: one, made once by a 2x2 target never shown that keeps it. Each
    page made its own before: ~50 graphics driver threads and its share of the card's memory each (PC 2026-11-21:
    162 driver threads, 61 pages). SPRIGCAST_PAGE_DEVICE_EACH=1 goes back to one each."""

    def __init__(self, card, make_target):
        self.card, self.make_target = card, make_target
        self.holder = None
        self.tried = False

    def __call__(self) -> int | None:
        if os.environ.get("SPRIGCAST_PAGE_DEVICE_EACH"):
            return None
        if not self.tried:
            self.tried = True
            try:
                self.holder = self.make_target(self.card, 2, 2, f"sprigcast-pages-{os.getpid()}-holder")
                log.info("web pages share one graphics device")
            except Exception as exc:  # noqa: BLE001 - each page makes its own, as before
                log.warning("web pages: one shared graphics device couldn't be made (%s): one each", exc)
        return self.holder.device if self.holder is not None else None


# The helper process -----------------------------------------------------------------------------


class _PipeLog(logging.Handler):
    def __init__(self, emit):
        super().__init__(logging.INFO)
        self._emit = emit

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emit({"kind": "log", "level": record.levelno, "text": record.getMessage()})
        except Exception:  # noqa: BLE001
            pass


def _card_from_env() -> tuple[int, int] | None:
    """The engine's card from SprigCast (CARD_ENV), with Qt Quick set up to draw on it; None when pages are drawn
    in memory (no native engine, not Windows, or Qt Quick's WebEngine missing). Before the QApplication."""
    text = os.environ.get(CARD_ENV, "")
    if not text:
        return None
    try:
        low, high = (int(part) for part in text.split(","))
        from sprigcast.app import browser_quick

        if not browser_quick.available():
            log.info("browser pages drawn in memory: Qt Quick's WebEngine isn't here")
            return None
        browser_quick.prepare()
        return low, high
    except Exception as exc:  # noqa: BLE001 - memory pages still work
        log.warning("browser pages drawn in memory: %s", exc)
        return None


class PageBlocks:
    """The helper's shared memory blocks, one per page, each announced to SprigCast ("frames") when it is made.

    A block goes when its page does (BrowserHost.page_closed), not when the close is asked for. Pages are opened
    and closed a moment after the ask (a queued call), so a block let go at the ask could be made again by the
    page still there, or by an open that came just before the close. That block was never let go, the page's
    next opening used it without announcing it, and SprigCast never learned where the page's pictures were: the
    engine took none, the helper made a new texture every second, and the page stayed empty for good ("[Art]
    Gameplay" a checkerboard on Live, 2026-11-19)."""

    def __init__(self, emit):
        self.slots: dict[str, FrameSlot] = {}
        self.made = 0
        self.emit = emit

    def slot_for(self, source_id: str, pixels: int) -> FrameSlot:
        slot = self.slots.get(source_id)
        if slot is None or pixels * 4 > slot.capacity:
            if slot is not None:
                slot.close()
            self.made += 1
            name = f"sprigcast-{os.getpid()}-{self.made}"
            slot = self.slots[source_id] = FrameSlot.create(name, pixels)
            self.emit({"kind": "frames", "id": source_id, "name": name})
        return slot

    def drop(self, source_id: str) -> None:
        slot = self.slots.pop(source_id, None)
        if slot is not None:
            slot.close()

    def close(self) -> None:
        for source_id in list(self.slots):
            self.drop(source_id)


def serve() -> None:
    """The helper: the pages, drawn with Qt WebEngine on this process's own window thread."""
    from PySide6.QtCore import QCoreApplication, QObject, Qt, QTimer, Signal
    from PySide6.QtWidgets import QApplication

    from sprigcast.app import browser_host

    out = sys.stdout.buffer
    sys.stdout = sys.stderr  # nothing else may write into the message pipe
    out_lock = threading.Lock()

    def emit(header: dict, payload: bytes = b"") -> None:
        try:
            _write_message(out, out_lock, header, payload)
        except (OSError, ValueError):
            pass

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(_PipeLog(emit))
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = browser_host.chromium_flags(
        os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""))
    card = _card_from_env()
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication([sys.argv[0] if sys.argv else "sprigcast-browser"])
    app.setOrganizationName("Slothbloom")  # the same saved profile (cookies, local storage) as before
    app.setApplicationName("SprigCast")
    app.setQuitOnLastWindowClosed(False)
    host = browser_host.BrowserHost()
    host.state_of = lambda: host_state[0]
    host_state: list[dict] = [{}]
    blocks = PageBlocks(emit)
    slots, slot_for = blocks.slots, blocks.slot_for
    host.page_closed = blocks.drop  # with the page, in the page's own order (see PageBlocks)
    textures = [0]

    def store(source_id: str, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        slot_for(source_id, w * h).write(frame)

    host.store = store

    if card is not None:
        from sprigcast import sprigcore

        one_device = SharedPageDevice(card, sprigcore.PageTarget)

        def target_for(source_id: str, w: int, h: int, device: int | None = None):
            """A page's texture on the engine's card, named after its block (the engine opens it by that)."""
            slot = slot_for(source_id, w * h)  # room for its pictures in memory too (want_memory)
            if device is None:
                device = one_device()
            textures[0] += 1
            target = sprigcore.PageTarget(card, w, h, f"{slot.shm.name}-tex{textures[0]}", device)
            target.number, target.block = textures[0], slot.shm.name
            return target

        def card_drawn(source_id: str, target) -> None:
            slot = slots.get(source_id)
            if slot is not None and slot.shm.name == target.block:
                slot.mark_on_card(target.size[0], target.size[1], target.number)

        host.card, host.target_for, host.card_drawn = card, target_for, card_drawn
    real_set_sound = host.set_sound

    def set_sound(source_id: str, sound) -> None:
        real_set_sound(source_id, sound)
        emit({"kind": "sound_on", "id": source_id, "on": sound is not None})
        if sound is not None:
            def desktop(reason, sound=sound, source_id=source_id, before=sound.on_desktop):
                emit({"kind": "desktop", "id": source_id, "reason": reason})
                if before is not None:
                    before(reason)
            sound.on_desktop = desktop

    host.set_sound = set_sound
    real_set_note = host.set_note

    def set_note(source_id: str, text: str) -> None:
        real_set_note(source_id, text)
        emit({"kind": "note", "id": source_id, "text": text})

    host.set_note = set_note

    def send_sound() -> None:
        for source_id, sound in list(host._sounds.items()):
            n = len(sound.ring)
            if n:
                block = np.ascontiguousarray(sound.ring.read(n), dtype=np.float32)
                emit({"kind": "sound", "id": source_id}, block.tobytes())

    sound_timer = QTimer()
    sound_timer.timeout.connect(send_sound)
    sound_timer.start(SOUND_MS)

    class Inbox(QObject):
        got = Signal(object)

    inbox = Inbox()

    def handle(message: dict) -> None:
        op, source_id = message.get("op"), message.get("id", "")
        if op == "quit":
            app.quit()
        elif op == "open":
            host.open(source_id, message.get("settings") or {})
        elif op == "close":
            host.close(source_id)  # its block goes when the page does (host.page_closed)
        elif op == "pause":
            host.pause(source_id, bool(message.get("paused")))
        elif op == "reload":
            host.reload(source_id)
        elif op == "memory":
            host.want_memory(source_id)
        elif op == "state":
            host_state[0] = message.get("state") or {}
            host._check_state()
        elif op == "interact":
            window = host.interact(source_id, message.get("settings") or {}, message.get("title", "Browser"))
            if window is not None:
                window.raise_()
                window.activateWindow()

    inbox.got.connect(handle, Qt.ConnectionType.QueuedConnection)

    def listen() -> None:
        for line in sys.stdin.buffer:
            try:
                inbox.got.emit(json.loads(line))
            except ValueError:
                continue
        inbox.got.emit({"op": "quit"})  # SprigCast closed (or stopped): the pages go with it

    threading.Thread(target=listen, name="browser-helper-inbox", daemon=True).start()
    try:
        app.exec()
    finally:
        sound_timer.stop()
        host.shutdown()
        blocks.close()
