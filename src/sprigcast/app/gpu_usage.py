"""How busy the graphics card is, from Windows' own "GPU Engine" counters.

These are the numbers behind Task Manager's GPU column, so they work for any card (NVIDIA, AMD,
Intel) and cost next to nothing. NVIDIA's NVML library gave the same reading but kept 20 MB of
its own in SprigCast's memory just for one percentage in the status bar.
"""

from __future__ import annotations

import sys
from collections import defaultdict

COUNTER = r"\GPU Engine(*)\Utilization Percentage"
PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2


def busiest_engine(samples: list[tuple[str, float]], kind: str = "") -> float:
    """Task Manager's figure: add each engine's load over all processes, then take the busiest engine.

    A sample's name looks like "pid_4242_luid_0x0_0x1234_phys_0_eng_0_engtype_3D". With `kind`
    ("VideoEncode") only engines of that type count: the video encoder's own load.
    """
    engines: dict[str, float] = defaultdict(float)
    for name, value in samples:
        engine = name.split("_luid_", 1)[-1]  # without the process id
        if kind and not engine.endswith("engtype_" + kind):
            continue
        engines[engine] += value
    return min(100.0, max(engines.values(), default=0.0))


class GpuCounter:
    """Open once, then read() whenever. Raises OSError if the counters aren't available."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("needs Windows")
        import ctypes
        from ctypes import wintypes

        class Item(ctypes.Structure):  # PDH_FMT_COUNTERVALUE_ITEM_W with the double member of the union
            _fields_ = [("name", ctypes.c_wchar_p), ("status", wintypes.DWORD), ("value", ctypes.c_double)]

        self._ctypes, self._item = ctypes, Item
        pdh = self._pdh = ctypes.WinDLL("pdh")
        handle = wintypes.HANDLE
        pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(handle)]
        pdh.PdhAddEnglishCounterW.argtypes = [handle, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(handle)]
        pdh.PdhCollectQueryData.argtypes = [handle]
        pdh.PdhGetFormattedCounterArrayW.argtypes = [handle, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        pdh.PdhCloseQuery.argtypes = [handle]
        for fn in (pdh.PdhOpenQueryW, pdh.PdhAddEnglishCounterW, pdh.PdhCollectQueryData,
                   pdh.PdhGetFormattedCounterArrayW, pdh.PdhCloseQuery):
            fn.restype = ctypes.c_uint32
        self._query, self._counter = handle(), handle()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)):
            raise OSError("couldn't open a performance query")
        if pdh.PdhAddEnglishCounterW(self._query, COUNTER, 0, ctypes.byref(self._counter)):
            pdh.PdhCloseQuery(self._query)
            raise OSError("this Windows has no GPU Engine counters")
        pdh.PdhCollectQueryData(self._query)  # a rate needs two samples: this is the first
        self.encode: float | None = None  # the video encoder's load at the last read

    def read(self) -> float:
        """Percent busy since the last read (0 until there are two samples)."""
        ctypes, pdh = self._ctypes, self._pdh
        from ctypes import wintypes

        if pdh.PdhCollectQueryData(self._query):
            return 0.0
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        status = pdh.PdhGetFormattedCounterArrayW(self._counter, PDH_FMT_DOUBLE, ctypes.byref(size),
                                                  ctypes.byref(count), None)
        if status != PDH_MORE_DATA or not size.value:
            return 0.0
        buffer = ctypes.create_string_buffer(size.value)
        if pdh.PdhGetFormattedCounterArrayW(self._counter, PDH_FMT_DOUBLE, ctypes.byref(size),
                                            ctypes.byref(count), buffer):
            return 0.0
        items = ctypes.cast(buffer, ctypes.POINTER(self._item))
        samples = [(items[i].name or "", items[i].value) for i in range(count.value) if items[i].status in (0, 1)]
        self.encode = busiest_engine(samples, "VideoEncode")
        return busiest_engine(samples)

    def close(self) -> None:
        self._pdh.PdhCloseQuery(self._query)


_counter: GpuCounter | None | bool = None  # False once we know the counters can't be used
_opened_at = 0.0
RENEW_S = 300.0  # a "(*)" query collects handles as processes come and go: a fresh one every 5 minutes


def windows_gpu_load() -> float | None:
    """GPU busy % from Windows' counters, or None when they aren't available (then NVML is tried)."""
    global _counter, _opened_at
    import time

    now = time.monotonic()
    if _counter and now - _opened_at > RENEW_S:
        # Windows' GPU Engine counter has one instance per process per engine. A wildcard query that
        # keeps collecting while apps start and stop holds on to handles (+10 a minute in the hour-long
        # retest, never given back); closing the query frees them.
        try:
            _counter.close()
        except OSError:
            pass
        _counter = None
    if _counter is None:
        _opened_at = now
        try:
            _counter = GpuCounter()
        except (OSError, AttributeError):
            _counter = False
    if not _counter:
        return None
    try:
        return _counter.read()
    except (OSError, ValueError):
        return None


def windows_encoder_load() -> float | None:
    """The video encoder's busy % at the GPU meter's last read, or None when unknown."""
    return getattr(_counter, "encode", None) if _counter else None
