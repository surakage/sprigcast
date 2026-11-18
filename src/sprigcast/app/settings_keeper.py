"""A copy of SprigCast's settings next to its gardens, so they survive settings going missing from the registry.

Qt keeps settings in the registry (HKCU\\Software\\Slothbloom\\SprigCast). On 2026-11-17, after an update, the
control/ and window/ groups and general/optimized were gone while the rest and the gardens were fine: the Control
API port (back to OBS's 4455), StreamBridge's pairing and the window layout were lost, and the first-run wizard
opened. Now SprigCast copies its settings to settings-kept.ini in its config folder 30 s after it starts, on
every Save in Settings and when it closes. At start, any setting that's missing but
is in the copy comes back (and the log says which), apart from ones SprigCast removes on purpose.

The copy goes wherever the settings would: Clear all my data deletes the config folder (and stops copying), and
uninstall's forget_keys takes the keys and pairings out of the copy as it does from the registry.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

FILE = "settings-kept.ini"
MARKER = "kept/at"
PASSWORD, LOCAL_KEY = "control/password", "control/local_key"  # as app/control_server.py  # when the copy was last made; in both the settings and the copy
stopped = False  # Clear all my data: nothing more is copied


def path(config: Path) -> Path:
    return Path(config) / FILE


def _copy_of(config: Path):
    from PySide6.QtCore import QSettings

    return QSettings(str(path(config)), QSettings.Format.IniFormat)


def keep(config: Path, settings=None) -> int:
    """Copy every setting into the kept copy. Returns how many. Never raises (a copy is a bonus)."""
    if stopped:
        return 0
    try:
        from PySide6.QtCore import QSettings

        live = settings if settings is not None else QSettings()
        live.setFallbacksEnabled(False)  # only SprigCast's own settings, never Qt's fallback places (HKLM, ...)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        live.setValue(MARKER, stamp)
        live.sync()
        Path(config).mkdir(parents=True, exist_ok=True)
        copy = _copy_of(config)
        copy.clear()
        keys = live.allKeys()
        keys = [key for key in keys if not _never_kept(key)]
        for key in keys:
            copy.setValue(key, live.value(key))
        copy.sync()
        return len(keys)
    except Exception as exc:  # noqa: BLE001
        log.warning("settings: couldn't keep a copy: %s", exc)
        return 0


# Removed on purpose by SprigCast itself, so never put back: canvas changes waiting for a restart (applied
# once), the upload a clean stream proved (forgotten after trouble) and settings of features taken out.
NEVER_BACK = ("video/pending", "upload/proven_kbps")
# Paired devices are never copied or put back: a list from an older copy brings back devices you removed and
# drops ones paired since (PC test 2026-11-18: the pre-wipe list replaced the current harness pairing).
NEVER_KEPT = ("control/devices",)


def _never_kept(key: str) -> bool:
    return any(key == name or key.startswith(name + "_") for name in NEVER_KEPT)


def _never_back(key: str) -> bool:
    from sprigcast.app import leftovers

    return (key in NEVER_BACK or _never_kept(key) or key == MARKER or key.endswith("_unreadable")
            or key in leftovers.KEYS or key.startswith(tuple(f"{group}/" for group in leftovers.GROUPS)))


def restore(config: Path, settings=None) -> list[str]:
    """At start: put back any setting that's missing but was there at the last copy (any already there stay).
    The 2026-11-17 update lost the control/ and window/ groups and general/optimized while the rest stayed, so
    each setting is checked, not only an emptied whole. Returns the keys put back (each one is logged)."""
    try:
        from PySide6.QtCore import QSettings

        if stopped or not path(config).is_file():
            return []
        live = settings if settings is not None else QSettings()
        live.setFallbacksEnabled(False)  # missing means missing here, whatever a fallback place holds
        copy = _copy_of(config)
        back = [key for key in copy.allKeys() if not live.contains(key) and not _never_back(key)]
        if LOCAL_KEY in back and live.contains(PASSWORD):
            back.remove(LOCAL_KEY)  # a new password made it go on purpose (apps get a new one)
        for key in back:
            live.setValue(key, copy.value(key))
        live.sync()
        if back:
            log.warning("settings: %d were missing and came back from the copy kept at %s: %s",
                        len(back), copy.value(MARKER), ", ".join(sorted(back)))
        return back
    except Exception as exc:  # noqa: BLE001
        log.warning("settings: couldn't put back the kept copy: %s", exc)
        return []


def forget(config: Path) -> None:
    """Clear all my data: delete the copy now and make no more."""
    global stopped
    stopped = True
    try:
        path(config).unlink()
    except OSError:
        pass
