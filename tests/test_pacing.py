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
