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
import threading
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


# Cleanup on the window's thread only ------------------------------------------------------------------------------
# A collection runs on whichever thread happens to allocate. When one on a stream, encoder or render thread finds
# Qt objects in a reference cycle (a closed page's widgets), PySide defers deleting them to the main thread, and
# for some of them it then called a null deleter: the test suite crashed that way every full run (2026-11-18,
# a hotkeys page's widgets freed on a stream link's thread), and the app could too. A collection on another
# thread now frees nothing (DEBUG_SAVEALL): what it finds is handed to the main thread, which lets go of it
# when it next collects, and it is freed there.

_main_ident = threading.main_thread().ident
_deferred: list[list] = []  # garbage found by other threads' collections, held until the main thread collects
_debug_before = 0


def _cleanup_on_main(phase: str, info: dict) -> None:
    global _debug_before
    if threading.get_ident() == _main_ident:
        if phase == "start" and _deferred:
            _deferred.clear()  # unreachable again: this collection (or the next older one) frees it here
        return
    if phase == "start":
        _debug_before = gc.get_debug()
        gc.set_debug(_debug_before | gc.DEBUG_SAVEALL)
        return
    gc.set_debug(_debug_before)
    if gc.garbage:
        _deferred.append(gc.garbage[:])
        gc.garbage.clear()


def collect_on_main_thread() -> None:
    """Only the main thread frees what Python's cycle collector finds (see above). Call once, at start-up."""
    if _cleanup_on_main not in gc.callbacks:
        gc.callbacks.insert(0, _cleanup_on_main)


def deferred_count() -> int:
    """Objects other threads' collections found that the main thread hasn't let go of yet (tests)."""
    return sum(len(batch) for batch in _deferred)


def watch_gc() -> None:
    if _on_gc not in gc.callbacks:
        gc.callbacks.append(_on_gc)


def pause_since(t: float) -> str:
    """" python cleanup N ms" when a slow collection ended after perf_counter time `t`, else ""."""
    ended, ms, _gen = last_pause
    return f"; python cleanup {ms:.0f} ms" if ended >= t else ""


def tame_gc() -> None:
    """Collect the young objects, freeze everything start-up made, and run young collections less often.

    Young only: a full collection here walked every object start-up made, and held the window 44-63 ms on the PC
    just after start (2026-11-18). Start-up's own old objects are almost all kept for the whole session; any cycle
    among them is freed by settle() at rest."""
    began = time.perf_counter()
    gc.collect(1)
    gc.freeze()
    _gen0, gen1, gen2 = gc.get_threshold()
    gc.set_threshold(GEN0_THRESHOLD, gen1, gen2)
    log.info("python cleanup: froze %d start-up objects in %.0f ms", gc.get_freeze_count(),
             1000 * (time.perf_counter() - began))


# Each tidy walks what was made since the last one, and frees what of it is garbage. Every 5 minutes that was 15-73
# ms on the user's PC (2026-11-15); every minute, a fifth of that, until Move transitions: each left garbage that
# lived past the young collections, and the minute's tidy freed 55-68 thousand objects in 65-72 ms, holding the
# render thread 100-113 ms (PC Move soak, 2026-11-18). Every 10 seconds it is a sixth of that, inside a frame's
# idle time. SPRIGCAST_GC_TYPES=1 logs what kinds of objects each slow tidy freed, to find where they come from.
TIDY_EVERY_MS = 10_000
MINUTE_MS = 60_000  # the rest of the housekeeping (threads, memory, CPU)


def tidy() -> tuple[int, float]:
    """Collect what was made since the last tidy, then freeze the survivors. Returns (frozen, ms).

    The 2-hour soak had a full collection every ~10 minutes at ~70 ms: everything made after start-up
    (chat, encoders, plugins, libraries loaded on first use) was walked each time, and the frame that
    ran into it was late. Collecting every few minutes walks only what is new, a few ms, and once frozen
    those objects are skipped by every later collection. Freezing only skips the cycle finder: objects
    that die later are still freed as usual, unless they are in a reference cycle, and settle() takes
    care of those when the show ends."""
    import os

    kinds = os.environ.get("SPRIGCAST_GC_TYPES")
    if kinds:
        before = gc.get_debug()
        gc.set_debug(before | gc.DEBUG_SAVEALL)
    began = time.perf_counter()
    gc.collect()
    ms = 1000 * (time.perf_counter() - began)
    freed = ""
    if kinds:
        gc.set_debug(before)
        from collections import Counter

        counts = Counter(type(o).__qualname__ for o in gc.garbage)
        gc.garbage.clear()
        gc.collect()  # saved, not freed: now freed for real (never frozen)
        if ms >= SLOW_GC_MS:
            freed = "; freed " + ", ".join(f"{n} {kind}" for kind, n in counts.most_common(12))
    gc.freeze()
    if ms >= SLOW_GC_MS:
        log.info("python tidy took %.0f ms (%d objects kept aside)%s", ms, gc.get_freeze_count(), freed)
    return gc.get_freeze_count(), ms


# The minute's housekeeping (tidy, the thread count, the memory line) runs on the window's thread and holds
# Python's lock while it works. On the PC (2026-11-17) frames 20 and 18 ms late landed on it, always at the same
# second of the minute. Each piece now starts right after the render thread finishes a frame, so it uses the
# frame's idle time, and the pieces run seconds apart rather than back to back.
_frame_done = None  # threading.Event the render thread sets after each frame
_frame_at = 0.0  # perf_counter time of the last frame
# Wait at most this for the frame in progress: one frame at 60 fps and a little. It was 50 ms, and the wait is on
# the window's thread: when the frame's end didn't come in time (the new engine's render tick, a busy moment), the
# window stood still the whole 50 ms and then did its work on top (71 ms as Settings opened, PC 2026-11-19).
FRAME_WAIT_S = 0.02
HOUSEKEEPING_SLOW_MS = 5.0
_housekeeping_logged = float("-inf")


def frame_ended() -> None:
    """The render thread finished a frame (cheap: called every frame)."""
    global _frame_done, _frame_at
    if _frame_done is None:
        import threading

        _frame_done = threading.Event()
    _frame_at = time.perf_counter()
    _frame_done.set()


def at_frame_end(wait_s: float = FRAME_WAIT_S) -> None:
    """Wait (briefly) for the render thread to finish the frame it's on, so work that holds Python's lock starts
    with a whole frame's idle time ahead of it. Returns at once when nothing is rendering."""
    done = _frame_done
    if done is None or time.perf_counter() - _frame_at > 1.0:
        return
    done.clear()
    done.wait(wait_s)


def housekeeping(name: str, work) -> None:
    """Run one piece of the minute's housekeeping at a frame's end; log it when it held things up."""
    at_frame_end()
    began = time.perf_counter()
    work()
    global _housekeeping_logged
    ms = 1000 * (time.perf_counter() - began)
    if ms >= HOUSEKEEPING_SLOW_MS and began - _housekeeping_logged >= 60.0:
        _housekeeping_logged = began
        log.info("housekeeping: %s took %.1f ms", name, ms)


# Where threads are, without ever holding one of their frames. A frame object taken from another thread
# (sys._current_frames) keeps that function's local objects alive: when the function returns while the frame is
# still held, they are freed by whoever lets the frame go, on that thread. The stall watch held the window's
# frame inside theme._shadow_picture, so its graphics scene was freed on the watch's thread, where Qt can't stop
# the scene's 2-second timer; the timer's tick then went to the freed object: the "access violation" about one
# start in 40 had on GitHub, 2 s after the card shadow was warmed (2026-11-19/20), and a risk for any Qt object a
# busy function of the window's holds. Python's fault handler writes the same lines while this thread holds
# Python's lock, straight from the interpreter's own records: no frame objects, nothing kept alive.
_stack_file = None
_stack_lock = threading.Lock()


def thread_stacks() -> dict[int, list[tuple[str, int, str]]]:
    """Thread ident -> [(file, line, function), ...], innermost first, for every Python thread."""
    import faulthandler
    import re
    import tempfile

    global _stack_file
    with _stack_lock:
        try:
            if _stack_file is None:
                _stack_file = tempfile.TemporaryFile()  # noqa: SIM115 (kept open for the whole run)
            _stack_file.seek(0)
            _stack_file.truncate()
            faulthandler.dump_traceback(_stack_file, all_threads=True)
            _stack_file.seek(0)
            text = _stack_file.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - no temp folder, a closed file: nothing to say then
            return {}
    stacks: dict[int, list[tuple[str, int, str]]] = {}
    current: list[tuple[str, int, str]] | None = None
    for line in text.splitlines():
        head = re.match(r"(?:Current thread|Thread) 0x([0-9a-fA-F]+)", line)
        if head:
            current = stacks.setdefault(int(head.group(1), 16), [])
            continue
        at = re.match(r'\s+File "(.*)", line (\d+) in (.*)$', line)
        if at and current is not None:
            current.append((at.group(1), int(at.group(2)), at.group(3)))
    return stacks


def _told(stack, depth: int) -> str:
    import os

    return " <- ".join(f"{os.path.basename(file)}:{line} {name}" for file, line, name in stack[:depth])


def window_at() -> str:
    """Where the window's thread is right now (its three innermost lines), or "" when unknown."""
    ident = _ui.get("ident")
    return _told(thread_stacks().get(ident, []), 3) if ident is not None else ""


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
    if sys.platform == "win32":  # whose the native ones are, from a thread of its own (it opens each thread)
        def name_them():
            native = native_threads()
            if native:
                log.info("native threads by who started them: %s", native)

        threading.Thread(target=name_them, name="sprigcast-thread-names", daemon=True).start()
    return line


_native_labels: dict[int, str] = {}


def native_threads(top: int = 10) -> str:
    """Windows: the threads that aren't Python's, counted by their own description (sprigcore names its threads)
    or the library they were started in: "nvwgf2umx.dll 20, ntdll.dll 18, SprigCast engine frames 1, ...". The
    light-PC test (2026-11-19) saw "native 88" and couldn't say whose. Empty elsewhere or when it can't be read."""
    if sys.platform != "win32":
        return ""
    import re

    try:
        from sprigcast.tools.cpuwhere import _windows_thread_times, native_thread_label

        ours = {t.native_id for t in threading.enumerate()}
        ids = [tid for tid in _windows_thread_times() if tid not in ours]
        counts: dict[str, int] = {}
        for tid in ids:
            if tid not in _native_labels:
                _native_labels[tid] = (native_thread_label(tid) or "unknown").removeprefix("threads started in ")
            kind = re.sub(r"([-_ #][^-_ #]*\d[^-_ #]*)+$", "", _native_labels[tid]) or _native_labels[tid]
            counts[kind] = counts.get(kind, 0) + 1
        for tid in [t for t in _native_labels if t not in set(ids)]:
            del _native_labels[tid]  # thread ids get reused
        return ", ".join(f"{k} {n}" for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top])
    except Exception:  # noqa: BLE001 (a diagnostic: never in the way)
        return ""


CPU_EVERY_S = 300.0  # a CPU-by-thread line this often (watch_cpu)
_cpu_watch = None


def watch_cpu(now: float | None = None) -> str | None:
    """Every 5 minutes: which threads used the CPU since the last line, in % of one core, busiest first
    (tools/cpuwhere.py: Python threads by name, native ones by their own name or the library they started
    in). PC tests read it from the log instead of running a separate tool. Returns the line."""
    global _cpu_watch
    from sprigcast.tools.cpuwhere import CpuWatch

    if _cpu_watch is None:
        _cpu_watch = CpuWatch()
        return None
    if time.monotonic() - _cpu_watch._at < CPU_EVERY_S and now is None:
        return None
    whole, threads = _cpu_watch.busiest()
    _cpu_watch.restart()
    top = ", ".join(f"{name} {share:.1f}" for name, share in threads[:12] if share >= 0.1)
    line = f"CPU by thread: {whole:.1f}% of the whole CPU; in % of one core: {top or 'idle'}"
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
    from sprigcast.core.memory_map import log_memory_map

    log_memory_map()  # and what holds the committed memory, from a thread of its own
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
# "Everything waited" means a thread held Python's lock, but the watch can only look once it has the lock back,
# when that thread has moved on. It used to have Python's fault handler print every thread's stack from a thread
# of its own while the lock was held (SPRIGCAST_STALL_DUMPS=1): that reads the stacks of threads that are still
# running, and it killed SprigCast three times on the PC (2026-11-17 20:57, 2026-11-18 12:21 and twice on
# 2026-11-19 09:52, each an access violation in python312.dll reading a frame that was going away). It is gone:
# nothing reads a running thread's stack without holding Python's lock any more, except the exit guard below when
# SprigCast is ending anyway. (thread_stacks holds the lock while the lines are written: no thread runs Python then.)
STALL_DUMPS_ENV = ("SPRIGCAST_STALL_DUMPS", "SPRIGCAST_STALL_DUMP_MS")  # no longer read: said once at start
_IDLE_CALLS = {"wait", "sleep", "get", "select", "_wait_for_tstate_lock", "join", "acquire", "run_forever",
               "_run_once", "_poll", "accept", "recv", "read", "readinto", "wait_for", "exec", "_worker"}


def busy_stacks(skip: set[int] | None = None) -> str:
    """"name at file.py:line func <- file.py:line func" for each thread not waiting, newest call first."""
    import threading

    names = {t.ident: t.name for t in threading.enumerate()}
    parts = []
    for ident, stack in thread_stacks().items():
        if skip and ident in skip:
            continue
        if not stack or stack[0][2] in _IDLE_CALLS:
            continue
        parts.append(f"{names.get(ident, ident)} at {_told(stack, 3)}")
    return "; ".join(parts) or "no Python thread busy (native code or the system)"


_watch_args: dict = {}  # how the stall watch was started, to start it again (keep_watching)
WATCH_CHECK_BEATS = 50  # the window's beat looks every 5 s that the stall watch still runs


def keep_watching() -> bool:
    """Start the stall watch again if it stopped (it never should). True if it had to."""
    import threading

    if not _watch_args or any(t.name == "sprigcast-stall-watch" and t.is_alive() for t in threading.enumerate()):
        return False
    log.warning("stall watch had stopped: started again")
    watch_stalls(**_watch_args)
    return True


def old_stall_switches(environ=None) -> list[str]:
    """The stall-dump switches that are set: they do nothing now, and the log says so."""
    import os

    found = [name for name in STALL_DUMPS_ENV if (environ if environ is not None else os.environ).get(name)]
    if found:
        log.warning("%s is set but no longer used: printing every thread's stack during a stall could crash "
                    "SprigCast", " / ".join(found))
    return found


def watch_stalls(clock=time.perf_counter, interval: float = 0.05) -> None:
    """Start the stall watch (once). Logs at most one line every STALL_EVERY_S."""
    import threading

    if any(t.name == "sprigcast-stall-watch" for t in threading.enumerate()):
        return
    _watch_args.update(clock=clock, interval=interval)
    old_stall_switches()

    def run():
        me = {threading.get_ident()}
        thread_stacks()  # its file made now, not in the middle of the first stall
        logged = float("-inf")
        last = clock()
        failed_at = float("-inf")
        while True:
            time.sleep(interval)
            try:  # nothing here may ever stop the watch
                now = clock()
                late = 1000 * (now - last - interval)
                last = now
                if late >= STALL_MS and now - logged >= STALL_EVERY_S:
                    logged = now
                    log.info("everything waited %.0f ms; busy then: %s", late, busy_stacks(me))
                _check_window(now)
                _check_busy(now)
            except Exception:  # noqa: BLE001
                if time.monotonic() - failed_at >= STALL_EVERY_S:  # said once in a while, never a flood
                    failed_at = time.monotonic()
                    log.exception("stall watch: a check failed; still watching")
    threading.Thread(target=run, name="sprigcast-stall-watch", daemon=True).start()


# SprigCast closing: if it hasn't ended EXIT_LIMIT_S after the window closed, it ends anyway. faulthandler's
# timer does it from a thread of its own, without Python's lock, so a thread stuck holding the lock (or in the
# graphics driver) can't stop it; every thread's stack goes to the log first. The native simple run
# (2026-11-18) had SprigCast still running 15 s after its window was told to close, and it had to be killed.
EXIT_LIMIT_S = 20.0
_dump_lock = threading.Lock()
_closing: dict = {"at": None, "file": None, "on": False}  # on: set by app/main.py (never in tests)


def exit_guard(limit_s: float = EXIT_LIMIT_S) -> None:
    """The window is closing (everything worth keeping is saved): end the process if it's still here after
    limit_s. Once."""
    import faulthandler
    import logging as _logging

    with _dump_lock:
        if _closing["at"] is not None or not _closing["on"]:
            return
        _closing["at"] = time.monotonic()
        stream = next((h.stream for h in _logging.getLogger().handlers
                       if isinstance(h, _logging.FileHandler) and getattr(h, "stream", None) is not None), None)
        try:
            if stream is not None:
                stream.write(f"(if SprigCast is still closing in {limit_s:.0f} s, it ends then; "
                             "the stacks below show what held it)\n")
                stream.flush()
            faulthandler.dump_traceback_later(limit_s, repeat=False, file=stream or sys.stderr, exit=True)
            _closing["file"] = stream
        except (RuntimeError, ValueError, OSError, AttributeError):
            log.warning("exit guard not set", exc_info=True)


def use_exit_guard(on: bool = True) -> None:
    """SprigCast itself (app/main.py) closes with the guard; tests that close windows don't."""
    _closing["on"] = on


def exit_guard_off() -> None:
    """Let the guard go (tests)."""
    import faulthandler

    with _dump_lock:
        if _closing["at"] is not None:
            faulthandler.cancel_dump_traceback_later()
        _closing["at"], _closing["file"] = None, None


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
        stack = thread_stacks().get(entry[0])
        if stack is None:
            continue
        entry[2] = _told(stack, 4) or "?"


# The window's own thread: a timer on it beats every UI_BEAT_MS; when the beat stops for UI_STALL_MS the stall
# watch notes where that thread is, and logs it with how long the window didn't answer once it answers again.
# (The PC saw the window busy 1.6 s some 10 s after a minimized start, with nothing in the log.)
UI_BEAT_MS = 100
UI_STALL_MS = 250.0
# Fine mode, for the PC's pop-up sweep (SPRIGCAST_UI_STALL_MS=50): every stall from that many ms is logged, with
# where the window's thread was. The beat and the watch then run faster (UI_FINE_BEAT_MS, UI_FINE_WATCH_S) and the
# stack is taken half way, so a short stall is caught while it's still going; one that ends short of the limit is
# dropped. Off unless asked for: it costs a little Python many times a second.
UI_FINE_BEAT_MS = 10
UI_FINE_WATCH_S = 0.01
UI_FINE_MIN_MS = 20.0
_ui = {"beat": None, "ident": None, "where": None, "since": 0.0, "clock": time.perf_counter, "fine": None}


def ui_fine_ms(environ=None) -> float | None:
    """The fine-mode limit asked for in SPRIGCAST_UI_STALL_MS, or None (the usual UI_STALL_MS watch)."""
    import os

    text = (environ if environ is not None else os.environ).get("SPRIGCAST_UI_STALL_MS", "").strip()
    try:
        ms = float(text)
    except ValueError:
        return None
    return max(UI_FINE_MIN_MS, ms) if ms > 0 else None


def watch_window(environ=None) -> tuple[int, float]:
    """Set up the window watch: (the beat's ms, the stall watch's interval in s), fine if asked for."""
    fine = ui_fine_ms(environ)
    _ui["fine"] = fine
    if fine is None:
        return UI_BEAT_MS, 0.05
    log.info("window stalls logged from %.0f ms", fine)
    return UI_FINE_BEAT_MS, UI_FINE_WATCH_S


def ui_beat() -> None:
    """Call on the window's thread every UI_BEAT_MS (a QTimer in main.py)."""
    import threading

    now = _ui["clock"]()
    fine, beat = _ui.get("fine"), _ui["beat"]
    if fine is not None and beat is not None:
        took = 1000 * (now - beat)
        if took >= fine:
            log.info("the window didn't answer for %.0f ms; it was at %s", took,
                     _ui["where"] or "(too short to catch where)")
        _ui["where"] = None
    elif _ui["where"] is not None:
        log.info("the window didn't answer for %.0f ms; it was at %s", 1000 * (now - _ui["since"]), _ui["where"])
        _ui["where"] = None
    _ui["beat"], _ui["ident"] = now, threading.get_ident()
    _ui["beats"] = _ui.get("beats", 0) + 1
    if _ui["beats"] % (WATCH_CHECK_BEATS * (UI_BEAT_MS // UI_FINE_BEAT_MS if fine else 1)) == 0:
        keep_watching()


def ui_rest() -> None:
    """The window is closing: stop watching it (the beat stops with it)."""
    _ui["beat"], _ui["where"] = None, None


def _check_window(now: float) -> None:
    import threading

    beat, ident, fine = _ui["beat"], _ui["ident"], _ui.get("fine")
    limit = UI_STALL_MS if fine is None else fine / 2
    if beat is None or _ui["where"] is not None or 1000 * (now - beat) < limit:
        return
    stack = thread_stacks().get(ident)
    if stack is None:
        return
    where = _where(stack)
    # Who else is running right then: a window held up on a trivial line is waiting for Python's lock, and
    # the thread holding it is among these (PC test 2026-11-17: 328 ms on a one-line check).
    others = busy_stacks({ident, threading.get_ident()})
    if _ui["beat"] != beat:
        return  # it answered meanwhile (fine mode looks often): this stack is from after the stall
    # Set once, whole: the window's thread may answer (ui_beat clears "where") at any moment. Adding to it in
    # a second step met None there once and stopped the watch for the rest of the session (2026-11-17).
    _ui["since"], _ui["where"] = beat, f"{where}; meanwhile {others}"


def _where(stack, depth: int = 12) -> str:
    """Where a thread is: its innermost frames, file:line name, innermost first. Inside an import, Python's
    own import machinery is left out (the PC saw 2.4 s and 5.7 s in "<frozen importlib…>"): the first line
    left is the module being loaded, or the one that asked for it."""
    importing = any(name == "_find_and_load" and file.startswith("<frozen") for file, _line, name in stack)
    if importing:
        stack = [f for f in stack if not f[0].startswith("<frozen")]
    text = _told(stack, depth) or "?"
    return f"loading a module: {text}" if importing else text


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
    parts.append(f"python threads {threading.active_count()} ({thread_names()})")
    return "; ".join(parts)


def thread_names(threads=None) -> str:
    """"sprigcast-audio, sprigcast-worker ×4, Thread ×2": which threads run, to find ones idle for no reason."""
    import collections
    import re
    import threading

    names = collections.Counter(re.sub(r"[-_]\d+", "", t.name).strip() or "thread"
                                for t in (threading.enumerate() if threads is None else threads))
    return ", ".join(f"{name} ×{n}" if n > 1 else name for name, n in sorted(names.items()))


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


def trim_working_set_later(done=None) -> None:
    """trim_working_set on a thread of its own: Windows walking the app's pages took 60-66 ms (PC 2026-11-18),
    the window waiting all that time when it ran there. done(trimmed) is called on that thread."""

    def trim() -> None:
        trimmed = trim_working_set()
        if done is not None:
            done(trimmed)

    threading.Thread(target=trim, name="trim-memory", daemon=True).start()


def private_mb() -> float:
    """The app's private memory in MB (Task Manager's "Memory" is close to it), for log lines; 0 if unknown."""
    try:
        import psutil

        info = psutil.Process().memory_info()
        return getattr(info, "private", info.rss) / 1e6  # private: Windows only (cheap, unlike a USS walk)
    except Exception:
        return 0.0
