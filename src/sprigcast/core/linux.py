"""What the Linux version needs to know about itself (Phase 0 of the Linux plan).

SprigCast on Linux runs on the Python engine. Parts that talk to Windows (sound devices, screen capture,
graphics card encoders, the virtual camera) get their Linux versions phase by phase; until then each says so in
the same words, from here. Nothing in this file is loaded on Windows.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Not on Linux yet, in the words a source's status and the start-up line use. Taken out as each phase lands.
NOT_YET = ("Microphone and desktop sound", "Screen, window and game capture", "Sound from one app",
           "Graphics card encoders other than NVIDIA's", "Virtual camera", "Global hotkeys", "In-app updates")

NO_KEY_STORE = ("No password store was found on this PC. Install GNOME Keyring or KWallet, sign in to your "
                "desktop again, and SprigCast can save stream keys safely.")


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def not_yet(what: str) -> str:
    """A source's status when its Linux version isn't built yet."""
    return f"unavailable: {what} isn't on Linux yet"


def state_dir() -> Path:
    """Where logs and other files SprigCast writes for itself go: $XDG_STATE_HOME/SprigCast, which is
    ~/.local/state/SprigCast unless the desktop says otherwise. (Documents isn't a given on Linux.)"""
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".local" / "state"
    return root / "SprigCast"


def start_line() -> str:
    """One line for the log at start, so a report from a Linux PC says what to expect."""
    return "Linux: not built yet here: " + "; ".join(sorted(NOT_YET))
