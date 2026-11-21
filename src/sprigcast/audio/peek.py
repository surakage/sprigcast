"""Let the Filters window hear a sound as it comes in, before its filters, while the window is open.

    from sprigcast.audio import peek

    peek.add("mic-1", on_block)     # on_block(block): float32 (1024, 2) at 48 kHz, before filters and volume
    peek.remove("mic-1", on_block)

The window runs its own draft of the filters on these blocks, so you hear a change before it reaches the stream
(user 2026-11-21: nothing changes until Apply). on_block runs on the mixer's thread about 47 times a second: copy
what you need and return. Unlike listen.py this never moves a sound off the engine: the engine hands it from a
tap entry (SC_MIXER_SOURCE_RAW_TAP + the sound), or Python does when Python runs the sound's filters. With nobody
watching the mixer does nothing extra.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

Watcher = Callable[[np.ndarray], None]

# Sound id -> who watches it. Swapped whole, never changed in place, so the mixer reads it without a lock.
WATCHERS: dict[str, tuple[Watcher, ...]] = {}
_lock = threading.Lock()


def add(sound_id: str, fn: Watcher) -> None:
    global WATCHERS
    with _lock:
        have = WATCHERS.get(sound_id, ())
        if fn not in have:
            WATCHERS = {**WATCHERS, sound_id: (*have, fn)}


def remove(sound_id: str, fn: Watcher) -> None:
    global WATCHERS
    with _lock:
        have = tuple(f for f in WATCHERS.get(sound_id, ()) if f != fn)
        out = {k: v for k, v in WATCHERS.items() if k != sound_id}
        if have:
            out[sound_id] = have
        WATCHERS = out


def hand(fns: tuple[Watcher, ...], block: np.ndarray) -> None:
    for fn in fns:
        try:
            fn(block)
        except Exception:  # noqa: BLE001 - a watcher's problem never stops the sound
            log.exception("sound peek failed")
