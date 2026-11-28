import threading
import time

import pytest

from sprigcast.core import pacing, workers


@pytest.fixture
def check_window(monkeypatch):
    """The stall watch's look at the window, for this test alone to call. A stall watch an earlier test started
    never ends and looks at the window too, with the real time: in a full run it sometimes noted where the window
    was before the test's own look did (or after the test thought the window had answered)."""
    look = pacing._check_window
    monkeypatch.setattr(pacing, "_check_window", lambda now: None)
    return look


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

    assert pacing.trim_working_set(kernel32=Kernel32(), ram_gb=16)
    keep = pacing.TRIM_KEEP_MB * 1024 * 1024
    assert calls == [(7, keep // 4, keep, 0)]  # soft limits: it grows again when it needs to
    assert all(size != (1 << 64) - 1 for size in calls[0][1:3])  # not (SIZE_T)-1, which empties it


def test_a_small_memory_pc_keeps_its_pages():
    """A 4 GB PC with a hard drive froze 6-26 s whenever trimmed pages came back from the disk (Mikedog151)."""
    from sprigcast.core import pacing

    class Kernel32:
        def GetCurrentProcess(self):
            raise AssertionError("never asked")

    assert not pacing.trim_working_set(kernel32=Kernel32(), ram_gb=3.7)


def test_the_window_not_answering_is_logged_with_where_it_was(caplog, check_window):
    import logging
    import threading

    from sprigcast.core import pacing

    clock = [10.0]
    pacing._ui.update(beat=None, ident=None, where=None, clock=lambda: clock[0])
    try:
        pacing.ui_beat()  # this thread plays the window's
        check_window(10.1)
        assert pacing._ui["where"] is None  # 100 ms: fine
        seen, stop = threading.Event(), threading.Event()

        def hog():  # another thread busy right then: named with the stall (it may be the one holding the lock)
            while not stop.is_set():
                sum(range(100))

        def check():
            check_window(11.7)
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
    # An hour ahead: the app's own stall watch (started by an earlier test, on the real clock) never calls it long
    # while this test's thread is slow to start under load; only the checks below, on this clock, do.
    pacing._busy["render"][1] += 3600.0
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
    # Sooner when it grew a lot since the last line, and once after a bloom switch (the PC's Gameplay bloom grew
    # 1.1 GB in 9 minutes, 2026-11-21, with nothing in the log to say where).
    at = 3000.0
    monkeypatch.setattr(pacing, "_committed_mb", lambda: 500.0)
    monkeypatch.setattr(pacing, "_memory_logged", None)
    assert pacing.watch_memory(now=at) is not None and pacing.watch_memory(now=at + 40) is None
    monkeypatch.setattr(pacing, "_committed_mb", lambda: 500.0 + pacing.MEMORY_GREW_MB)
    assert "(grew 200 MB in 80 s)" in pacing.watch_memory(now=at + 80)
    assert pacing.watch_memory(now=at + 120) is None  # measured from that line on
    monkeypatch.setattr(pacing, "_memory_switch_said", float("-inf"))
    pacing._memory_after_switch(at + 130)
    assert pacing.watch_memory(now=at + 140) is None
    assert "(after a bloom switch)" in pacing.watch_memory(now=at + 130 + pacing.MEMORY_AFTER_SWITCH_S)
    pacing._memory_after_switch(at + 170)  # switching all the time: not a line for each
    assert pacing.watch_memory(now=at + 200) is None


def test_the_stall_watch_never_stops(monkeypatch, caplog):
    """2026-11-17: a check met None mid-update, raised, and the watch was gone for the rest of the session."""
    import logging
    import threading

    from sprigcast.core import pacing

    def watching():
        return [t for t in threading.enumerate() if t.name == "sprigcast-stall-watch" and t.is_alive()]

    calls = []

    def broken(now):
        calls.append(now)
        raise TypeError("unsupported operand type(s) for +=: 'NoneType' and 'str'")

    monkeypatch.setattr(pacing, "_check_window", broken)
    pacing.watch_stalls()
    with caplog.at_level(logging.INFO, logger="sprigcast.core.pacing"):
        deadline = time.monotonic() + 2
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
    assert len(calls) >= 3 and watching()  # it failed three times and still runs
    assert "still watching" in caplog.text


def test_keep_watching_starts_it_again(monkeypatch):
    import threading

    from sprigcast.core import pacing

    started = []
    monkeypatch.setattr(pacing, "_watch_args", {"interval": 0.05})
    monkeypatch.setattr(threading, "enumerate", lambda: [])  # it isn't running
    monkeypatch.setattr(pacing, "watch_stalls", lambda **kw: started.append(kw))
    assert pacing.keep_watching() and started == [{"interval": 0.05}]


def test_window_stall_is_noted_whole_even_if_the_window_answers_meanwhile(monkeypatch, check_window):
    from sprigcast.core import pacing

    pacing._ui.update(beat=10.0, ident=threading_ident(), where=None)

    def answered(skip=None):
        pacing._ui["where"] = None  # the window's thread answered right then
        return "render at engine.py:1 tick"

    monkeypatch.setattr(pacing, "busy_stacks", answered)
    try:
        check_window(11.0)
        assert "meanwhile render at engine.py:1 tick" in pacing._ui["where"]
    finally:
        pacing._ui.update(beat=None, ident=None, where=None)


def threading_ident():
    import threading

    return threading.get_ident()


def test_housekeeping_waits_for_the_end_of_a_frame_and_says_when_it_was_slow(monkeypatch, caplog):
    """PC 2026-11-17: the minute's tidy, thread count and memory line, run at once on the window's thread, made a
    late frame on the same second of the minute. Each now starts as a frame ends and is logged when slow."""
    import logging
    import threading

    from sprigcast.core import pacing

    pacing.at_frame_end()  # nothing rendering yet in this process, or long ago: returns at once
    monkeypatch.setattr(pacing, "_housekeeping_logged", float("-inf"))
    order: list[str] = []
    stop = threading.Event()

    def render():
        while not stop.is_set():
            time.sleep(0.005)
            order.append("frame")
            pacing.frame_ended()

    thread = threading.Thread(target=render, daemon=True)
    thread.start()
    try:
        time.sleep(0.03)
        order.clear()
        with caplog.at_level(logging.INFO, logger="sprigcast.core.pacing"):
            pacing.housekeeping("tidy", lambda: (order.append("work"), time.sleep(0.006)))
    finally:
        stop.set()
        thread.join(1)
    assert order[0] == "frame" and "work" in order  # it waited for a frame to finish first
    assert "housekeeping: tidy took" in caplog.text


def test_a_slow_frame_step_names_where_the_window_was(monkeypatch):
    from sprigcast.render.engine import SLOW_STEP_S, Engine

    monkeypatch.setitem(pacing._ui, "ident", threading.get_ident())
    engine = Engine.__new__(Engine)
    engine._marks = [("start", time.perf_counter() - 2 * SLOW_STEP_S)]
    engine._window_at = ""
    engine._mark("sprigs")
    assert "test_a_slow_frame_step_names_where_the_window_was" in engine._window_at
    assert "window at " in engine._where()


def test_fine_window_watch_logs_short_stalls_with_where_they_were(caplog, check_window):
    """SPRIGCAST_UI_STALL_MS=50 (the PC's pop-up sweep): every window stall from 50 ms is logged with the window
    thread's stack, taken half way; shorter ones are dropped; without it the usual 250 ms watch is unchanged."""
    import logging

    from sprigcast.core import pacing

    assert pacing.ui_fine_ms({}) is None and pacing.ui_fine_ms({"SPRIGCAST_UI_STALL_MS": "off"}) is None
    assert pacing.ui_fine_ms({"SPRIGCAST_UI_STALL_MS": "5"}) == pacing.UI_FINE_MIN_MS
    assert pacing.watch_window({}) == (pacing.UI_BEAT_MS, 0.05) and pacing._ui["fine"] is None
    clock = [10.0]
    pacing._ui.update(beat=None, ident=None, where=None, clock=lambda: clock[0])
    try:
        assert pacing.watch_window({"SPRIGCAST_UI_STALL_MS": "50"}) == (pacing.UI_FINE_BEAT_MS, pacing.UI_FINE_WATCH_S)
        pacing.ui_beat()
        seen = threading.Event()

        def check(at):
            check_window(at)
            seen.set()

        threading.Thread(target=check, args=(10.02,)).start()
        seen.wait(2)
        assert pacing._ui["where"] is None  # 20 ms: not yet half way
        seen.clear()
        threading.Thread(target=check, args=(10.03,)).start()
        seen.wait(2)
        assert "test_threads.py" in pacing._ui["where"]
        clock[0] = 10.04
        with caplog.at_level(logging.INFO, logger="sprigcast.core.pacing"):
            pacing.ui_beat()  # 40 ms in all: dropped
            assert "didn't answer" not in caplog.text and pacing._ui["where"] is None
            clock[0] = 10.11
            pacing.ui_beat()  # 70 ms, too short to be caught: still logged
        assert "the window didn't answer for 70 ms; it was at (too short to catch where)" in caplog.text
    finally:
        pacing._ui.update(beat=None, ident=None, where=None, clock=time.perf_counter, fine=None)


def test_a_helper_programs_memory_goes_in_the_log():
    """PC 2026-11-21: the page helper held 1,486 MB and nothing in the log said so. Its line adds up the helper and
    every process it started."""
    from types import SimpleNamespace as NS

    def proc(private_mb, rss_mb, children=()):
        return NS(memory_info=lambda: NS(private=private_mb * 1e6, rss=rss_mb * 1e6),
                  children=lambda recursive=False: list(children))

    def gone():
        raise ProcessLookupError

    tree = proc(300, 200, [proc(900, 700), proc(250, 150), NS(memory_info=gone)])
    line = pacing.helper_memory_line("page helper", 4242, process=lambda pid: tree)
    assert line == "page helper memory: committed 1,450 MB in 3 processes (biggest 900, 300, 250 MB), in use 1,050 MB"
    assert pacing.helper_memory_line("page helper", 4242, process=lambda pid: gone()) is None
    # Its own change, not SprigCast's: "(grew 834 MB in 60 s)" after this line read as the helper's (PC 2026-11-21).
    total: list = []
    pacing.helper_memory_line("page helper", 4242, process=lambda pid: tree, total=total)
    assert total == [1450.0]
    pacing._helper_was.pop("page helper", None)
    assert pacing.helper_change("page helper", 1929.0, "grew 786 MB in 120 s") == ""
    assert pacing.helper_change("page helper", 2257.0, "grew 834 MB in 60 s") == " (+328 MB since its last line)"
    assert pacing.helper_change("page helper", 1993.0, "after a bloom switch") == \
        " (-264 MB since its last line, after a bloom switch)"
    assert pacing.helper_change("page helper", 1993.2) == ""
    pacing._helper_was.pop("page helper", None)


def test_a_long_window_stall_is_said_while_it_lasts(check_window, caplog):
    """A window that stays stuck is looked at again at 10 s, 20 s, 40 s..., and the log says where it is now
    (the one look at 250 ms can't tell a slow line from the place it is really stuck, 2026-11-28)."""
    import logging
    import threading

    from sprigcast.core import pacing

    clock = [10.0]
    pacing._ui.update(beat=None, ident=None, where=None, clock=lambda: clock[0])

    def look(now):  # the stall watch, on its own thread, while "the window" is right here
        t = threading.Thread(target=check_window, args=(now,))
        t.start()
        t.join(5)

    try:
        pacing.ui_beat()
        with caplog.at_level(logging.WARNING, logger="sprigcast.core.pacing"):
            look(10.5)
            assert pacing._ui["where"] is not None
            look(15.0)
            assert "has not answered" not in caplog.text  # 5 s: not yet
            look(20.5)
            assert "the window has not answered for 10 s; it is at " in caplog.text
            assert "test_threads.py" in caplog.text and "; meanwhile " in caplog.text
            caplog.clear()
            look(25.0)
            assert "has not answered" not in caplog.text  # next at 20 s
            look(30.5)
            assert "the window has not answered for 20 s" in caplog.text
        clock[0] = 31.0
        pacing.ui_beat()  # it answers: the next stall starts over at 10 s
        look(31.5)
        assert pacing._ui["long"] == pacing.UI_LONG_STALL_S
    finally:
        pacing._ui.update(beat=None, ident=None, where=None, clock=time.perf_counter)
