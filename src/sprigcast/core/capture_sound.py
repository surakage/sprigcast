"""A game or window capture's own sound, the way OBS's "Capture audio" works.

The sound is an ordinary sound source next to the capture (so it has a row on the Sound card, its
volume, filters and tracks), linked by `capture_of` and named after the capture:

- "app": the captured app's own sound only (process loopback, sources/app_audio.py), following its exe;
- a device name: what plays on that output, e.g. "Game (Elgato Virtual Audio)" when the streamer
  routes the game to Elgato Wave Link's Game channel;
- None: no sound of its own (desktop sound still carries it, as before).
"""

from __future__ import annotations

from sprigcast.core.scene import SceneCollection, SourceConfig

APP = "app"
CAPTURE_KINDS = ("window_capture", "game_capture")


def capture_sound(collection: SceneCollection, capture_id: str) -> SourceConfig | None:
    return next((s for s in collection.sources if s.settings.get("capture_of") == capture_id), None)


def sound_choice(collection: SceneCollection, capture_id: str) -> str | None:
    """APP, a device name, or None."""
    sound = capture_sound(collection, capture_id)
    if sound is None:
        return None
    return APP if sound.kind == "app_audio" else str(sound.settings.get("device", "default"))


def set_capture_sound(collection: SceneCollection, capture: SourceConfig, choice: str | None) -> SourceConfig | None:
    """Give the capture its own sound (APP or a device name), or take it away (None). Returns the sound."""
    if collection.audio.global_sources is None:  # pin the global devices before a new desktop sound joins
        collection.audio.global_sources = collection.global_device_ids()
    current = capture_sound(collection, capture.id)
    kept = {k: v for k, v in (current.settings if current else {}).items()
            if k in ("volume_db", "muted", "monitor", "mono", "balance", "delay_ms", "tracks", "vod", "track4",
                     "track5")}  # what you set on the sound survives a change of where it comes from
    if current is not None:
        collection.sources = [s for s in collection.sources if s.id != current.id]
    if choice is None:
        return None
    base = {"capture_of": capture.id, "group": "Game", **kept}
    if choice == APP:
        process = str(capture.settings.get("process", "")).strip()
        sound = SourceConfig(id=current.id if current else collection.new_id("sound"), kind="app_audio",
                             name=f"{capture.name} sound",
                             settings={**base, "preset": "custom", "apps": [process] if process else [],
                                       "auto_game": not process})
    else:
        sound = SourceConfig(id=current.id if current else collection.new_id("sound"), kind="audio_output",
                             name=f"{capture.name} sound", settings={**base, "device": choice})
    collection.sources.append(sound)
    return sound


def follow_capture(collection: SceneCollection, capture: SourceConfig) -> None:
    """After the capture changes window or name: its own app sound follows the new app and name."""
    sound = capture_sound(collection, capture.id)
    if sound is None:
        return
    sound.name = f"{capture.name} sound"
    if sound.kind == "app_audio":
        process = str(capture.settings.get("process", "")).strip()
        sound.settings = {**sound.settings, "apps": [process] if process else [], "auto_game": not process}
