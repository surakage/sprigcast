"""Start SprigCast when Windows starts, and start it minimised.

"Start with Windows" is a value under HKEY_CURRENT_USER\\...\\Run, the same place Task Manager's
Startup apps tab lists and can switch off, so the registry is the only record of it. An installed copy
runs SprigCast.exe (the launcher next to the bundled Python, no console window) with ``--minimized``;
a copy run from source uses pythonw.exe with ``-m sprigcast.app.main --minimized``. Either way a Windows
start never puts a window in front of you.

"Start minimised" is a setting for every start, picked in Settings → General.
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "SprigCast"
MINIMIZED_FLAG = "--minimized"
START_MINIMIZED = "general/start_minimized"


def supported() -> bool:
    return sys.platform == "win32"


def command() -> str:
    """The command line Windows runs at sign-in."""
    exe = Path(sys.executable)
    launcher = exe.with_name("SprigCast.exe")
    # In a development checkout's virtual environment "SprigCast.exe" is pip's console script for `sprigcast`
    # (Windows names aren't case-sensitive): it would start with a console window. Only the installed one counts.
    in_venv = (exe.parent.parent / "pyvenv.cfg").exists()
    if launcher.exists() and not in_venv:
        return f'"{launcher}" {MINIMIZED_FLAG}'
    windowless = exe.with_name("pythonw.exe")
    if windowless.exists():
        exe = windowless
    return f'"{exe}" -m sprigcast.app.main {MINIMIZED_FLAG}'


_known: bool | None = None  # what the registry said last (read at start-up, kept up to date by set_enabled)


def enabled(fresh: bool = False) -> bool:
    """Whether Windows starts SprigCast. Read once (at start-up, off the window's thread: see refresh) and then
    remembered: read again when Settings → General was made, it was 141 ms of the warm-up while live on the PC
    (2026-11-18). fresh: read the registry again."""
    global _known
    if not supported():
        return False
    if _known is not None and not fresh:
        return _known
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE)
        _known = True
    except OSError:
        _known = False
    return _known


def set_enabled(on: bool) -> None:
    """Add or remove the Run entry. Raises OSError if Windows refuses."""
    if not supported():
        return
    import winreg

    global _known
    _known = None  # read again next time, should this fail part way
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, VALUE)
            except FileNotFoundError:
                pass
    _known = bool(on)


def refresh() -> None:
    """Keep the Run entry pointing at this copy of SprigCast (the folder or Python may have moved)."""
    try:
        if enabled():
            set_enabled(True)
    except OSError:
        pass


def start_minimized(argv: list[str]) -> bool:
    from PySide6.QtCore import QSettings

    return MINIMIZED_FLAG in argv or QSettings().value(START_MINIMIZED, False, type=bool)
