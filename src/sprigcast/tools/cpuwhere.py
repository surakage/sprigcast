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
        if sys.platform == "win32":
            try:
                return _windows_thread_times()
            except Exception:  # noqa: BLE001 (psutil below)
                pass
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
        labels = getattr(self, "_labels", {})
        for tid, total in after.items():  # only threads that ran, each looked up once (340 ms for all, 2026-11-17)
            if tid not in names and total > self._before.get(tid, 0.0):
                if tid not in labels:
                    labels[tid] = native_thread_label(tid)
                if labels[tid]:
                    names[tid] = labels[tid]
        self._labels = {tid: label for tid, label in labels.items() if tid in after}  # thread ids get reused
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


def _windows_thread_times() -> dict[int, float]:
    """{thread id: CPU seconds} for this process's threads. psutil's own walk of every thread on the PC held
    Python's lock 434 ms on the PC (2026-11-17), freezing the window even from another thread; ctypes lets go of
    it at each call."""
    import ctypes
    from ctypes import wintypes

    class THREADENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", ctypes.c_long),
                    ("tpDeltaPri", ctypes.c_long), ("dwFlags", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Thread32First.argtypes = kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetThreadTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD: every thread on the PC
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        raise OSError("no thread snapshot")
    pid, found = os.getpid(), {}
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        ours = []
        while more:
            if entry.th32OwnerProcessID == pid:
                ours.append(entry.th32ThreadID)
            more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    times = [wintypes.FILETIME() for _ in range(4)]
    for tid in ours:
        handle = kernel32.OpenThread(0x0800, False, tid)  # THREAD_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            if kernel32.GetThreadTimes(handle, *(ctypes.byref(t) for t in times)):
                kernel, user = times[2], times[3]
                found[tid] = ((kernel.dwHighDateTime << 32 | kernel.dwLowDateTime)
                              + (user.dwHighDateTime << 32 | user.dwLowDateTime)) / 1e7
        finally:
            kernel32.CloseHandle(handle)
    if not found:
        raise OSError("no thread times")
    return found


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
