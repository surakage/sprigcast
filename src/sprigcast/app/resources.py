"""What SprigCast costs the PC right now: its CPU share, its memory, and how busy the GPU is.

Shown in the status bar every two seconds so any change (a new source, Eco settings) shows its
effect straight away. The GPU reading comes from nvidia-smi on a background thread, since it can
take a moment; without an NVIDIA card it is simply left out.
"""

from __future__ import annotations

import os
import sys
import threading
import time

try:
    import psutil
except ImportError:  # optional: the meter shows nothing without it
    psutil = None

GPU_EVERY_S = 3.0


class ResourceMeter:
    def __init__(self, gpu: bool = True):
        self._process = psutil.Process(os.getpid()) if psutil else None
        self._cores = (psutil.cpu_count() or 1) if psutil else 1
        if self._process is not None:
            self._process.cpu_percent()  # the first call only starts the measurement
        self.gpu_pct: float | None = None
        self._stop = threading.Event()
        self._gpu_thread = None
        if gpu:
            self._gpu_thread = threading.Thread(target=self._poll_gpu, name="sprigcast-gpu-meter", daemon=True)
            self._gpu_thread.start()

    def _poll_gpu(self) -> None:
        from sprigcast.app.doctor import gpu_load

        while True:
            self.gpu_pct = gpu_load()
            if self.gpu_pct is None or self._stop.wait(GPU_EVERY_S):
                return  # no NVIDIA tools, or stopping

    def stop(self) -> None:
        self._stop.set()

    def _ram_bytes(self) -> int:
        return _private_working_set(self._process)

    def sample(self) -> dict:
        """{"cpu": % of the whole PC, "ram_mb": resident memory, "gpu": % or None}; {} without psutil."""
        if self._process is None:
            return {}
        try:
            cpu = self._process.cpu_percent() / self._cores
            ram = self._ram_bytes() / 1e6
        except (psutil.Error, OSError):
            return {}
        return {"cpu": cpu, "ram_mb": ram, "gpu": self.gpu_pct, "at": time.monotonic()}


def _private_working_set(process) -> int:
    """What Task Manager's "Memory" column shows on Windows (private working set); RSS elsewhere.

    RSS also counts shared files such as GPU driver DLLs and fonts, which other apps share and Windows
    can drop at any time, so it reads higher than Task Manager.

    Windows keeps this number ready (GetProcessMemoryInfo), so reading it is instant. psutil's USS
    walks every page of the process instead, which held up the render thread long enough to leave
    an empty frame in recordings every time the meter refreshed (every two seconds).
    """
    if sys.platform == "win32":
        fast = windows_private_working_set()
        if fast is not None:
            return fast
        try:
            return process.memory_full_info().uss
        except (psutil.Error, OSError):
            pass
    return process.memory_info().rss


def windows_private_working_set() -> int | None:
    """This process's private working set in bytes from PROCESS_MEMORY_COUNTERS_EX2, or None."""
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX2 (Windows 10 1809 and later)
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                         "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
                         "PeakPagefileUsage", "PrivateUsage", "PrivateWorkingSetSize", "SharedCommitUsage")
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        get_info = kernel32.K32GetProcessMemoryInfo
        get_info.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        counters = Counters(cb=ctypes.sizeof(Counters))
        if not get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None  # older Windows without the EX2 layout
        return int(counters.PrivateWorkingSetSize)
    except (AttributeError, OSError):
        return None


def describe(sample: dict) -> str:
    """"CPU 4% · RAM 312 MB · GPU 18%" for the status bar."""
    if not sample:
        return ""
    text = f"CPU {sample['cpu']:.0f}% · RAM {sample['ram_mb']:.0f} MB"
    if sample.get("gpu") is not None:
        text += f" · GPU {sample['gpu']:.0f}%"
    return text


HIGH_RAM_MB = 2000  # SprigCast normally sits well under this, even recording both canvases with replay on


def ram_warning(sample: dict) -> str:
    """A short warning when SprigCast's memory is unusually high, else ""."""
    ram = sample.get("ram_mb") if sample else None
    if ram is None or ram < HIGH_RAM_MB:
        return ""
    return (f"SprigCast is using {ram / 1000:.1f} GB of memory, more than usual. If it keeps climbing, "
            "please save the log and tell us what was running.")
