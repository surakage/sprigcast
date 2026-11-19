"""The capture helper (app/capture_helper.py, docs/native-engine.md "Capture helper"): captures run in a program
of their own, which ends when nothing is captured so Windows gives their graphics memory back."""

import sys
import time
from pathlib import Path

import pytest
from test_native_bridge import FakeEngine, _collection
from test_native_screen_capture import EngineCaptures, wait_for

from sprigcast import sprigcore
from sprigcast.app import capture_helper
from sprigcast.core.source import create_source
from sprigcast.render import native_bridge
from sprigcast.sources import capture

FAKE = [sys.executable, str(Path(__file__).with_name("fake_capture_helper.py"))]


@pytest.fixture
def helper(monkeypatch):
    monkeypatch.setattr(capture_helper, "LINGER_S", 0.05)
    monkeypatch.delenv(capture_helper.ENV_OFF, raising=False)
    made = capture_helper.CaptureHelper(FAKE)
    made.use_card((0x1234, 0))
    monkeypatch.setattr(capture_helper, "HELPER", made)
    yield made
    made.shutdown()


@pytest.fixture
def in_engine(monkeypatch):
    monkeypatch.setattr(sprigcore, "use_native_capture", lambda: True)
    monkeypatch.setattr(capture, "_monitor_handle", lambda index: 0x5000 + index)
    monkeypatch.setattr(capture, "RETRY_SECONDS", 0.05)
    monkeypatch.setattr(capture, "QUICK_RETRY_SECONDS", 0.05)


def test_a_capture_runs_in_the_helper_which_ends_when_nothing_is_captured(helper):
    feed = helper.start(window=7, monitor=0, flags=1, interval_ms=16)
    assert feed is not None and feed.startswith("sprigcast-capture-") and isinstance(feed, str)
    assert wait_for(lambda: feed.state() == (sprigcore.SCREEN_RUNNING, 640, 360, 1)) and feed.told
    proc = helper.proc
    other = helper.start(window=8, monitor=0, flags=0, interval_ms=16)
    assert helper.proc is proc and helper.starts == 1 and helper.captures == 2  # one helper for every capture
    helper.stop(feed)
    time.sleep(0.2)
    assert helper.proc is proc and proc.poll() is None  # one is still captured
    helper.stop(other)
    assert wait_for(lambda: helper.proc is None and proc.poll() is not None)  # ended: Windows frees its captures
    again = helper.start(window=7, monitor=0, flags=1, interval_ms=16)
    assert again is not None and helper.starts == 2 and helper.captures == 1 and again != feed
    assert wait_for(lambda: again.state()[0] == sprigcore.SCREEN_RUNNING)


def test_a_capture_stopped_and_another_started_at_once_keep_the_helper(helper, monkeypatch):
    monkeypatch.setattr(capture_helper, "LINGER_S", 0.5)
    feed = helper.start(window=7, monitor=0, flags=1, interval_ms=16)
    proc = helper.proc
    helper.stop(feed)  # a bloom switch: one capture sleeps, the next bloom's wakes
    other = helper.start(window=8, monitor=0, flags=1, interval_ms=16)
    time.sleep(0.7)
    assert helper.proc is proc and proc.poll() is None and helper.starts == 1
    helper.stop(other)


def test_a_helper_that_dies_ends_its_captures_and_the_next_one_starts_it_again(helper):
    feed = helper.start(window=7, monitor=0, flags=1, interval_ms=16)
    assert wait_for(lambda: feed.state()[0] == sprigcore.SCREEN_RUNNING)
    helper._send(helper.proc, "die")
    assert wait_for(lambda: feed.state() == (sprigcore.SCREEN_ENDED, 640, 360, 1))
    assert wait_for(lambda: helper.proc is None)
    helper.stop(feed)  # the source lets it go: nothing to tell
    again = helper.start(window=7, monitor=0, flags=1, interval_ms=16)
    assert again is not None and helper.starts == 2


def test_the_helper_is_not_used_when_turned_off_missing_or_without_a_card(helper, monkeypatch):
    assert helper.wanted() and helper.usable()
    helper.use_card(None)
    assert helper.wanted() and not helper.usable() and helper.start(7, 0, 0, 16) is None
    helper.use_card((1, 0))
    monkeypatch.setenv(capture_helper.ENV_OFF, "1")
    assert not helper.wanted() and helper.start(7, 0, 0, 16) is None
    monkeypatch.delenv(capture_helper.ENV_OFF)
    missing = capture_helper.CaptureHelper(["/nowhere/SprigCastCapture.exe"])
    missing.use_card((1, 0))
    assert missing.start(7, 0, 0, 16) is None and not missing.usable()  # not tried again for a while
    assert helper.starts == 0


def test_the_bridge_gives_the_engine_a_feeds_block_and_links_it(helper, monkeypatch):
    calls = []

    class Sharing(FakeEngine):
        def attach_shared(self, source_id, name):
            calls.append(("shared", source_id, name))

        def hold_shared(self, source_id, held):
            calls.append(("hold", source_id, held))

    monkeypatch.setattr(sprigcore, "Engine", Sharing)
    bridge = native_bridge.NativeCanvases(_collection())
    feed = helper.start(window=7, monitor=0, flags=1, interval_ms=16)
    assert bridge.attach_media("screen", feed) and bridge.attach_media("screen", feed)
    assert calls == [("shared", "screen", str(feed))] and type(calls[0][2]) is str and feed.linked
    assert bridge.attach_media("screen", feed, held=True)
    assert calls[1:] == [("hold", "screen", True)]
    assert not bridge.attach_media("screen", None)
    assert calls[-1] == ("shared", "screen", None) and not feed.linked
    assert bridge.attach_media("screen", feed)
    bridge.fail("test")
    assert not feed.linked


def test_a_source_is_captured_in_the_helper_and_sleeping_ends_it(helper, in_engine):
    engine = EngineCaptures()
    src = create_source("window_capture", "w", {"window": "x"})
    src._target = lambda: {"window_hwnd": 7}
    src.start()
    try:
        assert wait_for(lambda: src.engine_player is not None)
        feed = src.engine_player
        assert isinstance(feed, capture_helper.Feed) and src._engine_target is None
        assert src.video_frame() is None and not src.engine_shows  # the engine hasn't taken it up yet
        feed.link(engine, "w")
        assert wait_for(lambda: src.status == "capturing" and src.size == (640, 360))
        assert src.engine_shows and engine.reads == 0
        assert src.video_frame().shape == (18, 32, 4) and engine.reads == 1  # a frame Python draws: read back
        proc = helper.proc
        src.sleep()
        assert wait_for(lambda: src.engine_player is None and proc.poll() is not None)  # nothing lingers
        src.wake()
        assert wait_for(lambda: src.engine_player is not None and src.engine_player != feed)
        assert helper.starts == 2
    finally:
        src.stop()


def test_a_window_the_helper_refuses_is_captured_by_the_engine(helper, in_engine):
    src = create_source("window_capture", "w", {"window": "x"})
    src._target = lambda: {"window_hwnd": 404}
    src.start()
    try:
        assert wait_for(lambda: isinstance(src.engine_player, sprigcore.CaptureTarget))
        assert src.engine_player.window == 404 and src._helper_refused
    finally:
        src.stop()


def test_a_window_that_closes_is_looked_for_again(helper, in_engine):
    src = create_source("window_capture", "w", {"window": "x"})
    seen = []
    src._target = lambda: seen.append(1) or {"window_hwnd": 500}
    src.start()
    try:
        assert wait_for(lambda: len(seen) >= 3)
        assert not src._helper_refused and src._engine_target is None  # still the helper's to capture
    finally:
        src.stop()


def test_a_helper_about_to_end_is_kept_by_a_capture_being_started(helper, monkeypatch):
    feed = helper.start(window=7, monitor=0, flags=1, interval_ms=16)
    proc = helper.proc
    waited = capture_helper.CaptureHelper._start_in

    def slow(self, *args):  # the wait for nothing to capture runs out while this capture is being started
        helper.stop(feed)
        time.sleep(0.3)
        return waited(self, *args)

    monkeypatch.setattr(capture_helper.CaptureHelper, "_start_in", slow)
    other = helper.start(window=8, monitor=0, flags=1, interval_ms=16)
    assert other is not None and helper.proc is proc and helper.starts == 1
    helper.stop(other)
    assert wait_for(lambda: helper.proc is None and proc.poll() is not None)  # and it still ends with nothing


def test_a_helper_that_ended_as_a_capture_was_asked_for_is_started_again(helper, monkeypatch):
    waited = capture_helper.CaptureHelper._start_in
    ended = []

    def gone(self, proc, *args):
        if not ended:
            ended.append(proc)
            helper._send(proc, "die")
            proc.wait(5)
            assert wait_for(lambda: helper.proc is None)
        return waited(self, proc, *args)

    monkeypatch.setattr(capture_helper.CaptureHelper, "_start_in", gone)
    feed = helper.start(window=7, monitor=0, flags=1, interval_ms=16)
    assert feed is not None and helper.starts == 2 and feed.proc is helper.proc
