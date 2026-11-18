"""Windows: every Python thread carries its name, so a crash dump says which thread it was.

The 2026-11-18 12:21 crash (live, right after a bloom switch) was in a Python thread made 1.6 s before it, deep in
Python's own thread teardown, and the dump could only say "a thread Python started": Python never tells Windows
its threads' names. Here each thread gives Windows its name when it starts, and "<name> (ending)" once its own
work is done and Python is tearing it down, so the next dump names both the thread and the stage. Two calls per
thread, nothing per frame.
"""

from __future__ import annotations

import sys
import threading

ENDING = " (ending)"
_installed = False
_describe = None  # kernel32's SetThreadDescription (Windows 10 1607+), or None


def _set(name: str) -> None:
    describe = _describe
    if describe is None:
        return
    try:
        describe(_current(), name[:200])
    except Exception:  # noqa: BLE001 - a name is only a help for crash reports
        pass


def _current():
    import ctypes

    return ctypes.c_void_p(-2)  # GetCurrentThread()'s pseudo handle: nothing to close


def install(platform: str | None = None, describe=None) -> bool:
    """Name every thread started from now on (and the main thread). True if it's in place. describe: for
    tests, a stand-in for SetThreadDescription(handle, name)."""
    global _installed, _describe
    if _installed:
        return True
    if describe is None:
        if (platform or sys.platform) != "win32":
            return False
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32")
            describe = kernel32.SetThreadDescription
            describe.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            describe.restype = ctypes.c_long
        except (OSError, AttributeError):
            return False  # older Windows: no thread names
    _describe = describe
    inner = threading.Thread._bootstrap_inner  # type: ignore[attr-defined]

    def named(self):
        _set(self.name)
        try:
            inner(self)
        finally:
            _set(self.name + ENDING)

    threading.Thread._bootstrap_inner = named  # type: ignore[attr-defined]
    _installed = True
    _set(threading.current_thread().name)
    return True
