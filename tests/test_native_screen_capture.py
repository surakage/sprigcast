"""Screen capture inside the native engine (docs/native-engine.md "Capture"): the supervisor in sources/capture.py
hands the engine a CaptureTarget, the bridge attaches it, and when the engine can't, the package captures again."""

import sys
import threading
import time
import types

import numpy as np
import pytest
from test_native_bridge import FakeEngine, _collection

from sprigcast import sprigcore
from sprigcast.core.source import create_source
from sprigcast.render import native_bridge
from sprigcast.sources import capture


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class EngineCaptures:
    """Stands in for the engine's side of a capture: a state per source the test sets, and a picture to read."""

    def __init__(self):
        self.states: dict[str, tuple[int, int, int, int]] = {}
        self.reads = 0

    def capture_state(self, source_id):
        return self.states.get(source_id, (sprigcore.SCREEN_STARTING, 0, 0, 0))

    def capture_read(self, source_id):
        self.reads += 1
        frame = np.zeros((18, 32, 4), dtype=np.uint8)
        frame[..., 1] = 200
        return frame


class FakeControl:
    def __init__(self):
        self.stopped = threading.Event()

    def stop(self):
        self.stopped.set()

    def is_finished(self):
        return self.stopped.is_set()


@pytest.fixture
def package(monkeypatch):
    """A fake windows-capture: remembers the captures it started."""
    started = []

    class FakeWindowsCapture:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            started.append(self)

        def event(self, handler):
            return handler

        def start_free_threaded(self):
            return FakeControl()

    module = types.ModuleType("windows_capture")
    module.WindowsCapture = FakeWindowsCapture
    monkeypatch.setitem(sys.modules, "windows_capture", module)
    return started


@pytest.fixture
def in_engine(monkeypatch):
    monkeypatch.setattr(sprigcore, "use_native_capture", lambda: True)
    monkeypatch.setattr(capture, "_monitor_handle", lambda index: 0x5000 + index)
    monkeypatch.setattr(capture, "RETRY_SECONDS", 0.05)


def test_targets_are_equal_when_they_ask_for_the_same_capture():
    a = sprigcore.CaptureTarget(monitor=7, flags=sprigcore.SCREEN_CURSOR, interval_ms=16, serial=1)
    assert a == sprigcore.CaptureTarget(monitor=7, flags=1, interval_ms=16, serial=1)
    assert a != sprigcore.CaptureTarget(monitor=7, flags=1, interval_ms=16, serial=2)  # found again: starts over
    assert len({a, sprigcore.CaptureTarget(monitor=7, flags=1, interval_ms=16, serial=1)}) == 1
    assert a.state() == (sprigcore.SCREEN_NONE, 0, 0, 0) and a.read() is None and not a.linked
    assert sprigcore.CaptureTarget(window=1, interval_ms=99999).interval_ms == 10000
    assert getattr(a, "holds", False)  # render/engine.py keeps it attached, held, through a freeze


def test_the_bridge_attaches_a_capture_once_holds_it_and_lets_it_go(monkeypatch):
    calls = []

    class Capturing(FakeEngine):
        def attach_capture(self, source_id, target):
            calls.append(("capture", source_id, target))

        def hold_capture(self, source_id, held):
            calls.append(("hold", source_id, held))

        def attach_media(self, source_id, player):
            calls.append(("media", source_id, player))

        def forget(self, source_id):
            calls.append(("forget", source_id))

    monkeypatch.setattr(sprigcore, "Engine", Capturing)
    bridge = native_bridge.NativeCanvases(_collection())
    first = sprigcore.CaptureTarget(monitor=7, serial=1)
    assert bridge.attach_media("screen", first) and bridge.attach_media("screen", first)
    assert calls == [("capture", "screen", first)] and first.linked
    assert bridge.attach_media("screen", first, held=True) and bridge.attach_media("screen", first, held=True)
    assert bridge.attach_media("screen", first)
    assert calls[1:] == [("hold", "screen", True), ("hold", "screen", False)]
    calls.clear()
    again = sprigcore.CaptureTarget(monitor=7, serial=2)  # the display came back: the engine starts it over
    assert bridge.attach_media("screen", again)
    assert calls == [("capture", "screen", again)] and again.linked and not first.linked
    player = object()
    assert bridge.attach_media("screen", player)  # a capture let go before a player takes over
    assert calls[1:] == [("capture", "screen", None), ("media", "screen", player)]
    calls.clear()
    assert bridge.attach_media("screen", again)
    assert not bridge.attach_media("screen", None)
    assert calls == [("media", "screen", None), ("capture", "screen", again), ("capture", "screen", None)]
    assert not again.linked
    assert bridge.attach_media("screen", again)
    bridge.forget_gone([])
    assert not again.linked
    assert bridge.attach_media("screen", again)
    bridge.fail("test")
    assert not again.linked  # the engine stopped: the source reads it no more


def test_the_engine_captures_a_display_and_python_reads_it_back_only_when_it_draws(in_engine, package):
    engine = EngineCaptures()
    src = create_source("display_capture", "d", {"monitor": 2, "fps_limit": 30})
    src.start()
    try:
        assert wait_for(lambda: src.engine_player is not None)
        target = src.engine_player
        assert target.monitor == 0x5002 and target.window == 0
        assert target.flags == sprigcore.SCREEN_CURSOR and target.interval_ms == 33
        assert src.video_frame() is None  # not linked yet: the engine hasn't taken it up
        assert not src.engine_shows
        target.link(engine, "d")
        engine.states["d"] = (sprigcore.SCREEN_RUNNING, 1920, 1080, 0)
        assert not src.engine_shows  # running, but no picture shown yet: a switch still waits
        engine.states["d"] = (sprigcore.SCREEN_RUNNING, 1920, 1080, 5)
        assert src.engine_shows and engine.reads == 0  # a waiting switch learns it without a read-back
        assert wait_for(lambda: src.status == "capturing" and src.size == (1920, 1080))
        src.engine_tick()
        assert engine.reads == 0  # the engine drew it: nothing read back
        frame = src.video_frame()
        assert frame.shape == (18, 32, 4) and engine.reads == 1
        assert package == []  # the package never captured
    finally:
        src.stop()
    assert src.engine_player is None


def test_a_capture_that_ends_is_started_over_with_a_new_serial(in_engine, package, monkeypatch):
    from sprigcast.sources import win32_enum

    monkeypatch.setattr(win32_enum, "find_window",
                        lambda title, process="": win32_enum.WindowInfo(hwnd=0x77, title="Game", process="game.exe"))
    engine = EngineCaptures()
    src = create_source("window_capture", "w", {"title": "Game", "process": "game.exe", "cursor": False})
    src.start()
    try:
        assert wait_for(lambda: src.engine_player is not None)
        first = src.engine_player
        assert first.window == 0x77 and first.flags == 0
        first.link(engine, "w")
        engine.states["w"] = (sprigcore.SCREEN_ENDED, 0, 0, 0)
        assert wait_for(lambda: src.engine_player is not None and src.engine_player.serial > first.serial)
        assert package == []
    finally:
        src.stop()


def test_a_game_capture_asks_the_engine_for_a_solid_picture(in_engine):
    src = create_source("game_capture", "g", {"mode": "window", "title": "Game", "process": "game.exe",
                                              "cursor": False})
    assert src.channel_order == "BGRX"
    assert src._engine_flags() == sprigcore.SCREEN_OPAQUE
    src.settings["cursor"] = True
    assert src._engine_flags() == sprigcore.SCREEN_CURSOR | sprigcore.SCREEN_OPAQUE


def test_when_the_engine_refuses_the_package_captures(in_engine, package):
    engine = EngineCaptures()
    src = create_source("display_capture", "d", {"monitor": 1})
    src.start()
    try:
        assert wait_for(lambda: src.engine_player is not None)
        src.engine_player.link(engine, "d")
        engine.states["d"] = (sprigcore.SCREEN_REFUSED, 0, 0, 0)
        assert wait_for(lambda: len(package) == 1)
        assert package[0].kwargs["monitor_index"] == 1
        assert src.engine_player is None
    finally:
        src.stop()


def test_a_refused_source_tries_the_engine_again_later_and_others_never_stop(in_engine, package, monkeypatch):
    """One slow start (refused) moved that source to the package, and it stays there only a while: it tries the
    engine again after ENGINE_AGAIN_SECONDS (PC 2026-11-19: every source stayed on the package until a restart)."""
    monkeypatch.setattr(capture, "ENGINE_AGAIN_SECONDS", 0.2)
    engine = EngineCaptures()
    src = create_source("display_capture", "d", {"monitor": 1})
    other = create_source("display_capture", "d2", {"monitor": 2})
    src.start()
    try:
        assert wait_for(lambda: src.engine_player is not None)
        refused = src.engine_player
        refused.link(engine, "d")
        engine.states["d"] = (sprigcore.SCREEN_REFUSED, 0, 0, 0)
        assert wait_for(lambda: len(package) == 1)
        other.start()
        assert wait_for(lambda: other.engine_player is not None)  # another source still goes to the engine
        time.sleep(0.25)
        engine.states.pop("d")
        src._restart.set()  # the package's capture ends (its target changed): the engine is asked again
        assert wait_for(lambda: src.engine_player is not None and src.engine_player.serial > refused.serial)
    finally:
        src.stop()
        other.stop()


def test_a_capture_no_engine_takes_up_falls_back_to_the_package(in_engine, package, monkeypatch):
    monkeypatch.setattr(capture, "LINK_SECONDS", 0.1)
    src = create_source("display_capture", "d", {"monitor": 1})
    src.start()
    try:
        assert wait_for(lambda: len(package) == 1)
        assert src.engine_player is None
    finally:
        src.stop()


def test_without_the_engine_the_package_captures_at_once(package, monkeypatch):
    monkeypatch.setattr(sprigcore, "use_native_capture", lambda: False)
    src = create_source("display_capture", "d", {"monitor": 1})
    src.start()
    try:
        assert wait_for(lambda: len(package) == 1, timeout=1.0)
        assert src.engine_player is None
    finally:
        src.stop()


@pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")
def test_the_engine_captures_the_primary_display_or_says_it_cant():
    """The real engine (CI's Windows, on WARP): a display captured and read back through the bindings. A runner with
    no desktop to capture says so (ended or refused), and nothing breaks."""
    engine = sprigcore.Engine(warp=True)
    try:
        target = sprigcore.CaptureTarget(monitor=capture._monitor_handle(1), flags=sprigcore.SCREEN_CURSOR,
                                         interval_ms=16, serial=1)
        assert target.monitor
        engine.attach_capture("screen", target)
        target.link(engine, "screen")
        assert wait_for(lambda: target.state()[0] != sprigcore.SCREEN_STARTING, timeout=5.0)
        state, width, height, frames = target.state()
        assert state in (sprigcore.SCREEN_RUNNING, sprigcore.SCREEN_ENDED, sprigcore.SCREEN_REFUSED)
        if state == sprigcore.SCREEN_RUNNING:
            frame = target.read()
            assert frame is not None and frame.shape == (height, width, 4) and frames > 0
            engine.hold_capture("screen", True)
            engine.hold_capture("screen", False)
        engine.attach_capture("screen", None)
        assert engine.capture_state("screen")[0] == sprigcore.SCREEN_NONE
    finally:
        engine.close()
