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


# Each tidy walks what was made since the last one. Every 5 minutes that was 15-73 ms on the user's PC
# (2026-11-15), long enough to make a frame late now and then; every minute it is a fifth of that.
TIDY_EVERY_MS = 60_000


def tidy() -> tuple[int, float]:
    """Collect what was made since the last tidy, then freeze the survivors. Returns (frozen, ms).

    The 2-hour soak had a full collection every ~10 minutes at ~70 ms: everything made after start-up
    (chat, encoders, plugins, libraries loaded on first use) was walked each time, and the frame that
    ran into it was late. Collecting every few minutes walks only what is new, a few ms, and once frozen
    those objects are skipped by every later collection. Freezing only skips the cycle finder: objects
    that die later are still freed as usual, unless they are in a reference cycle, and settle() takes
    care of those when the show ends."""
    began = time.perf_counter()
    gc.collect()
    gc.freeze()
    ms = 1000 * (time.perf_counter() - began)
    if ms >= SLOW_GC_MS:
        log.info("python tidy took %.0f ms (%d objects kept aside)", ms, gc.get_freeze_count())
    return gc.get_freeze_count(), ms


THREADS_MOVED = 20  # log the thread count when it moved this much since the last line
THREADS_EVERY_S = 600.0  # and every 10 minutes anyway
_threads_logged: tuple[float, int] | None = None


def thread_census() -> tuple[int, dict[str, int]]:
    """(threads in the whole process, Python threads by kind). Kinds drop the numbers and ids at the end of
    a name ("sprigcast-work_2" and "camera-cam1" count as "sprigcast-work" and "camera"). The rest of the
    process's threads are native: Qt, FFmpeg's codec threads, drivers, Windows' audio."""
    import re
    import threading

    kinds: dict[str, int] = {}
    for thread in threading.enumerate():
        kind = re.sub(r"([-_ ][^-_ ]*\d[^-_ ]*)+$", "", thread.name) or thread.name
        kind = re.sub(r"-[a-z0-9]{6,}$", "", kind)
        kinds[kind] = kinds.get(kind, 0) + 1
    total = sum(kinds.values())
    try:
        import psutil

        total = psutil.Process().num_threads()
    except Exception:
        pass
    return total, kinds


def watch_threads(now: float | None = None) -> str | None:
    """Once a minute: log the threads when their number moved a lot (or every 10 minutes). Returns the line."""
    global _threads_logged
    now = time.monotonic() if now is None else now
    total, kinds = thread_census()
    if _threads_logged is not None:
        at, logged = _threads_logged
        if abs(total - logged) < THREADS_MOVED and now - at < THREADS_EVERY_S:
            return None
    _threads_logged = (now, total)
    python = sum(kinds.values())
    top = ", ".join(f"{k} {n}" for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))[:8])
    line = f"threads {total} (python {python}: {top}; native {max(0, total - python)})"
    log.info(line)
    return line


MEMORY_EVERY_S = 600.0  # a memory line this often, so a slow climb shows where it went (not only at stops)
_memory_logged: float | None = None


def watch_memory(now: float | None = None) -> str | None:
    """Every 10 minutes: one memory line, with what Python keeps aside (frozen objects are only looked
    through again at rest; a climb there is cycles left behind since). Returns the line."""
    global _memory_logged
    now = time.monotonic() if now is None else now
    if _memory_logged is not None and now - _memory_logged < MEMORY_EVERY_S:
        return None
    _memory_logged = now
    line = f"memory now: {memory_line()}; kept aside {gc.get_freeze_count():,}"
    log.info(line)
    return line


def settle() -> float:
    """At rest (nothing recorded or streamed): look through everything once, frozen included, so cycles
    from the show (a stopped encoder, a closed window) are freed; then freeze again. Returns ms."""
    began = time.perf_counter()
    gc.unfreeze()
    gc.collect()
    gc.freeze()
    return 1000 * (time.perf_counter() - began)


HIGHEST, ABOVE_NORMAL = 2, 1  # Windows' THREAD_PRIORITY_* values


# Stall watch: Python's lock held so long that nothing else ran. The 2026-11-16 soak had hitches of
# 280 and 750 ms that landed in whichever step the render thread was in (a camera, the canvases), with
# no garbage collection at the time. A small thread wakes every 50 ms; when it wakes far too late,
# it notes where every busy thread is right then, which names the one that held the lock.
STALL_MS = 200.0
STALL_EVERY_S = 10.0
_IDLE_CALLS = {"wait", "sleep", "get", "select", "_wait_for_tstate_lock", "join", "acquire", "run_forever",
               "_run_once", "accept", "recv", "read", "readinto", "wait_for", "exec", "_worker"}


def busy_stacks(skip: set[int] | None = None) -> str:
    """"name at file.py:line func <- file.py:line func" for each thread not waiting, newest call first."""
    import os
    import threading
    import traceback

    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        if skip and ident in skip:
            continue
        stack = traceback.extract_stack(frame)[::-1]
        if not stack or stack[0].name in _IDLE_CALLS:
            continue
        where = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in stack[:3])
        parts.append(f"{names.get(ident, ident)} at {where}")
    return "; ".join(parts) or "no Python thread busy (native code or the system)"


def watch_stalls(clock=time.perf_counter, interval: float = 0.05) -> None:
    """Start the stall watch (once). Logs at most one line every STALL_EVERY_S."""
    import threading

    if any(t.name == "sprigcast-stall-watch" for t in threading.enumerate()):
        return

    def run():
        me = {threading.get_ident()}
        logged = float("-inf")
        last = clock()
        while True:
            time.sleep(interval)
            now = clock()
            late = 1000 * (now - last - interval)
            last = now
            if late >= STALL_MS and now - logged >= STALL_EVERY_S:
                logged = now
                log.info("everything waited %.0f ms; busy then: %s", late, busy_stacks(me))
            _check_window(now)
            _check_busy(now)
    threading.Thread(target=run, name="sprigcast-stall-watch", daemon=True).start()


# A step that should be quick (a render tick) can be held up without Python's lock held, inside the graphics
# driver: then nothing else stops and "everything waited" never shows. The step says when it starts and ends;
# if it runs past BUSY_STALL_S the stall watch notes where its thread is, and the step's own log line says so.
BUSY_STALL_S = 0.15
_busy: dict[str, list] = {}  # name -> [thread ident, started (perf_counter), where or None]


def busy_begin(name: str) -> None:
    import threading

    _busy[name] = [threading.get_ident(), time.perf_counter(), None]


def busy_end(name: str) -> str | None:
    """Where the step's thread was when it ran long, or None."""
    entry = _busy.pop(name, None)
    return entry[2] if entry is not None else None


def _check_busy(now: float) -> None:
    for entry in list(_busy.values()):
        if entry[2] is not None or now - entry[1] < BUSY_STALL_S:
            continue
        frame = sys._current_frames().get(entry[0])
        if frame is None:
            continue
        import os
        import traceback

        stack = traceback.extract_stack(frame)[::-1][:4]
        del frame
        entry[2] = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in stack) or "?"


# The window's own thread: a timer on it beats every UI_BEAT_MS; when the beat stops for UI_STALL_MS the stall
# watch notes where that thread is, and logs it with how long the window didn't answer once it answers again.
# (The PC saw the window busy 1.6 s some 10 s after a minimized start, with nothing in the log.)
UI_BEAT_MS = 100
UI_STALL_MS = 250.0
_ui = {"beat": None, "ident": None, "where": None, "since": 0.0, "clock": time.perf_counter}


def ui_beat() -> None:
    """Call on the window's thread every UI_BEAT_MS (a QTimer in main.py)."""
    import threading

    now = _ui["clock"]()
    if _ui["where"] is not None:
        log.info("the window didn't answer for %.0f ms; it was at %s", 1000 * (now - _ui["since"]), _ui["where"])
        _ui["where"] = None
    _ui["beat"], _ui["ident"] = now, threading.get_ident()


def ui_rest() -> None:
    """The window is closing: stop watching it (the beat stops with it)."""
    _ui["beat"], _ui["where"] = None, None


def _check_window(now: float) -> None:
    beat, ident = _ui["beat"], _ui["ident"]
    if beat is None or _ui["where"] is not None or 1000 * (now - beat) < UI_STALL_MS:
        return
    frame = sys._current_frames().get(ident)
    if frame is None:
        return
    _ui["since"] = beat
    _ui["where"] = _where(frame)
    del frame


def _where(frame, depth: int = 5) -> str:
    """Where a thread is: its innermost frames, file:line name, innermost first. Inside an import, Python's
    own import machinery is left out and the module being loaded is named (the PC saw 2.4 s and 5.7 s in
    "<frozen importlib…>" with no clue which module)."""
    import os
    import traceback

    importing = []
    walk = frame
    while walk is not None:
        if walk.f_code.co_name == "_find_and_load" and walk.f_code.co_filename.startswith("<frozen"):
            name = walk.f_locals.get("name")
            if isinstance(name, str):
                importing.append(name)
        walk = walk.f_back
    del walk
    stack = traceback.extract_stack(frame)[::-1]
    if importing:
        stack = [f for f in stack if not f.filename.startswith("<frozen")]
    text = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in stack[:depth]) or "?"
    if importing:  # outermost first: what the code asked for, then what that pulled in
        text = f"loading {' > '.join(importing[::-1])}: {text}"
    return text


def raise_thread_priority(level: int = HIGHEST) -> bool:
    """Run the calling thread at Windows' "highest" (or `level`) thread priority. True if it took effect."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        return bool(kernel32.SetThreadPriority(kernel32.GetCurrentThread(), level))
    except Exception as exc:
        log.info("could not raise thread priority: %s", exc)
        return False


def memory_line() -> str:
    """Where memory is, for the log when a recording or the stream stops: what Task Manager shows, what is
    committed, how many Python objects (and which kinds most), and how many threads. Next to each other
    over a long show they tell a leak (one count climbing) from the graphics driver or the heap holding on."""
    import collections
    import threading

    parts = []
    try:
        import psutil

        info = psutil.Process().memory_info()
        shown = None
        if sys.platform == "win32":
            from sprigcast.app.resources import windows_private_working_set

            shown = windows_private_working_set()
        parts.append(f"Task Manager {(shown or info.rss) / 1e6:.0f} MB")
        committed = getattr(info, "private", None)
        if committed:
            parts.append(f"committed {committed / 1e6:.0f} MB")
    except Exception:
        pass
    objects = gc.get_objects()
    kinds = collections.Counter(map(type, objects)).most_common(5)
    parts.append(f"python objects {len(objects):,} (" + ", ".join(f"{k.__name__} {n:,}" for k, n in kinds) + ")")
    del objects
    parts.append(f"python threads {threading.active_count()}")
    return "; ".join(parts)


TRIM_KEEP_MB = 160  # what stays in memory at rest: the pages touched last (the window, the render loop)


def trim_working_set(keep_mb: float = TRIM_KEEP_MB, kernel32=None) -> bool:
    """Windows: hand back to the system the pages the app hasn't touched in a while (what Task Manager counts),
    keeping the last-used keep_mb.

    After a long show the app keeps hundreds of MB of pages it touched once (encoder start-up, the clips
    buffer, frames in flight) that it won't touch again at rest. This used to empty the whole working set (14
    MB in Task Manager), so the next look at the window brought every page of the window, Qt and the graphics
    driver back one by one: SprigCast started minimized, or minimized at rest, took seconds to open fully.
    Windows trims the least recently used pages first, so the window and the render loop stay. True if
    Windows took it."""
    if sys.platform != "win32" and kernel32 is None:
        return False
    try:
        import ctypes

        if kernel32 is None:
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetProcessWorkingSetSizeEx.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t,
                                                             ctypes.c_uint32]
        keep = int(keep_mb * 1024 * 1024)
        # Soft limits (no QUOTA_LIMITS_HARDWS_* flags): the app grows past them again whenever it needs to.
        return bool(kernel32.SetProcessWorkingSetSizeEx(kernel32.GetCurrentProcess(), keep // 4, keep, 0))
    except Exception as exc:
        log.info("could not trim memory: %s", exc)
        return False


def private_mb() -> float:
    """The app's private memory in MB (Task Manager's "Memory" is close to it), for log lines; 0 if unknown."""
    try:
        import psutil

        info = psutil.Process().memory_info()
        return getattr(info, "private", info.rss) / 1e6  # private: Windows only (cheap, unlike a USS walk)
    except Exception:
        return 0.0
