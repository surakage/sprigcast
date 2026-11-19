"""A copy of SprigCast's settings next to its gardens, so they survive settings going missing from the registry.

Qt keeps settings in the registry (HKCU\\Software\\Slothbloom\\SprigCast). On 2026-11-17, after an update, the
control/ and window/ groups and general/optimized were gone while the rest and the gardens were fine: the Control
API port (back to OBS's 4455), StreamBridge's pairing and the window layout were lost, and the first-run wizard
opened. Now SprigCast copies its settings to settings-kept.ini in its config folder 30 s after it starts, on
every Save in Settings and when it closes. At start, any setting that's missing but
is in the copy comes back (and the log says which), apart from ones SprigCast removes on purpose.

While SprigCast runs, the copy is made by a short program of its own (keep_aside): Qt writes the file with
Python's lock held, so every Python thread waits for it. On the PC (2026-11-19, 30 s after start) that write took
1.7 s: the window froze and the old engine missed 99 frames. Only the copy at closing is made in SprigCast itself.

The copy goes wherever the settings would: Clear all my data deletes the config folder (and stops copying), and
uninstall's forget_keys takes the keys and pairings out of the copy as it does from the registry.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

FILE = "settings-kept.ini"
MARKER = "kept/at"
PASSWORD, LOCAL_KEY = "control/password", "control/local_key"  # as app/control_server.py  # when the copy was last made; in both the settings and the copy
stopped = False  # Clear all my data: nothing more is copied
ENV_HERE = "SPRIGCAST_SETTINGS_COPY_HERE"  # =1: every copy is made in SprigCast itself, as before (the tests)
ASIDE_S = 30.0  # a copying program still running after this long is ended
_aside: list[subprocess.Popen] = []  # copying programs running now
_aside_lock = threading.Lock()


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


def aside_command(config: Path, settings=None) -> list[str] | None:
    """The program that makes the copy: this Python running keep_here() on the same settings. None when there's
    no saying where the settings are."""
    try:
        from PySide6.QtCore import QSettings

        live = settings if settings is not None else QSettings()
        ini = live.format() == QSettings.Format.IniFormat
        names = (live.organizationName(), live.applicationName())
        if all(names):  # kept in files (the tests): the folder they are under, <folder>/<organization>/<name>.ini
            place = str(Path(live.fileName()).parent.parent) if ini else ""
        elif ini and live.fileName():
            names, place = ("", ""), live.fileName()  # one settings file, by its path
        else:
            return None
    except Exception:  # noqa: BLE001
        return None
    code = ("import sys; sys.path[:0] = [p for p in sys.argv[5:] if p]; "
            "from sprigcast.app.settings_keeper import keep_here; sys.exit(keep_here(*sys.argv[1:5]))")
    return [sys.executable, "-I", "-c", code, str(config), names[0], names[1], place,
            *[p for p in sys.path if p and Path(p).is_dir()]]


def keep_here(config: str, organization: str, application: str, place: str = "") -> int:
    """In the copying program: make the copy. Returns its exit code (0: made)."""
    from PySide6.QtCore import QSettings

    if not organization:
        live = QSettings(place, QSettings.Format.IniFormat)
    else:
        if place:
            QSettings.setDefaultFormat(QSettings.Format.IniFormat)
            QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, place)
        live = QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, organization, application)
    return 0 if keep(Path(config), live) else 1


def keep_aside(config: Path, settings=None, command: list[str] | None = None) -> bool:
    """keep() while SprigCast runs: in a short program of its own, so nothing here waits for the file to be
    written. True when that program was started; if it can't be, the copy is made here as before."""
    if stopped:
        return False
    command = command or (None if os.environ.get(ENV_HERE) else aside_command(config, settings))
    proc = None
    if command is not None:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        except OSError as exc:
            log.warning("settings: the copying program didn't start (%s); copying here", exc)
    if proc is None:
        keep(config, settings)
        return False
    with _aside_lock:
        _aside.append(proc)
    threading.Thread(target=_await_aside, args=(proc,), name="sprigcast-settings-copy", daemon=True).start()
    return True


def _await_aside(proc: subprocess.Popen) -> None:
    try:
        code = proc.wait(ASIDE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
    with _aside_lock:
        if proc in _aside:
            _aside.remove(proc)
    if code != 0 and not stopped:
        log.warning("settings: the copying program ended with code %s; the copy is made again at the next Save "
                    "or when SprigCast closes", code)


def _end_aside() -> None:
    """Clear all my data: no copying program writes a copy after this."""
    with _aside_lock:
        running = list(_aside)
    for proc in running:
        try:
            proc.kill()
            proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass


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
    _end_aside()
    try:
        path(config).unlink()
    except OSError:
        pass
