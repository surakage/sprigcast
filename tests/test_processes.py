"""Everything running from one call to Windows (core/processes.py)."""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import time

import pytest

from sprigcast.core import processes


def _entry(step: int, pid: int, parent: int, name_at: int, name: str, user_s: float, kernel_s: float) -> bytes:
    raw = bytearray(processes.ENTRY + 32)
    struct.pack_into("<I", raw, processes.NEXT, step)
    struct.pack_into("<qq", raw, processes.USER_TIME, int(user_s * 1e7), int(kernel_s * 1e7))
    struct.pack_into("<HH", raw, processes.NAME_LENGTH, len(name) * 2, len(name) * 2 + 2)
    struct.pack_into("<Q", raw, processes.NAME_BUFFER, name_at)
    struct.pack_into("<QQ", raw, processes.PID, pid, parent)
    return bytes(raw)


def test_the_answer_is_read_entry_by_entry():
    names = {1000: "Game.exe", 2000: "MsMpEng.exe"}
    size = processes.ENTRY + 32
    raw = (_entry(size, 0, 0, 0, "", 50.0, 0.0) + _entry(size, 4321, 4, 1000, names[1000], 1.5, 0.25)
           + _entry(0, 77, 4321, 2000, names[2000], 0.0, 3.0))
    got = processes.parse(raw, lambda at, count: names[at][:count])
    assert got == [processes.Running(0, 0, "", 50.0), processes.Running(4321, 4, "Game.exe", 1.75),
                   processes.Running(77, 4321, "MsMpEng.exe", 3.0)]
    assert processes.parse(b"", ctypes.wstring_at) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own list")
def test_windows_gives_this_process_and_its_parent_in_one_call():
    from sprigcast.sources import app_audio

    started = time.perf_counter()
    found = processes.snapshot()
    assert time.perf_counter() - started < 1.0
    me = next(p for p in found if p.pid == os.getpid())
    assert me.parent == os.getppid() and me.name.lower().startswith("python") and me.cpu_s > 0
    walked = {pid: (parent, name) for pid, parent, name in app_audio._toolhelp_processes()}
    same = [p for p in found if p.pid in walked and p.name]
    assert len(same) > 10 and sum(walked[p.pid] == (p.parent, p.name) for p in same) >= len(same) - 3
    assert app_audio._list_processes()  # the App sound scan takes it from there


@pytest.mark.skipif(sys.platform == "win32", reason="elsewhere psutil is asked")
def test_elsewhere_it_says_so():
    with pytest.raises(OSError):
        processes.snapshot()
