"""Keep the render thread on time: Python's garbage collector and Windows' thread scheduling.

A 23-minute stress test (2026-11-15) logged a late frame every ~2 minutes where one step of an
ordinary frame suddenly took 50-135 ms, plus a few where the render thread woke 34-66 ms late with
nothing to do. Both fit Python's full garbage collection: it walks every object the app holds while
holding Python's lock, so whichever thread triggers it stalls and every other thread waits for the lock.

- tame_gc(): after start-up, collect once and freeze what's left (fonts, Qt wrappers, the scene
  collection, imported modules). Frozen objects are skipped by every later collection, so a full one
  only walks what was made since, a small fraction of the heap. Young collections run less often too.
- watch_gc(): log any collection that took 15 ms or more, and remember the latest one so the
  late-frame log line can say when a collection was to blame.
- raise_thread_priority(): the render thread asks Windows to run it ahead of the app's other threads
  when it wakes (it sleeps most of each frame, so this takes almost nothing from anyone).
"""

from __future__ import annotations

import gc
import logging
import sys
import time

log = logging.getLogger(__name__)

SLOW_GC_MS = 15.0
GEN0_THRESHOLD = 7000  # Python's default is 700: young collections 10x less often, each still tiny

_started: float | None = None
last_pause: tuple[float, float, int] = (0.0, 0.0, 0)  # (ended at perf_counter, ms, generation)
_logged_at = 0.0


def _on_gc(phase: str, info: dict) -> None:
    global _started, last_pause, _logged_at
    now = time.perf_counter()
    if phase == "start":
        _started = now
        return
    if _started is None:
        return
    ms = 1000 * (now - _started)
    _started = None
    if ms < SLOW_GC_MS:
        return
    last_pause = (now, ms, info.get("generation", -1))
    if now - _logged_at >= 10.0:
        _logged_at = now
        log.info("python cleanup (generation %d) took %.1f ms, %d objects freed",
                 info.get("generation", -1), ms, info.get("collected", 0))


def watch_gc() -> None:
    if _on_gc not in gc.callbacks:
        gc.callbacks.append(_on_gc)


def pause_since(t: float) -> str:
    """" python cleanup N ms" when a slow collection ended after perf_counter time `t`, else ""."""
    ended, ms, _gen = last_pause
    return f"; python cleanup {ms:.0f} ms" if ended >= t else ""


def tame_gc() -> None:
    """Collect once, freeze everything start-up made, and run young collections less often."""
    began = time.perf_counter()
    gc.collect()
    gc.freeze()
    _gen0, gen1, gen2 = gc.get_threshold()
    gc.set_threshold(GEN0_THRESHOLD, gen1, gen2)
    log.info("python cleanup: froze %d start-up objects in %.0f ms", gc.get_freeze_count(),
             1000 * (time.perf_counter() - began))


def raise_thread_priority() -> bool:
    """Run the calling thread at Windows' "highest" thread priority. True if it took effect."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 2))  # THREAD_PRIORITY_HIGHEST
    except Exception as exc:
        log.info("could not raise the render thread's priority: %s", exc)
        return False


def private_mb() -> float:
    """The app's private memory in MB (Task Manager's "Memory" is close to it), for log lines; 0 if unknown."""
    try:
        import psutil

        info = psutil.Process().memory_info()
        return getattr(info, "private", info.rss) / 1e6  # private: Windows only (cheap, unlike a USS walk)
    except Exception:
        return 0.0
