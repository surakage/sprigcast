"""Let a plugin hear a sound SprigCast is already mixing, without opening the device again.

    from sprigcast.audio import listen

    listen.add("mic-1", on_block)     # on_block(block): float32 (1024, 2) at 48 kHz, after filters and volume
    listen.add(listen.MIX, on_block)  # the stream mix: everything viewers hear
    listen.remove("mic-1", on_block)
    listen.sounds()                   # [(id, name), ...] of the sounds being mixed now, A-Z

on_block runs on the mixer's thread about 47 times a second: copy what you need and return (well under
a millisecond). Listen only while you need it (on air) and remove it straight after: with no listeners
the mixer does nothing extra.
"""

from __future__ import annotations

import threading
from typing import Callable

import numpy as np

MIX = "mix"  # the stream mix, everything viewers hear

# Sound id (or MIX) -> its listeners. Swapped whole, never changed in place, so the mixer reads it without a lock.
LISTENERS: dict[str, tuple[Callable[[np.ndarray], None], ...]] = {}
# (id, name) of every sound the mixer has now, set by the engine whenever sources change.
SOUNDS: list[tuple[str, str]] = []
_lock = threading.Lock()


def add(sound_id: str, fn: Callable[[np.ndarray], None]) -> None:
    global LISTENERS
    with _lock:
        have = LISTENERS.get(sound_id, ())
        if fn not in have:
            LISTENERS = {**LISTENERS, sound_id: (*have, fn)}


def remove(sound_id: str, fn: Callable[[np.ndarray], None]) -> None:
    global LISTENERS
    with _lock:
        have = tuple(f for f in LISTENERS.get(sound_id, ()) if f != fn)
        out = {k: v for k, v in LISTENERS.items() if k != sound_id}
        if have:
            out[sound_id] = have
        LISTENERS = out


def listeners() -> dict[str, tuple[Callable[[np.ndarray], None], ...]]:
    return LISTENERS


def sounds() -> list[tuple[str, str]]:
    return list(SOUNDS)


def set_sounds(pairs: list[tuple[str, str]]) -> None:
    global SOUNDS
    SOUNDS = sorted(pairs, key=lambda p: p[1].lower())


def hand(fns: tuple[Callable[[np.ndarray], None], ...], block: np.ndarray) -> None:
    """Give a block to each listener; one that fails is dropped, never the mixer."""
    import logging

    for fn in fns:
        try:
            fn(block)
        except Exception:
            logging.getLogger(__name__).exception("a sound listener failed; removing it")
            for key, have in list(LISTENERS.items()):
                if fn in have:
                    remove(key, fn)
