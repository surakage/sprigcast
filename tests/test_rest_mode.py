"""Rest mode: SprigCast's CPU and GPU boosts are only for while something goes out (live, recording, clips, SprigCast
Camera watched). Idle, it sits at normal priority, so a game played without streaming gets the PC; Windows' power
saving comes back only while the window is out of sight."""

from types import SimpleNamespace

import pytest

from sprigcast.app import priority


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(priority, "set_cpu_priority", lambda level: seen.append(("cpu", level)) or True)
    monkeypatch.setattr(priority, "set_gpu_normal", lambda: seen.append(("gpu", "normal")) or True)
    monkeypatch.setattr(priority, "raise_gpu_priority", lambda: seen.append(("gpu", "high")) or "high")
    monkeypatch.setattr(priority, "stop_power_throttling", lambda off=True: seen.append(("throttling off", off)) or True)
    monkeypatch.setattr(priority, "chosen", lambda: "above_normal")
    monkeypatch.setitem(priority._state, "resting", False)
    monkeypatch.setitem(priority._state, "saving", False)
    return seen


def test_rest_steps_down_and_waking_puts_the_boosts_back(calls):
    assert priority.rest(save_power=False)
    assert calls == [("cpu", "normal"), ("gpu", "normal")] and priority.resting()
    calls.clear()
    assert not priority.rest(save_power=False)  # already resting: nothing again every check
    assert priority.rest(save_power=True) and calls == [("throttling off", False)]  # minimized: Windows saves power
    calls.clear()
    assert priority.rest(save_power=False) and calls == [("throttling off", True)]  # window back: smooth again
    calls.clear()
    assert priority.wake() and not priority.resting()
    assert calls == [("cpu", "above_normal"), ("gpu", "high"), ("throttling off", True)]
    calls.clear()
    assert not priority.wake() and not calls


def test_the_window_rests_after_a_quiet_while_and_wakes_when_anything_goes_out(calls, monkeypatch):
    from sprigcast.app.main_window import MainWindow

    class Camera:
        watched = False

    class Window:
        REST_AFTER_QUIET_S = MainWindow.REST_AFTER_QUIET_S
        _going_out = MainWindow._going_out
        _wake_up = MainWindow._wake_up
        _check_rest = MainWindow._check_rest
        _rest_when_settled = MainWindow._rest_when_settled
        SETTLED_S = MainWindow.SETTLED_S
        recording = streaming = replay = None
        collection = SimpleNamespace(active_scenes={"h": "gaming"})
        rests = 0

        def _rest_if_idle(self):
            self.rests += 1
        camera = Camera()
        _quiet_since = None
        minimized = False

        def _busy_with_outputs(self):
            return MainWindow._busy_with_outputs(self)

        def isMinimized(self):
            return self.minimized

        def isVisible(self):
            return True

    clock = {"now": 100.0}
    monkeypatch.setattr("sprigcast.app.main_window.time.monotonic", lambda: clock["now"])
    w = Window()
    w._check_rest()
    assert not priority.resting()  # quiet only just now
    clock["now"] += MainWindow.REST_AFTER_QUIET_S
    w._check_rest()
    assert priority.resting() and not priority._state["saving"]
    w.minimized = True
    w._check_rest()
    assert priority._state["saving"]
    w.camera.watched = True  # Discord opened SprigCast Camera
    w._check_rest()
    assert not priority.resting() and w._quiet_since is None
    w.camera.watched = False
    w.streaming = object()  # live from the Stream Deck
    clock["now"] += 100
    w._check_rest()
    assert not priority.resting()


def test_memory_is_given_back_a_minute_after_start_and_after_each_bloom_switch(calls, monkeypatch):
    from sprigcast.app.main_window import MainWindow

    class Window:
        REST_AFTER_QUIET_S, SETTLED_S = MainWindow.REST_AFTER_QUIET_S, MainWindow.SETTLED_S
        _going_out, _wake_up = MainWindow._going_out, MainWindow._wake_up
        _check_rest, _rest_when_settled = MainWindow._check_rest, MainWindow._rest_when_settled
        recording = streaming = replay = camera = None
        collection = SimpleNamespace(active_scenes={"h": "gaming"})
        _quiet_since = None
        rests = 0

        def _rest_if_idle(self):
            self.rests += 1

        def _busy_with_outputs(self):
            return MainWindow._busy_with_outputs(self)

        def isMinimized(self):
            return False

        def isVisible(self):
            return True

    clock = {"now": 100.0}
    monkeypatch.setattr("sprigcast.app.main_window.time.monotonic", lambda: clock["now"])
    w = Window()
    for _ in range(40):  # 80 s after start, a check every 2 s
        w._check_rest()
        clock["now"] += 2
    assert w.rests == 1  # once, a minute in
    w.collection.active_scenes = {"h": "brb"}
    for _ in range(25):
        w._check_rest()
        clock["now"] += 2
    assert w.rests == 1  # 50 s after the switch: not yet
    for _ in range(10):
        w._check_rest()
        clock["now"] += 2
    assert w.rests == 2  # a minute after it: once more
    w.streaming = object()  # live: never while something goes out
    w.collection.active_scenes = {"h": "gaming"}
    for _ in range(60):
        w._check_rest()
        clock["now"] += 2
    assert w.rests == 2
