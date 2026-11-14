"""Mixes every audio source into one 48 kHz stereo track and measures levels.

Runs on its own thread, one 1024-sample block (about 21 ms) at a time, paced by the clock.
Every mixed block is handed to each tap (recordings and streams).

Two extras on top of the plain mix:

- Ducking: while a voice source (your mic) is above a threshold, every other source glides down
  by a set amount, and glides back up a moment after you stop talking.
- Monitoring: sources marked "monitor" are also summed into a second mix for your speakers. That
  mix never reaches the stream; the stream always gets every unmuted source.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from sprigcast.sources.audio import SAMPLE_RATE

log = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class Ducking:
    """How other sources make room for your voice."""

    enabled: bool = False
    amount_db: float = -14.0  # how far the others go down while you talk
    threshold_db: float = -38.0  # voice level (RMS dBFS, after volume) that counts as talking
    attack_ms: float = 250.0  # how quickly the others fade down
    release_ms: float = 900.0  # how quickly they come back up
    hold_ms: float = 400.0  # how long to wait after you stop before coming back up (no pumping between words)


def is_voice(source) -> bool:
    """Whether a source leads ducking: mics do unless switched off ("duck_voice": False)."""
    settings = getattr(source, "settings", {}) or {}
    return bool(settings.get("duck_voice", getattr(source, "kind", "") == "audio_input"))


def is_monitored(source) -> bool:
    """True for "me + stream" and "just me" (monitor: True or "only")."""
    return bool((getattr(source, "settings", {}) or {}).get("monitor", False))


def is_monitor_only(source) -> bool:
    """'Just me': on your speakers, never in the stream or recordings."""
    return (getattr(source, "settings", {}) or {}).get("monitor") == "only"


MAX_DELAY_MS = 2000


def shape(block: np.ndarray, settings: dict, pending: dict, key: str) -> np.ndarray:
    """Per-source mono, left/right balance and delay (sync offset), after the filters.

    settings: "mono" (bool), "balance" (-1 left .. 0 centre .. 1 right), "delay_ms" (0..2000).
    pending[key] holds the delayed samples between blocks."""
    if settings.get("mono"):
        both = block.mean(axis=1, keepdims=True)
        block = np.repeat(both, 2, axis=1)
    balance = max(-1.0, min(1.0, float(settings.get("balance", 0.0) or 0.0)))
    if balance:
        block = block * np.array([min(1.0, 1.0 - balance), min(1.0, 1.0 + balance)], dtype=np.float32)
    delay = int(max(0, min(MAX_DELAY_MS, int(settings.get("delay_ms", 0) or 0))) * SAMPLE_RATE / 1000)
    if delay:
        held = pending.get(key)
        if held is None or len(held) != delay:
            held = np.zeros((delay, 2), dtype=np.float32)  # a new delay starts with silence
        joined = np.concatenate([held, block.astype(np.float32, copy=False)])
        block, pending[key] = joined[: len(block)], joined[len(block):]
    else:
        pending.pop(key, None)
    return block


class Mixer:
    def __init__(self, report: Callable[[dict[str, tuple[float, float]]], None]):
        self.report = report
        self.ducking = Ducking()
        self.duck_gain = 1.0  # current gain on non-voice sources, 1.0 = not ducked
        self._hold_left = 0.0
        self._monitor_taps: list[Callable[[np.ndarray], None]] = []
        self._taps: list[Callable[[np.ndarray], None]] = []
        self._sources: list = []
        self._delays: dict[str, np.ndarray] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sprigcast-audio", daemon=True)

    def set_sources(self, sources: list) -> None:
        """Called by the engine whenever sources change. Only sources with read_block() are mixed."""
        with self._lock:
            self._sources = [s for s in sources if hasattr(s, "read_block")]

    def add_tap(self, callback: Callable[[np.ndarray], None]) -> None:
        """Hand every mixed block, float32 (BLOCK, 2), to callback on the mixer thread. It must not block."""
        with self._lock:
            self._taps = [*self._taps, callback]

    def remove_tap(self, callback: Callable[[np.ndarray], None]) -> None:
        with self._lock:
            self._taps = [t for t in self._taps if t != callback]

    def set_ducking(self, ducking: Ducking) -> None:
        self.ducking = ducking

    def add_monitor_tap(self, callback: Callable[[np.ndarray], None]) -> None:
        """Hand the monitor mix (monitored sources only) to callback on the mixer thread."""
        with self._lock:
            self._monitor_taps = [*self._monitor_taps, callback]

    def remove_monitor_tap(self, callback: Callable[[np.ndarray], None]) -> None:
        with self._lock:
            self._monitor_taps = [t for t in self._monitor_taps if t != callback]

    def _duck_ramp(self, voice_db: float) -> np.ndarray | float:
        """Per-sample gain for non-voice sources this block: a glide from the last gain toward the target."""
        d = self.ducking
        block_s = BLOCK / SAMPLE_RATE
        if d.enabled and voice_db > d.threshold_db:
            self._hold_left = d.hold_ms / 1000
        else:
            self._hold_left = max(0.0, self._hold_left - block_s)
        talking = d.enabled and self._hold_left > 0
        target = 10 ** (d.amount_db / 20) if talking else 1.0
        start = self.duck_gain
        if abs(target - start) < 1e-4:
            self.duck_gain = target
            return target
        tau = (d.attack_ms if target < start else d.release_ms) / 1000
        end = start + (target - start) * (1 - math.exp(-block_s / max(tau, 1e-3)))
        self.duck_gain = end
        return np.linspace(start, end, BLOCK, dtype=np.float32)[:, None]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def tick(self) -> np.ndarray:
        """Mix one block. Returns it and collects per-source levels (after gain and ducking)."""
        with self._lock:
            sources, taps, monitor_taps = list(self._sources), self._taps, self._monitor_taps
        processed, meters = [], {}
        voice_db = FLOOR_DB
        for source in sources:
            block = source.read_block(BLOCK)
            chain = getattr(source, "audio_chain", None)
            if chain is not None:
                try:
                    block = chain.process(block)
                except Exception:
                    log.exception("audio filters on %s failed; turning them off", source.id)
                    source.audio_chain = None
            settings = getattr(source, "settings", {}) or {}
            if settings.get("mono") or settings.get("balance") or settings.get("delay_ms") \
                    or source.id in self._delays:
                block = shape(block, settings, self._delays, source.id)
            gain = source.gain
            voice = is_voice(source)
            if voice and gain:
                voice_db = max(voice_db, levels(block * gain)[1])
            processed.append((source, block, gain, voice))
        duck = self._duck_ramp(voice_db)
        blocks, monitored = [], []
        for source, block, gain, voice in processed:
            if not voice and not (isinstance(duck, float) and duck == 1.0):
                block = block * duck
            if not is_monitor_only(source):
                blocks.append((block, gain))
            meters[source.id] = levels(block * gain) if gain else (FLOOR_DB, FLOOR_DB)
            if monitor_taps and gain and is_monitored(source):
                monitored.append((block, gain))
        mixed = mix(blocks)
        meters["master"] = levels(mixed)
        duck_db = to_db(self.duck_gain)
        meters["duck"] = (duck_db, duck_db)
        self._meters = meters
        for tap in taps:
            tap(mixed)
        if monitor_taps:
            monitor_mix = mix(monitored)
            for tap in monitor_taps:
                tap(monitor_mix)
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
