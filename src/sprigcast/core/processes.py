"""Every running process from one call to Windows: id, parent, name and CPU time used.

psutil reads one process at a time, and for a process it may not open (Windows Defender, services) it falls back to
a walk over every process on the PC with Python's lock held, about 17 ms each. Naming the heaviest apps while the
PC's CPU was pinned took hundreds of those walks, and the window waited up to 3.4 s behind them (PC 2026-11-21, a
Defender scan). NtQuerySystemInformation gives all of it in one call that needs no rights and lets Python go on.
"""

from __future__ import annotations

import struct
import sys
from typing import NamedTuple

SYSTEM_PROCESS_INFORMATION = 5
LENGTH_MISMATCH = 0xC0000004
# SYSTEM_PROCESS_INFORMATION on 64-bit Windows (the only kind SprigCast runs on): where each field starts.
NEXT, USER_TIME, KERNEL_TIME, NAME_LENGTH, NAME_BUFFER, PID, PARENT, ENTRY = 0, 40, 48, 56, 64, 80, 88, 96


class Running(NamedTuple):
    pid: int
    parent: int
    name: str
    cpu_s: float  # CPU seconds used since it started, all cores added up


def parse(raw: bytes, text_at) -> list[Running]:
    """The entries of a SystemProcessInformation answer. text_at(address, characters) reads a name."""
    out: list[Running] = []
    at = 0
    while at + ENTRY <= len(raw):
        step = struct.unpack_from("<I", raw, at + NEXT)[0]
        user, kernel = struct.unpack_from("<qq", raw, at + USER_TIME)
        length = struct.unpack_from("<H", raw, at + NAME_LENGTH)[0]
        buffer, = struct.unpack_from("<Q", raw, at + NAME_BUFFER)
        pid, parent = struct.unpack_from("<QQ", raw, at + PID)
        name = text_at(buffer, length // 2) if buffer and length else ""
        out.append(Running(int(pid), int(parent), name, (user + kernel) / 1e7))  # times are in 100 ns
        if not step:
            break
        at += step
    return out


def snapshot() -> list[Running]:
    """Everything running now. Raises OSError where Windows can't be asked this way (other systems: psutil)."""
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise OSError("only asked on 64-bit Windows")
    import ctypes

    ntdll = ctypes.WinDLL("ntdll")
    query = ntdll.NtQuerySystemInformation
    query.restype = ctypes.c_uint32
    query.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
    size = 1 << 20
    for _ in range(6):
        buffer = ctypes.create_string_buffer(size)
        needed = ctypes.c_uint32(0)
        status = query(SYSTEM_PROCESS_INFORMATION, buffer, size, ctypes.byref(needed))
        if status == 0:
            # Names point into the buffer itself: read while it is alive.
            return parse(buffer.raw[:needed.value or size], ctypes.wstring_at)
        if status != LENGTH_MISMATCH:
            raise OSError(f"NtQuerySystemInformation failed (0x{status:08X})")
        size = max(size * 2, needed.value + (1 << 16))
    raise OSError("the list of processes kept growing")
