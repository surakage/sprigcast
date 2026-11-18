"""A small crash dump in the Logs folder when native code crashes SprigCast (Windows).

crash.log (faulthandler) names the Python lines each thread was on, but a crash in native code (Qt, a driver,
an asyncio completion port) shows as "<no Python frame>": the 2026-11-16 idle crash said only "access
violation". A minidump holds every thread's native call stack and the loaded DLLs, which says which library it
was. It's written by Windows' own dbghelp from an unhandled-exception filter, after faulthandler has written
its part; Windows Error Reporting still runs after it.

Only stacks and module lists go in (MiniDumpNormal plus thread and unloaded-module details, a few MB), not
the whole memory, so stream keys and chat aren't copied out wholesale. The newest KEEP stay; nothing sends
them anywhere.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

KEEP = 3
_installed: dict = {}  # holds the callback and buffers for the life of the process

MiniDumpNormal = 0x0
MiniDumpWithThreadInfo = 0x1000
MiniDumpWithUnloadedModules = 0x20
MiniDumpWithIndirectlyReferencedMemory = 0x40
DUMP_TYPE = MiniDumpNormal | MiniDumpWithThreadInfo | MiniDumpWithUnloadedModules
EXCEPTION_CONTINUE_SEARCH = 0
GENERIC_WRITE = 0x40000000
CREATE_ALWAYS = 2
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE = ctypes.c_void_p(-1).value


class _MINIDUMP_EXCEPTION_INFORMATION(ctypes.Structure):
    _pack_ = 4
    _fields_ = [("ThreadId", ctypes.c_uint32), ("ExceptionPointers", ctypes.c_void_p), ("ClientPointers", ctypes.c_int)]


def dump_name(now: float | None = None) -> str:
    stamp = time.strftime("%Y-%m-%d %H-%M-%S", time.localtime(time.time() if now is None else now))
    return f"crash {stamp} pid{os.getpid()}.dmp"


def prune(folder: Path, keep: int = KEEP) -> int:
    """Keep the newest `keep` dumps; returns how many were removed."""
    dumps = sorted(folder.glob("crash *.dmp"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for old in dumps[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _launcher_guard(path: str) -> bool:
    """SprigCast.exe's own crash guard (native/launcher/launcher.c), set up to write to path. False when this isn't
    SprigCast.exe (python.exe while developing), or it's an older one without it."""
    if Path(sys.executable).name.lower() != "sprigcast.exe":
        return False
    try:
        exe = ctypes.WinDLL(sys.executable)  # the running exe itself: its exported functions
        guard = exe.sprigcast_crash_guard
    except (OSError, AttributeError):
        return False
    guard.argtypes = [ctypes.c_wchar_p]
    guard.restype = ctypes.c_int
    return bool(guard(path))


def install(folder: Path) -> bool:
    """Write a minidump into folder if SprigCast crashes in native code. True when set up (Windows only).

    In SprigCast.exe the launcher's guard does it, in plain C, and always ends the process (a crash on a worker
    thread hung the window for 25 minutes with the Python callback below, PC 2026-11-18). The Python callback
    is only for python.exe, while developing."""
    if sys.platform != "win32" or _installed:
        return bool(_installed)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        prune(folder)
        target = str(folder / dump_name())
        if _launcher_guard(target):
            _installed.update(path=target, by="launcher")
            log.info("crash dumps: SprigCast.exe's guard")
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        dbghelp = ctypes.WinDLL("dbghelp")  # loaded now: nothing to load while crashing
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetCurrentThreadId.restype = ctypes.c_uint32
        dbghelp.MiniDumpWriteDump.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        dbghelp.MiniDumpWriteDump.restype = ctypes.c_int
        path = ctypes.create_unicode_buffer(str(folder / dump_name()))  # made now, not while crashing
        info = _MINIDUMP_EXCEPTION_INFORMATION()
        pid = os.getpid()

        @ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
        def on_crash(pointers):
            try:
                handle = kernel32.CreateFileW(path, GENERIC_WRITE, 0, None, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
                if handle and handle != INVALID_HANDLE:
                    info.ThreadId = kernel32.GetCurrentThreadId()
                    info.ExceptionPointers = pointers
                    info.ClientPointers = 0
                    dbghelp.MiniDumpWriteDump(kernel32.GetCurrentProcess(), pid, handle, DUMP_TYPE,
                                              ctypes.byref(info), None, None)
                    kernel32.CloseHandle(handle)
            except BaseException:  # noqa: BLE001 (crashing already: never make it worse)
                pass
            return EXCEPTION_CONTINUE_SEARCH  # Windows Error Reporting goes on as before

        kernel32.SetUnhandledExceptionFilter.restype = ctypes.c_void_p
        kernel32.SetUnhandledExceptionFilter.argtypes = [ctypes.c_void_p]
        kernel32.SetUnhandledExceptionFilter(ctypes.cast(on_crash, ctypes.c_void_p))
        _installed.update(callback=on_crash, path=path, info=info, kernel32=kernel32, dbghelp=dbghelp)
        return True
    except Exception as exc:  # noqa: BLE001 (no dumps is fine: crash.log still says what it can)
        log.info("crash dumps off: %s", exc)
        return False
