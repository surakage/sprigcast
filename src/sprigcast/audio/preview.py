"""The Filters window's sound preview: a sound as it comes in, run through a draft of its filters.

User 2026-11-21: sound filters work like picture filters. You hear and see a change in the window while you adjust
it, and nothing changes on the stream until Apply. So the window never touches the sound's real filters: it hears
the sound before its filters (peek.py), runs its own copy of the filters it is editing on a worker thread, keeps
the last half second of both sides for the pictures, and plays Before or After on the Listen through device when
asked. It costs nothing until the window opens and stops when it closes.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable

import numpy as np

from sprigcast.audio import peek
from sprigcast.core.filters import AudioChain

log = logging.getLogger(__name__)

RATE = 48000
KEEP = RATE // 2  # the last half second of each side, for the waveform and the spectrum
FLOOR_DB = -90.0
LISTEN = ("off", "before", "after")


def peak_db(block: np.ndarray) -> float:
    peak = float(np.max(np.abs(block))) if len(block) else 0.0
    return max(FLOOR_DB, 20.0 * np.log10(peak)) if peak > 0 else FLOOR_DB


def rms_db(block: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) if len(block) else 0.0
    return max(FLOOR_DB, 20.0 * np.log10(rms)) if rms > 0 else FLOOR_DB


def _speakers(device: str | None):
    from sprigcast.audio.monitor import Monitor

    return Monitor(device or None)


class Preview:
    """start() to begin hearing, set_filters() on every edit, set_listen("off"|"before"|"after"), snapshot() for
    the pictures, stop() when the window closes. Blocks are worked on a thread of its own, never the mixer's."""

    def __init__(self, source_id: str, filters, device: str | None = None,
                 speakers: Callable[[str | None], object] = _speakers, threaded: bool = True):
        self.source_id = str(source_id)
        self.device = device
        self.listen = "off"
        self._speakers = speakers
        self._monitor = None  # audio/monitor.py's Monitor while listening
        self._chain = AudioChain(filters)
        self._lock = threading.Lock()
        self._queue: deque[np.ndarray] = deque(maxlen=24)  # about half a second, if the worker falls behind
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threaded = threaded
        self._thread: threading.Thread | None = None
        self.before = np.zeros(KEEP, np.float32)  # mono, newest last
        self.after = np.zeros(KEEP, np.float32)
        self.levels = (FLOOR_DB, FLOOR_DB, FLOOR_DB, FLOOR_DB)  # newest block: before peak, after peak, before rms, after rms
        self.blocks = 0

    # The window's side -------------------------------------------------------------------------------

    def start(self) -> None:
        peek.add(self.source_id, self._block)
        if self._threaded and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="sprigcast-filter-preview", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        peek.remove(self.source_id, self._block)
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._close_monitor()

    def set_filters(self, filters) -> None:
        """The filters as the window has them now (a draft: not the sound's real ones)."""
        with self._lock:
            if self._chain.matches(filters) or self._chain.update(filters):
                return
            self._chain = AudioChain(filters)

    def set_listen(self, mode: str) -> None:
        self.listen = mode if mode in LISTEN else "off"
        self._wake.set()

    def snapshot(self) -> tuple[np.ndarray, np.ndarray, tuple[float, float, float, float]]:
        with self._lock:
            return self.before.copy(), self.after.copy(), self.levels

    # The worker --------------------------------------------------------------------------------------

    def _block(self, block: np.ndarray) -> None:  # the mixer's thread: copy and go
        self._queue.append(np.array(block, dtype=np.float32, copy=True))
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.work()
            except Exception:  # noqa: BLE001 - never end the preview over one block
                log.exception("filter preview failed")

    def work(self) -> None:
        """Run what came in through the draft filters; play the side being listened to."""
        self._follow_listen()
        while self._queue:
            try:
                block = self._queue.popleft()
            except IndexError:
                break
            with self._lock:
                chain = self._chain
            try:
                after = chain.process(block) if chain.processors else block
            except Exception:  # noqa: BLE001 - a draft that fails is heard as it comes in
                log.exception("filter preview: the draft filters failed")
                after = block
            after = np.asarray(after, dtype=np.float32)
            levels = (peak_db(block), peak_db(after), rms_db(block), rms_db(after))
            with self._lock:
                self.before = np.concatenate((self.before[len(block):], block.mean(axis=1)))
                self.after = np.concatenate((self.after[len(after):], after.mean(axis=1)))
                self.levels = levels
                self.blocks += 1
            monitor = self._monitor
            if monitor is not None:
                monitor.write(block if self.listen == "before" else after)

    def _follow_listen(self) -> None:
        if self.listen != "off" and self._monitor is None:
            try:
                self._monitor = self._speakers(self.device)
                self._monitor.start()
            except Exception:  # noqa: BLE001 - nothing to listen through: the pictures still work
                log.exception("filter preview: can't open the Listen through device")
                self._monitor = None
                self.listen = "off"
        elif self.listen == "off" and self._monitor is not None:
            self._close_monitor()

    def _close_monitor(self) -> None:
        monitor, self._monitor = self._monitor, None
        if monitor is not None:
            monitor.stop()
