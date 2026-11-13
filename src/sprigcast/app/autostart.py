"""Start SprigCast when Windows starts, and start it minimised.

"Start with Windows" is a value under HKEY_CURRENT_USER\\...\\Run, the same place Task Manager's
Startup apps tab lists and can switch off, so the registry is the only record of it. It runs
pythonw.exe from SprigCast's own environment (no console window) with ``-m sprigcast.app.main
--minimized``, so a Windows start never puts a window in front of you.

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
    windowless = exe.with_name("pythonw.exe")
    if windowless.exists():
        exe = windowless
    return f'"{exe}" -m sprigcast.app.main {MINIMIZED_FLAG}'


def enabled() -> bool:
    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE)
        return True
    except OSError:
        return False


def set_enabled(on: bool) -> None:
    """Add or remove the Run entry. Raises OSError if Windows refuses."""
    if not supported():
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, VALUE)
            except FileNotFoundError:
                pass


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
