import gc

from sprigcast.core import pacing


def test_slow_collections_are_remembered_for_the_late_frame_line(monkeypatch):
    clock = iter([100.0, 100.002, 200.0, 200.060])
    monkeypatch.setattr(pacing.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(pacing, "last_pause", (0.0, 0.0, 0))
    pacing._on_gc("start", {"generation": 0})
    pacing._on_gc("stop", {"generation": 0, "collected": 3})  # 2 ms: not worth a mention
    assert pacing.pause_since(99.0) == ""
    pacing._on_gc("start", {"generation": 2})
    pacing._on_gc("stop", {"generation": 2, "collected": 10})  # 60 ms
    assert pacing.pause_since(199.9) == "; python cleanup 60 ms"
    assert pacing.pause_since(200.1) == ""  # ended before this frame began


def test_tame_gc_freezes_start_up_objects():
    before = gc.get_threshold()
    try:
        pacing.tame_gc()
        assert gc.get_freeze_count() > 0
        assert gc.get_threshold()[0] == pacing.GEN0_THRESHOLD
    finally:
        gc.unfreeze()
        gc.set_threshold(*before)


def test_watch_gc_is_added_once():
    try:
        pacing.watch_gc()
        pacing.watch_gc()
        assert gc.callbacks.count(pacing._on_gc) == 1
    finally:
        while pacing._on_gc in gc.callbacks:
            gc.callbacks.remove(pacing._on_gc)


def test_thread_priority_is_windows_only(monkeypatch):
    monkeypatch.setattr(pacing.sys, "platform", "linux")
    assert pacing.raise_thread_priority() is False


def test_tidy_freezes_what_survives_and_settle_frees_cycles_from_the_show():
    """Soak: a full collection every ~10 min took ~70 ms. Tidying often walks only what is new."""
    import gc

    from sprigcast.core import pacing

    pacing.tidy()
    before = gc.get_freeze_count()
    kept = [[n] for n in range(1000)]
    frozen, _ms = pacing.tidy()
    assert frozen >= before + 1000  # the survivors are frozen: later collections skip them
    loop = []
    loop.append(loop)  # a cycle that dies after being frozen
    pacing.tidy()
    ref = __import__("weakref").ref
    class Box:  # noqa: D401
        pass
    box = Box()
    box.me = box
    probe = ref(box)
    pacing.tidy()
    del box
    gc.collect()
    assert probe() is not None  # frozen: the cycle finder doesn't see it
    pacing.settle()
    assert probe() is None  # at rest everything is looked at once
    del kept, loop


def test_the_per_frame_walks_leave_no_reference_cycles():
    """Two nested functions that called themselves left a cycle every frame for the collector to find."""
    from sprigcast.core.scene import (
        Canvas,
        Scene,
        SceneCollection,
        SceneItem,
        SourceConfig,
    )
    from sprigcast.render.engine import _walk_visible

    col = SceneCollection(canvases=[Canvas(id="h", width=64, height=36)],
                          sources=[SourceConfig(id="c", kind="color", name="C")],
                          scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source="c")])],
                          active_scenes={"h": "a"})
    gc.collect()
    gc.set_debug(gc.DEBUG_SAVEALL)
    try:
        visible: set[str] = set()
        for _ in range(50):
            col.part_order()
            _walk_visible(col, {s.id: s for s in col.sources}, visible, col.scene("a"), set(), ("a",))
        gc.collect()
        assert gc.garbage == [] and visible == {"c"}
    finally:
        gc.set_debug(0)
        gc.garbage.clear()


def test_a_cpu_by_thread_line_goes_in_the_log(caplog):
    import threading
    import time as _time

    from sprigcast.core import pacing

    pacing._cpu_watch = None
    assert pacing.watch_cpu() is None  # the first call starts counting
    stop = threading.Event()
    spun = threading.Event()

    def busy():
        end = _time.monotonic() + 0.2
        while _time.monotonic() < end:
            pass
        spun.set()
        stop.wait(5)

    worker = threading.Thread(target=busy, name="sprigcast-busy")
    worker.start()
    spun.wait(5)
    assert pacing.watch_cpu() is None  # not 5 minutes yet
    with caplog.at_level("INFO"):
        line = pacing.watch_cpu(now=0.0)  # asked for: said now
    stop.set()
    worker.join()
    assert line.startswith("CPU by thread:") and "sprigcast-busy" in line and line in caplog.text


def test_the_stall_watch_never_reads_running_threads_stacks(caplog):
    """Printing every thread's stack while Python's lock was held (SPRIGCAST_STALL_DUMPS=1) read frames that were
    going away and killed SprigCast on the PC (2026-11-19 09:52, the old engine, 1 s after a stream started; the
    2026-11-18 12:21 crash was the same). The watch doesn't do it any more, also when the old switch is set."""
    import inspect

    from sprigcast.core import pacing

    with caplog.at_level("WARNING"):
        assert pacing.old_stall_switches({}) == [] and caplog.text == ""
        assert pacing.old_stall_switches({"SPRIGCAST_STALL_DUMPS": "1", "SPRIGCAST_STALL_DUMP_MS": "200"})
    assert "SPRIGCAST_STALL_DUMPS / SPRIGCAST_STALL_DUMP_MS is set but no longer used" in caplog.text
    watch = inspect.getsource(pacing.watch_stalls)
    assert "old_stall_switches()" in watch and "dump_traceback" not in watch and not hasattr(pacing, "_StallDump")
    uses = [line for line in inspect.getsource(pacing).splitlines() if "faulthandler.dump_traceback_later(" in line]
    assert len(uses) == 1 and "exit=True" in uses[0]  # only the exit guard, when SprigCast is ending anyway


def test_only_the_main_thread_frees_what_the_cycle_collector_finds():
    """A collection on another thread used to free a closed page's Qt widgets there, which crashed the test run
    (2026-11-18). Now what it finds waits for the main thread's next collection."""
    import threading

    from sprigcast.core import pacing

    pacing.collect_on_main_thread()

    class Node:
        def __init__(self):
            self.me = self

    def alive():
        return sum(1 for o in gc.get_objects() if type(o) is Node)

    def worker():
        Node()
        gc.collect()  # a collection on this thread finds the cycle but frees nothing

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert alive() == 1 and pacing.deferred_count() >= 1
    gc.collect()
    gc.collect()
    assert alive() == 0 and pacing.deferred_count() == 0


def test_trimming_memory_runs_off_the_window_thread(monkeypatch):
    """Windows walking the app's pages took 60-66 ms on the PC just after start: never on the window's thread."""
    import threading

    from sprigcast.core import pacing

    seen, done = [], threading.Event()
    monkeypatch.setattr(pacing, "trim_working_set", lambda: seen.append(threading.current_thread()) or True)
    pacing.trim_working_set_later(lambda trimmed: (seen.append(trimmed), done.set()))
    assert done.wait(5)
    assert seen[0] is not threading.main_thread() and seen[1] is True


def test_warm_up_keeps_its_imports_aside():
    import gc

    from sprigcast.core import warm

    gc.unfreeze()
    try:
        _keep = [{} for _ in range(100)]
        warm.keep_aside()
        assert gc.get_freeze_count() >= 100
    finally:
        gc.unfreeze()


def test_a_tidy_can_say_what_kinds_of_garbage_it_freed(monkeypatch, caplog):
    import gc
    import logging

    from sprigcast.core import pacing

    monkeypatch.setenv("SPRIGCAST_GC_TYPES", "1")
    monkeypatch.setattr(pacing, "SLOW_GC_MS", -1.0)

    class Knot:
        pass

    for _ in range(50):
        a, b = Knot(), Knot()
        a.other, b.other = b, a
    del a, b
    try:
        with caplog.at_level(logging.INFO, logger="sprigcast.core.pacing"):
            pacing.tidy()
        assert any("Knot" in r.getMessage() for r in caplog.records)
        assert gc.garbage == [] and not gc.get_debug() & gc.DEBUG_SAVEALL
        assert not any(type(o).__name__ == "Knot" for o in gc.get_objects())  # freed, not kept aside
    finally:
        gc.unfreeze()


def test_looking_where_a_thread_is_never_moves_its_objects_to_the_looker():
    """A frame taken from another thread keeps that function's objects alive, and whoever lets the frame go frees
    them, on that thread. The stall watch freed the window's graphics scene that way; Qt couldn't stop the scene's
    timer from there, and its next tick crashed SprigCast (about one start in 40 on GitHub, 2026-11-19/20). The
    looks never hold a frame now: an object made in a function is always freed by the thread that ran it."""
    import threading

    died = []

    class Thing:
        def __del__(self):
            died.append(threading.current_thread().name)

    inside, looked, seen = threading.Event(), threading.Event(), {}

    def work():
        thing = Thing()  # noqa: F841 (it lives as long as this function runs)
        inside.set()
        looked.wait(5)

    def look():
        inside.wait(5)
        seen["stacks"] = pacing.thread_stacks()
        seen["busy"] = pacing.busy_stacks()
        looked.set()

    worker = threading.Thread(target=work, name="worker")
    looker = threading.Thread(target=look, name="looker")
    worker.start(), looker.start()
    worker.join(5), looker.join(5)
    assert died == ["worker"]
    stack = seen["stacks"][worker.ident]
    assert any(name == "work" and file.endswith("test_pacing.py") for file, _line, name in stack)


def test_nothing_takes_another_threads_frame():
    """sys._current_frames hands out frames that keep other threads' objects alive (see above)."""
    from pathlib import Path

    import sprigcast

    users = [str(p) for p in Path(sprigcast.__file__).parent.rglob("*.py")
             if "sys._current_frames()" in p.read_text(encoding="utf-8")]
    assert users == []


def test_a_bloom_switch_holds_collection_and_housekeeping_a_moment(monkeypatch):
    """PC live 2026-11-21: Python's collection (16 ms) and the tidy (23 ms) landed right after a switch."""
    import gc

    from sprigcast.core import pacing

    monkeypatch.setattr(pacing, "SWITCH_QUIET_S", 0.2)
    ran = []
    try:
        pacing.bloom_switched()
        assert not gc.isenabled()
        pacing.housekeeping("tidy", lambda: ran.append(1))
        assert ran == []  # skipped: the next round comes later
        import time

        time.sleep(0.5)
        assert gc.isenabled()
    finally:
        gc.enable()
        pacing._quiet_until, pacing._gc_held = 0.0, False
