"""What SprigCast needs to know about itself on Linux and macOS.

On both it runs on the Python engine. Parts that talk to Windows (sound devices, screen capture, graphics card
encoders, the virtual camera) get their own versions phase by phase; until then each says so in the same words,
from here. Nothing in this file changes what SprigCast does on Windows.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Not built yet off Windows, in the words a source's status and the start-up line use. Taken out as each lands.
NOT_YET = ("Microphone and desktop sound", "Screen, window and game capture", "Sound from one app",
           "Graphics card encoders other than NVIDIA's", "Virtual camera", "Global hotkeys", "In-app updates")

NO_KEY_STORE = ("No password store was found on this PC. Install GNOME Keyring or KWallet, sign in to your "
                "desktop again, and SprigCast can save stream keys safely.")


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_mac() -> bool:
    return sys.platform == "darwin"


def name() -> str:
    """This system as people call it: "Linux", "macOS" or "Windows"."""
    return "Linux" if is_linux() else "macOS" if is_mac() else "Windows"


def not_yet(what: str) -> str:
    """A source's status when its Linux or macOS version isn't built yet."""
    return f"unavailable: {what} isn't on {name()} yet"


def state_dir() -> Path:
    """Linux: where logs and other files SprigCast writes for itself go: $XDG_STATE_HOME/SprigCast, which is
    ~/.local/state/SprigCast unless the desktop says otherwise. (Documents isn't a given on Linux.)"""
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".local" / "state"
    return root / "SprigCast"


def log_dir() -> Path | None:
    """Where logs go on Linux and macOS, or None on Windows (Documents\\SprigCast\\Logs, as ever).
    macOS: ~/Library/Logs/SprigCast, where Console looks and no permission question is asked (Documents asks)."""
    if is_linux():
        return state_dir() / "Logs"
    if is_mac():
        return Path.home() / "Library" / "Logs" / "SprigCast"
    return None


def start_line() -> str:
    """One line for the log at start, so a report from a Linux PC or a Mac says what to expect."""
    return f"{name()}: not built yet here: " + "; ".join(sorted(NOT_YET))


def mend_general_settings(settings=None) -> int:
    """Linux: give the "general" settings back their name, once at start. Returns how many were mended.

    Qt keeps settings in an .ini file there, where a group called "general" is written as [%General] and read
    back as "General": every general/ setting (theme, tray, "the optimizer was offered") was saved and then
    never found again, so the first-start wizard opened at every start (sim PC, 2026-11-28). Renamed here in
    memory, they read as general/ for this run and are written the same way again when SprigCast saves.
    Windows (the registry) and macOS (a property list) keep the name as it is."""
    if not is_linux():
        return 0
    from PySide6.QtCore import QSettings

    q = settings or QSettings()
    found = [key for key in q.allKeys() if key.startswith("General/")]
    for key in found:
        value = q.value(key)
        q.remove(key)
        q.setValue("general/" + key[len("General/"):], value)
    return len(found)


def private_settings(folder: Path | None = None) -> bool:
    """Linux and macOS: only this user may read SprigCast's settings folder, which holds the Control API password
    and paired apps' keys (control/secret_store.py). True when it was (or already is) so."""
    if not (is_linux() or is_mac()):
        return False
    import os

    try:
        if folder is None:
            from PySide6.QtCore import QSettings

            folder = Path(QSettings().fileName()).parent
        folder.mkdir(parents=True, exist_ok=True)
        if folder.stat().st_mode & 0o077:
            os.chmod(folder, 0o700)
        return True
    except OSError as exc:
        import logging

        logging.getLogger(__name__).warning("settings folder %s couldn't be made private: %s", folder, exc)
        return False
