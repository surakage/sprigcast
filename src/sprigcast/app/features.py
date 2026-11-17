"""Settings → General → Features: switch off what you never use (Clips, Virtual camera).

A feature that's off leaves Live's bar and the command list, and loads nothing: clips keep no seconds in
memory and the virtual camera isn't added. Its own settings stay, so switching it back on is as it was.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

OFF_KEY = "features/off"
FEATURES = {"camera": "Virtual camera", "clips": "Clips"}
OFF_NOTE = {"camera": "The virtual camera is off (Settings → General → Features).",
            "clips": "Clips are off (Settings → General → Features)."}


def off(settings: QSettings | None = None) -> set[str]:
    value = (settings or QSettings()).value(OFF_KEY, "", type=str) or ""
    return {name for name in value.split(",") if name in FEATURES}


def on(name: str, settings: QSettings | None = None) -> bool:
    return name not in off(settings)


def set_off(names: set[str], settings: QSettings | None = None) -> None:
    (settings or QSettings()).setValue(OFF_KEY, ",".join(sorted(n for n in names if n in FEATURES)))
