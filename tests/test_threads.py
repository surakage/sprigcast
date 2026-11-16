import threading

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
