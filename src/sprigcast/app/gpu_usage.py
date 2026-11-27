"""How busy the graphics card is, from Windows' own "GPU Engine" counters.

These are the numbers behind Task Manager's GPU column, so they work for any card (NVIDIA, AMD,
Intel) and cost next to nothing. NVIDIA's NVML library gave the same reading but kept 20 MB of
its own in SprigCast's memory just for one percentage in the status bar.
"""

from __future__ import annotations

import functools
import logging
import sys
import threading
import time
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


def busiest_processes(samples: list[tuple[str, float]], top: int = 4) -> list[tuple[int, str, float]]:
    """The processes using the card most: [(pid, engine type, %)], each process at its busiest engine."""
    best: dict[int, tuple[str, float]] = {}
    for name, value in samples:
        if not name.startswith("pid_"):
            continue
        try:
            pid = int(name.split("_", 2)[1])
        except ValueError:
            continue
        kind = name.rsplit("engtype_", 1)[-1] if "engtype_" in name else "?"
        if value > best.get(pid, ("", -1.0))[1]:
            best[pid] = (kind, value)
    ranked = sorted(((pid, kind, value) for pid, (kind, value) in best.items() if value >= 1.0), key=lambda r: -r[2])
    return ranked[:top]


def card_key(luid: tuple[int, int]) -> str:
    """A (low, high) LUID as the counters spell it: "0x00000000_0x0000d1a2" (high part first), lower case."""
    low, high = luid
    return f"0x{high & 0xFFFFFFFF:08x}_0x{low & 0xFFFFFFFF:08x}"


ENGINE_CARD: str | None = None  # card_key of the card the engine draws on (native_bridge tells it)


def by_card(samples: list[tuple[str, float]]) -> dict[str, tuple[float, str, list[tuple[int, float]]]]:
    """Each card's load: {card_key: (its busiest engine's %, that engine's type, [(pid, %) on that engine])}.
    A hybrid laptop has two: the one SprigCast draws on, and the one the screen is wired to (CRGPlays,
    2026-11-27: "graphics card 100%" with SprigCast's own frame work at 2 ms, and nothing said which chip)."""
    engines: dict[str, float] = defaultdict(float)
    users: dict[str, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for name, value in samples:
        rest = name.split("_luid_", 1)
        if len(rest) != 2:
            continue
        engines[rest[1]] += value
        try:
            users[rest[1]][int(rest[0].split("_", 1)[1])] += value
        except (IndexError, ValueError):
            pass
    cards: dict[str, tuple[float, str, list[tuple[int, float]]]] = {}
    for engine, load in engines.items():
        parts = engine.lower().split("_")
        if len(parts) < 2:
            continue
        key = f"{parts[0]}_{parts[1]}"
        if key in cards and cards[key][0] >= load:
            continue
        kind = engine.rsplit("engtype_", 1)[-1] if "engtype_" in engine else "?"
        top = sorted(((pid, v) for pid, v in users[engine].items() if v >= 1.0), key=lambda r: -r[1])[:3]
        cards[key] = (min(100.0, load), kind, top)
    return cards


def describe_cards(cards: dict[str, tuple[float, str, list[tuple[int, float]]]], own: str | None,
                   name_of=None) -> str:
    """"SprigCast's card 3D 41% (SprigCast.exe 30%); the other chip 3D 100% (dwm.exe 64%, SprigCast.exe 22%)"."""
    if name_of is None:
        name_of = _process_name
    parts = []
    for key, (load, kind, top) in sorted(cards.items(), key=lambda kv: (kv[0] != own, -kv[1][0])):
        if load < 1.0 and key != own:
            continue
        who = ", ".join(f"{name_of(pid)} {v:.0f}%" for pid, v in top) or "nobody"
        label = "SprigCast's card" if key == own else ("another card" if own else f"card {key}")
        parts.append(f"{label} {kind} {load:.0f}% ({who})")
    return "; ".join(parts) or "no card busy"


def _process_name(pid: int) -> str:
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001 (gone already, or not allowed to look)
        return "?"


def describe_processes(ranked: list[tuple[int, str, float]]) -> str:
    """"msedge.exe (pid 4242) 3D 85%, SprigCast.exe (pid 10) 3D 20%" or "nothing busy"."""
    import os

    parts = []
    for pid, kind, value in ranked:
        try:
            import psutil

            name = psutil.Process(pid).name()
        except Exception:  # noqa: BLE001 (gone already, or not allowed to look)
            name = "?"
        parts.append(f"{name} (pid {pid}{', this app' if pid == os.getpid() else ''}) {kind} {value:.0f}%")
    return ", ".join(parts) or "nothing busy"


@functools.cache
def _item_type():
    """PDH_FMT_COUNTERVALUE_ITEM_W with the double member of the union. Made once: a ctypes Structure class made
    in __init__ (a fresh query every RENEW_S) and pointed at with ctypes.POINTER is kept by ctypes forever."""
    import ctypes
    from ctypes import wintypes

    class Item(ctypes.Structure):
        _fields_ = [("name", ctypes.c_wchar_p), ("status", wintypes.DWORD), ("value", ctypes.c_double)]

    return Item


class GpuCounter:
    """Open once, then read() whenever. Raises OSError if the counters aren't available."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("needs Windows")
        import ctypes
        from ctypes import wintypes

        self._ctypes, self._item = ctypes, _item_type()
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
        self.processes: list[tuple[int, str, float]] = []  # the busiest processes at the last read
        self.cards: dict = {}  # by_card at the last read
        self.read_at = 0.0

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
        self.processes = busiest_processes(samples)
        self.cards = by_card(samples)
        self.read_at = time.monotonic()
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


def cards_line() -> str | None:
    """Each card's load at the GPU meter's last read, said plainly (None when unknown)."""
    if not _counter or not getattr(_counter, "cards", None):
        return None
    return describe_cards(_counter.cards, ENGINE_CARD)


def windows_encoder_load() -> float | None:
    """The video encoder's busy % at the GPU meter's last read, or None when unknown."""
    return getattr(_counter, "encode", None) if _counter else None


log = logging.getLogger(__name__)
STALL_LOOK_S = 3.5  # a little over the GPU meter's interval (app/resources.py GPU_EVERY_S): its next read covers it
_stall_logged = 0.0


def log_gpu_users_later(stalled_ms: float) -> None:
    """The graphics card stopped for this whole app (the render loop and the window waited ~0.75 s at once, a few
    times a day on the PC since 2026-11-17, idle or live, either engine): log which programs were using the card
    then, from Windows' own per-process counters. The meter's next read covers the stall; without the meter, a
    query of its own over the next second. At most once a minute, from a thread of its own."""
    global _stall_logged
    now = time.monotonic()
    if sys.platform != "win32" or now - _stall_logged < 60.0:
        return
    _stall_logged = now

    def look() -> None:
        try:
            time.sleep(STALL_LOOK_S)
            counter = _counter if _counter and getattr(_counter, "read_at", 0.0) > now else None
            if counter is None:
                counter = GpuCounter()
                time.sleep(1.0)
                counter.read()
                counter.close()
            from sprigcast.core.pacing import memory_pressure

            log.info("graphics card stalled %.0f ms; programs using it then: %s%s", stalled_ms,
                     describe_processes(counter.processes), memory_pressure())
        except Exception as exc:  # noqa: BLE001 (a diagnostic: never in the way)
            log.info("graphics card stalled %.0f ms; couldn't see who used it: %s", stalled_ms, exc)

    threading.Thread(target=look, name="sprigcast-gpu-stall", daemon=True).start()
