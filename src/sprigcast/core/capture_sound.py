"""A game or window capture's own sound, the way OBS's "Capture audio" works.

The sound is an ordinary sound source next to the capture (so it has a row on the Sound card, its
volume, filters and tracks), linked by `capture_of` and named after the capture:

- "app": the captured app's own sound only (process loopback, sources/app_audio.py), following its exe;
- a device name: what plays on that output, e.g. "Game (Elgato Virtual Audio)" when the streamer
  routes the game to Elgato Wave Link's Game channel;
- "input:<device>": a recording device, for a Video capture (camera kind): a capture card such as the Elgato
  HD60 X brings its sound as an input of its own, which paired_input finds by name when the card is added;
- None: no sound of its own (desktop sound still carries it, as before).
"""

from __future__ import annotations

import re
import unicodedata

from sprigcast.core.scene import SceneCollection, SourceConfig

APP = "app"
INPUT = "input:"  # "input:<device name>": a recording device (a capture card's own sound)
CAPTURE_KINDS = ("window_capture", "game_capture", "camera")

# Words that don't tell one device from another, left out when pairing a camera with its sound.
_GENERIC = {"digital", "audio", "interface", "input", "microphone", "mic", "line", "in", "usb", "device", "video",
            "webcam", "camera", "capture", "elgato", "cam", "the", "and"}


def _words(name: str) -> set[str]:
    text = unicodedata.normalize("NFKD", name or "").replace("Ɩ", "l").replace("ɩ", "l").casefold()
    return {w for w in re.findall(r"[a-z0-9]+", text) if w not in _GENERIC}


def paired_input(camera_name: str, inputs: list[str]) -> str | None:
    """The recording device that is this video capture's own sound, by name: a capture card such as
    "Game Capture HD60 X" lists its sound as "Game Capture HD60 X" or "Digital Audio Interface (Game Capture HD60 X)".
    Every telling word of the camera's name must be in the input's; None when nothing fits that well.
    A sound mixer app's devices (Wave Link) are never picked."""
    from sprigcast.sources.audio import is_mixer_app_device

    wanted = _words(camera_name)
    if not wanted:
        return None
    fits = [name for name in inputs if not is_mixer_app_device(name) and wanted <= _words(name)
            and not _another_mic(camera_name, name)]
    return min(fits, key=lambda name: len(_words(name) - wanted)) if fits else None


def _another_mic(camera_name: str, input_name: str) -> bool:
    """A microphone that only shares the card's brand: "fifine Video Capture" beside "Microphone (fifine
    Microphone)", a USB mic of the same make (a tester's desk, 2026-11-27). Pairing them would play the streamer's
    own voice back as the card's sound. An input that holds the card's whole name is the card's, whatever it is
    called."""
    plain = input_name.casefold()
    if camera_name.casefold().strip() in plain:
        return False
    return bool(re.search(r"\b(microphone|mic|headset)\b", plain))


def capture_sound(collection: SceneCollection, capture_id: str) -> SourceConfig | None:
    return next((s for s in collection.sources if s.settings.get("capture_of") == capture_id), None)


def sound_choice(collection: SceneCollection, capture_id: str) -> str | None:
    """APP, a device name, or None."""
    sound = capture_sound(collection, capture_id)
    if sound is None:
        return None
    if sound.kind == "audio_input":
        return INPUT + str(sound.settings.get("device", "default"))
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
    elif choice.startswith(INPUT):
        sound = SourceConfig(id=current.id if current else collection.new_id("sound"), kind="audio_input",
                             name=f"{capture.name} sound",
                             settings={"monitor": True,  # a card's sound: you hear the game too, like the console
                                       **base, "device": choice[len(INPUT):], "duck_voice": False})
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
