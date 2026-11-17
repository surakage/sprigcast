"""File → Export settings / Import settings: one small file to move a setup to another PC, or to share it.

It holds your choices, never your secrets: encoders, recording and clips, hotkeys and the app's options.
Stream keys, sign-ins, the Control API password and paired devices stay on this PC. Things that only make
sense on this PC (its tier, the recordings folder, window positions) are left out too.
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings

from sprigcast.core.scene import EncoderSettings, RecordingSettings, SceneCollection

FORMAT = "sprigcast-settings"
VERSION = 1
# QSettings groups and keys that travel. Anything else (sign-ins, window layout, this PC's tier) stays.
GROUPS = ("general/", "editing/", "advanced/", "camera/", "logs/", "settings/", "live/", "features/")
KEYS = ("updates/auto_check", "control/enabled", "control/lan", "control/obs_door", "control/web_door",
        "control/port", "control/use_password")
LEFT_OUT = ("general/pc_tier", "general/optimized", "camera/sound_to")
RECORDING_LEFT_OUT = ("folder",)  # a folder on this PC may not exist on the other one


def _travels(key: str) -> bool:
    return key not in LEFT_OUT and (key in KEYS or key.startswith(GROUPS))


def export_settings(collection: SceneCollection, path: Path, settings: QSettings | None = None) -> None:
    q = settings or QSettings()
    options = {}
    for key in q.allKeys():
        if _travels(key):
            value = q.value(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                options[key] = value
    data = {
        "format": FORMAT,
        "version": VERSION,
        "stream_encoder": collection.stream_encoder.model_dump(),
        "recording": collection.recording.model_dump(exclude=set(RECORDING_LEFT_OUT)),
        "hotkeys": dict(collection.hotkeys),
        "options": options,
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def import_settings(collection: SceneCollection, path: Path, settings: QSettings | None = None) -> list[str]:
    """Apply a settings file. Returns what changed, in words; raises ValueError for a file that isn't one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Can't read {path.name}: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError(f"{path.name} isn't a SprigCast settings file.")
    done = []
    try:
        if isinstance(data.get("stream_encoder"), dict):
            collection.stream_encoder = EncoderSettings(**data["stream_encoder"])
            done.append("Stream encoder")
        if isinstance(data.get("recording"), dict):
            keep = {k: getattr(collection.recording, k) for k in RECORDING_LEFT_OUT}
            collection.recording = RecordingSettings(**{**data["recording"], **keep})
            done.append("Recording and clips")
    except Exception as exc:  # noqa: BLE001 (pydantic: a value this version doesn't take)
        raise ValueError(f"Some settings in {path.name} don't fit this SprigCast: {exc}") from exc
    if isinstance(data.get("hotkeys"), dict):
        collection.hotkeys = {str(k): str(v) for k, v in data["hotkeys"].items()}
        done.append("Hotkeys")
    options = data.get("options")
    if isinstance(options, dict):
        q = settings or QSettings()
        count = 0
        for key, value in options.items():
            if isinstance(key, str) and _travels(key):
                q.setValue(key, value)
                count += 1
        if count:
            done.append("App options")
    return done
