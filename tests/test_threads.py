import threading
import time

from sprigcast.core import pacing, workers


def test_thread_census_groups_names_by_kind():
    stop = threading.Event()
    threads = [threading.Thread(target=stop.wait, name=f"camera-cam{i}", daemon=True) for i in range(3)]
    for t in threads:
        t.start()
    try:
        total, kinds = pacing.thread_census()
        assert kinds.get("camera") == 3
        assert total >= sum(kinds.values())
    finally:
        stop.set()


def test_thread_lines_only_when_the_count_moves(monkeypatch):
    counts = iter([100, 105, 130, 131, 131])
    monkeypatch.setattr(pacing, "thread_census", lambda: (next(counts), {"MainThread": 1}))
    monkeypatch.setattr(pacing, "_threads_logged", None)
    assert pacing.watch_threads(0.0).startswith("threads 100 (python 1: MainThread 1; native 99)")
    assert pacing.watch_threads(60.0) is None  # moved by 5
    assert pacing.watch_threads(120.0) is not None  # moved by 30
    assert pacing.watch_threads(180.0) is None
    assert pacing.watch_threads(800.0) is not None  # ten minutes on


def test_short_jobs_share_a_few_threads():
    names = set()
    jobs = [workers.run(lambda: names.add(threading.current_thread().name)) for _ in range(40)]
    for job in jobs:
        job.result(5)
    assert 1 <= len(names) <= workers.MAX_WORKERS
    assert all(n.startswith("sprigcast-work") for n in names)


def test_media_decoders_use_a_few_threads():
    from sprigcast.sources.media import decode_threads

    assert decode_threads(1920, 1080) == 2 and decode_threads(1280, 720) == 2
    assert decode_threads(3840, 2160) == 4


def test_a_long_hold_on_pythons_lock_names_the_busy_thread(caplog):
    import logging
    import time

    caplog.set_level(logging.INFO, logger="sprigcast.core.pacing")
    stop = threading.Event()

    def spin():
        while not stop.is_set():
            sum(range(1000))
    busy = threading.Thread(target=spin, name="camera-busy", daemon=True)
    busy.start()
    try:
        line = pacing.busy_stacks()
        assert "camera-busy at test_threads.py" in line
    finally:
        stop.set()
        busy.join()
    assert "camera-busy" not in pacing.busy_stacks()  # done: nothing to name


def test_the_stall_watch_logs_a_long_wait(caplog):
    import logging
    import time

    caplog.set_level(logging.INFO, logger="sprigcast.core.pacing")
    ticks = iter([0.0, 0.01, 0.02, 0.48])  # the third wake comes 450 ms late
    real = time.perf_counter

    def clock():
        return next(ticks, None) or real() + 100

    pacing.watch_stalls(clock=clock, interval=0.01)
    for _ in range(100):
        if "everything waited" in caplog.text:
            break
        time.sleep(0.01)
    assert "everything waited 450 ms" in caplog.text


def test_picture_conversion_starts_no_threads():
    import time

    import av
    import psutil

    from sprigcast.core.frames import reformat

    process = psutil.Process()
    peak, stop = [0], threading.Event()

    def watch():
        while not stop.is_set():
            peak[0] = max(peak[0], process.num_threads())
            time.sleep(0.0005)
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    time.sleep(0.02)
    before = process.num_threads()
    for _ in range(40):  # fresh frames, as a decoder hands them out
        reformat(av.VideoFrame(1920, 1080, "nv12"), format="rgba", width=1280, height=720).to_ndarray()
    stop.set()
    watcher.join()
    assert peak[0] <= before  # FFmpeg's own default started a pool as big as the CPU for each one


def test_trimming_memory_keeps_the_pages_used_last():
    """At rest the working set is trimmed down to TRIM_KEEP_MB (oldest pages first), never emptied: an emptied
    one made opening a minimized SprigCast take seconds while every page came back."""
    from sprigcast.core import pacing

    calls = []

    class Kernel32:
        def GetCurrentProcess(self):
            return 7

        def SetProcessWorkingSetSizeEx(self, process, low, high, flags):
            calls.append((process, low, high, flags))
            return 1

    assert pacing.trim_working_set(kernel32=Kernel32())
    keep = pacing.TRIM_KEEP_MB * 1024 * 1024
    assert calls == [(7, keep // 4, keep, 0)]  # soft limits: it grows again when it needs to
    assert all(size != (1 << 64) - 1 for size in calls[0][1:3])  # not (SIZE_T)-1, which empties it


def test_the_window_not_answering_is_logged_with_where_it_was(caplog):
    import logging
    import threading

    from sprigcast.core import pacing

    clock = [10.0]
    pacing._ui.update(beat=None, ident=None, where=None, clock=lambda: clock[0])
    try:
        pacing.ui_beat()  # this thread plays the window's
        pacing._check_window(10.1)
        assert pacing._ui["where"] is None  # 100 ms: fine
        seen, stop = threading.Event(), threading.Event()

        def hog():  # another thread busy right then: named with the stall (it may be the one holding the lock)
            while not stop.is_set():
                sum(range(100))

        def check():
            pacing._check_window(11.7)
            seen.set()

        threading.Thread(target=hog, name="lock-hog").start()
        threading.Thread(target=check).start()  # the stall watch, while "the window" is right here
        seen.wait(2)
        stop.set()
        assert "test_threads.py" in pacing._ui["where"]
        assert "meanwhile" in pacing._ui["where"] and "lock-hog at" in pacing._ui["where"]
        clock[0] = 11.8
        with caplog.at_level(logging.INFO, logger="sprigcast.core.pacing"):
            pacing.ui_beat()
        assert "the window didn't answer for 1800 ms; it was at " in caplog.text
        assert "test_the_window_not_answering_is_logged_with_where_it_was" in caplog.text
        assert pacing._ui["where"] is None
    finally:
        pacing._ui.update(beat=None, ident=None, where=None, clock=time.perf_counter)


def test_a_long_render_tick_says_where_it_was_held_up():
    """Held up without Python's lock (inside the graphics driver), nothing else stops and "everything waited"
    never shows; the tick's own late-frame line names where its thread was instead."""
    from sprigcast.core import pacing

    pacing.busy_begin("render")
    began = pacing._busy["render"][1]
    seen = threading.Event()

    def watch():
        pacing._check_busy(began + 0.1)  # not long yet
        assert pacing._busy["render"][2] is None
        pacing._check_busy(began + 0.3)
        seen.set()

    threading.Thread(target=watch).start()
    seen.wait(2)
    where = pacing.busy_end("render")
    assert "test_a_long_render_tick_says_where_it_was_held_up" in where
    assert pacing.busy_end("render") is None


def test_memory_is_logged_every_ten_minutes_with_what_python_keeps_aside(monkeypatch):
    from sprigcast.core import pacing

    monkeypatch.setattr(pacing, "_memory_logged", None)
    first = pacing.watch_memory(now=1000.0)
    assert first.startswith("memory now: ") and "python objects" in first and "kept aside" in first
    assert pacing.watch_memory(now=1000.0 + 60) is None  # not every minute
    assert pacing.watch_memory(now=1000.0 + pacing.MEMORY_EVERY_S + 1) is not None
