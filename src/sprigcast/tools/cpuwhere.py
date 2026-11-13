"""Which threads of this process use the CPU.

Windows and Linux count CPU time per thread; taking that twice and subtracting says who was busy in
between. Python threads are listed by name (the renderer, each encoder, the audio mixer); threads
started by libraries (the graphics driver, Qt, audio devices) are named, on Windows, by their own
description or the library they started in, and added up per library.
"""

from __future__ import annotations

import os
import sys
import threading
import time


def usable_cpus() -> int:
    """CPU threads this process may run on (fewer than the PC has when memcheck --weak limits it)."""
    try:
        import psutil

        return len(psutil.Process().cpu_affinity()) or 1
    except Exception:
        return os.cpu_count() or 1


class CpuWatch:
    def __init__(self) -> None:
        self._at = 0.0
        self._before: dict[int, float] = {}
        self.restart()

    @staticmethod
    def _times() -> dict[int, float]:
        try:
            import psutil

            return {t.id: t.user_time + t.system_time for t in psutil.Process().threads()}
        except Exception:
            return {}

    def restart(self) -> None:
        self._at, self._before = time.monotonic(), self._times()

    def busiest(self) -> tuple[float, list[tuple[str, float]]]:
        """(share of the whole CPU in %, [(thread name, % of one core)] busiest first) since restart()."""
        now, after = time.monotonic(), self._times()
        seconds = max(1e-6, now - self._at)
        names = {t.native_id: t.name for t in threading.enumerate()}
        for tid in after:
            if tid not in names:
                label = native_thread_label(tid)
                if label:
                    names[tid] = label
        used: dict[str, float] = {}
        for tid, total in after.items():
            spent = total - self._before.get(tid, 0.0)
            if spent > 0:
                name = names.get(tid, "other library threads")
                used[name] = used.get(name, 0.0) + spent
        whole = 100.0 * sum(used.values()) / seconds / usable_cpus()
        return whole, sorted(((name, 100.0 * spent / seconds) for name, spent in used.items()), key=lambda u: -u[1])

    def report(self, when: str, top: int = 8) -> None:
        """Print it. Never raises."""
        try:
            whole, threads = self.busiest()
        except Exception as exc:
            print(f"  (couldn't read CPU use: {exc})", flush=True)
            return
        if not threads:
            return
        print(f"  CPU {when}: {whole:.1f}% of the whole CPU ({usable_cpus()} threads). "
              "By thread, in % of one core:", flush=True)
        for name, share in threads[:top]:
            if share >= 0.05:
                print(f"      {share:5.1f}%  {name}", flush=True)


def native_thread_label(tid: int) -> str | None:
    """Windows: a thread's own description, else the library it was started in ("in nvoglv64.dll")."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenThread.restype = wintypes.HANDLE
    handle = kernel32.OpenThread(0x0040 | 0x0800, False, tid)  # QUERY_INFORMATION | QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        try:
            text = ctypes.c_wchar_p()
            get = kernel32.GetThreadDescription
            get.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
            if get(handle, ctypes.byref(text)) >= 0 and text.value:
                name = text.value
                kernel32.LocalFree(text)
                return name
        except AttributeError:
            pass
        start = ctypes.c_void_p()
        ntdll = ctypes.WinDLL("ntdll")
        if ntdll.NtQueryInformationThread(handle, 9, ctypes.byref(start), ctypes.sizeof(start), None) != 0:
            return None  # 9 = ThreadQuerySetWin32StartAddress
        module = wintypes.HMODULE()
        kernel32.GetModuleHandleExW.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.HMODULE)]
        if not start.value or not kernel32.GetModuleHandleExW(0x4 | 0x2, start.value, ctypes.byref(module)):
            return None  # FROM_ADDRESS | UNCHANGED_REFCOUNT
        path = ctypes.create_unicode_buffer(512)
        kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
        if not kernel32.GetModuleFileNameW(module, path, 512):
            return None
        return "threads started in " + path.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)
