"""Which app is asking, so a sound change it makes is logged by name (PC, 2026-11-20: 38 web pages went silent
during the afternoon and the log couldn't say which connected app did it). The control server sets it around
each message; changes made in SprigCast's own window aren't logged here. Only sound settings are named, with
their old and new values: no keys, passwords or other settings."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger("sprigcast.control")

SOUND_KEYS = ("audio_to_mixer", "mute", "muted", "volume_db", "balance", "mono", "monitor", "sync_ms", "delay_ms",
              "device", "apps", "pids", "blooms", "group", "tracks", "vod")
_asking: list[str] = []


@contextmanager
def asking(name: str) -> Iterator[None]:
    """Requests handled inside come from this app."""
    _asking.append(name)
    try:
        yield
    finally:
        _asking.pop()


def asker(otherwise: str = "") -> str:
    """The app whose request is being handled now, or `otherwise` when the change comes from SprigCast's own
    window (a place switched off while live read "app stopped" whoever did it: the user's stream, 2026-11-27)."""
    return _asking[-1] if _asking else otherwise


def sound_changed(source: Any, before: dict[str, Any]) -> None:
    """One line naming the app, the source and each sound setting it changed (nothing when none did)."""
    after = source.settings
    changed = [f"{key} {before.get(key)!r} -> {after.get(key)!r}" for key in SOUND_KEYS
               if before.get(key) != after.get(key)]
    if changed:
        who = _asking[-1] if _asking else "an app"
        log.info("control: %s changed %s (%s) sound: %s", who, source.name, source.id, "; ".join(changed))
