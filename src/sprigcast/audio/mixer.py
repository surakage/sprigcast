"""Mixes every audio source into one 48 kHz stereo track and measures levels.

Runs on its own thread, one 1024-sample block (about 21 ms) at a time, paced by the clock.
The mixed blocks wait in `output` for the encoder (M7); until then they are simply dropped.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from sprigcast.sources.audio import SAMPLE_RATE

BLOCK = 1024
FLOOR_DB = -60.0
LEVELS_EVERY = 3  # blocks between level reports (about 15 per second)


def to_db(value: float) -> float:
    return FLOOR_DB if value <= 10 ** (FLOOR_DB / 20) else max(FLOOR_DB, 20 * math.log10(value))


def levels(block: np.ndarray) -> tuple[float, float]:
    """(peak dBFS, RMS dBFS) of a block."""
    if not len(block):
        return FLOOR_DB, FLOOR_DB
    return to_db(float(np.max(np.abs(block)))), to_db(float(np.sqrt(np.mean(np.square(block)))))


def mix(blocks: list[tuple[np.ndarray, float]]) -> np.ndarray:
    """Sum (block, gain) pairs and clip to [-1, 1]."""
    out = np.zeros((BLOCK, 2), dtype=np.float32)
    for block, gain in blocks:
        if gain:
            out += block * gain
    np.clip(out, -1.0, 1.0, out=out)
    return out


class Mixer:
    def __init__(self, report: Callable[[dict[str, tuple[float, float]]], None]):
        self.report = report
        self.output: deque[np.ndarray] = deque(maxlen=int(SAMPLE_RATE / BLOCK))  # about 1 s
        self._sources: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sprigcast-audio", daemon=True)

    def set_sources(self, sources: list) -> None:
        """Called by the engine whenever sources change. Only sources with read_block() are mixed."""
        with self._lock:
            self._sources = [s for s in sources if hasattr(s, "read_block")]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def tick(self) -> np.ndarray:
        """Mix one block. Returns it and collects per-source levels (after gain)."""
        with self._lock:
            sources = list(self._sources)
        blocks, meters = [], {}
        for source in sources:
            block = source.read_block(BLOCK)
            gain = source.gain
            blocks.append((block, gain))
            meters[source.id] = levels(block * gain) if gain else (FLOOR_DB, FLOOR_DB)
        mixed = mix(blocks)
        meters["master"] = levels(mixed)
        self._meters = meters
        self.output.append(mixed)
        return mixed

    def _run(self) -> None:
        interval = BLOCK / SAMPLE_RATE
        next_at = time.perf_counter()
        count = 0
        while not self._stop.is_set():
            self.tick()
            count += 1
            if count % LEVELS_EVERY == 0:
                self.report(self._meters)
            next_at += interval
            delay = next_at - time.perf_counter()
            if delay > 0:
                self._stop.wait(delay)
            elif delay < -0.5:
                next_at = time.perf_counter()  # fell far behind (machine asleep): resync
