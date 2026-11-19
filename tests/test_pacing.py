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


def test_a_stall_dump_names_the_thread_holding_the_lock():
    """The stall watch can only look once Python's lock is free again; faulthandler prints every stack during it."""
    import threading

    from sprigcast.core.pacing import _StallDump, shorten_dump

    me = threading.get_ident()
    text = (f"Thread 0x{me + 1:x} (most recent call first):\n"
            '  File "C:\\\\x\\\\nvenc_d3d11.py", line 597 in encode_texture\n'
            '  File "C:\\\\x\\\\recorder.py", line 10 in _run\n\n'
            f"Current thread 0x{me:x} (most recent call first):\n"
            '  File "C:\\\\x\\\\compositor.py", line 943 in upload\n'
            '  File "C:\\\\x\\\\engine.py", line 1870 in _tick\n')
    short = shorten_dump(text, {me: "render"})
    assert short.startswith("render (holding the lock) at compositor.py:943 upload <- engine.py:1870 _tick")
    assert "nvenc_d3d11.py:597 encode_texture" in short

    assert _StallDump().file is None  # off unless SPRIGCAST_STALL_DUMPS=1: it reads running threads' stacks
    dump = _StallDump(on=True)
    import faulthandler
    import time as _time

    faulthandler.dump_traceback_later(0.05, repeat=False, file=dump.file)
    end = _time.monotonic() + 0.3
    while _time.monotonic() < end:  # holds the lock: faulthandler's own thread prints anyway
        pass
    got = dump.take()
    assert "MainThread at test_pacing.py" in got and "test_a_stall_dump_names_the_thread_holding_the_lock" in got
    assert dump.take() == ""  # read once


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


def test_stall_dumps_can_be_asked_for_shorter_stalls():
    """A 235 ms stall held a media source on the PC (2026-11-18), under the usual 350 ms dump: a test run can ask
    for dumps from 200 ms (never shorter: the watch re-arms the dump every half of it)."""
    from sprigcast.core.pacing import DUMP_AFTER_MIN_S, DUMP_AFTER_S, dump_after_s

    assert dump_after_s({}) == DUMP_AFTER_S
    assert dump_after_s({"SPRIGCAST_STALL_DUMP_MS": "220"}) == 0.22
    assert dump_after_s({"SPRIGCAST_STALL_DUMP_MS": "50"}) == DUMP_AFTER_MIN_S
    assert dump_after_s({"SPRIGCAST_STALL_DUMP_MS": "soon"}) == DUMP_AFTER_S


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
