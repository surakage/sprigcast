"""The Stream timer store plugin: time live, countdowns and stopwatches, drawn once a second while on screen."""

import sys
import time
from datetime import datetime

import pytest

from sprigcast import plugin_hooks
from sprigcast.control.native import ApiError, NativeApi
from sprigcast.core.scene import SourceConfig
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")

from test_control_api import _collection  # noqa: E402
from test_store_effects import Host, added, app  # noqa: E402,F401


@pytest.fixture
def timer(added):
    added("stream_timer")
    return sys.modules["sprigcast_plugin_stream_timer"]


@pytest.fixture(autouse=True)
def off_air():
    yield
    plugin_hooks.output_changed("stream", None)
    plugin_hooks.output_changed("record", None)


def _api(settings, name="Stream timer"):
    col = _collection()
    col.sources.append(SourceConfig(id="st-1", kind="stream_timer", name=name, settings=settings))
    host = Host(col)
    return col, host, NativeApi(host)


def test_its_only_there_once_added():
    assert "stream_timer" not in known_kinds()
    assert "timer.start" not in NativeApi(Host(_collection())).all_actions()


def test_clock_text_and_times_of_day(timer):
    assert [timer.clock_text(s) for s in (0, 5, 65, 600, 3599, 3600, 3723)] == [
        "0:00", "0:05", "1:05", "10:00", "59:59", "1:00:00", "1:02:03"]
    evening = datetime(2026, 11, 16, 19, 0)
    assert timer.until_time("8:30 PM", evening) == datetime(2026, 11, 16, 20, 30)
    assert timer.until_time("8pm", evening) == datetime(2026, 11, 16, 20, 0)
    assert timer.until_time("20:30", evening) == datetime(2026, 11, 16, 20, 30)
    assert timer.until_time("12:15 am", evening) == datetime(2026, 11, 17, 0, 15)  # long past today: tomorrow's
    assert timer.until_time("6:30 PM", evening) == datetime(2026, 11, 16, 18, 30)  # just past: it's done
    for bad in ("8", "25:00", "13pm", "soon", ""):
        assert timer.until_time(bad, evening) is None


def test_time_live_follows_going_live_and_recording(timer):
    settings = {"mode": "uptime"}
    assert timer.reading(settings)["text"] == "0:00" and not timer.reading(settings)["counting"]
    plugin_hooks.output_changed("stream", time.monotonic() - 3725)
    read = timer.reading(settings)
    assert read["counting"] and read["text"] == "1:02:05"
    assert timer.reading({"mode": "uptime", "count_from": "record"})["text"] == "0:00"  # not recording
    plugin_hooks.output_changed("record", time.monotonic() - 10)
    assert timer.reading({"mode": "uptime", "count_from": "record"})["text"] == "0:10"
    assert timer.reading({"mode": "uptime", "count_from": "either"})["text"] == "1:02:05"  # the earlier one
    plugin_hooks.output_changed("stream", None)
    assert timer.reading(settings)["text"] == "0:00"


def test_a_stopwatch_starts_pauses_carries_on_and_resets(timer):
    s = {"mode": "stopwatch"}
    s = timer.started(s, 1000.0)
    assert timer.reading(s, now=1065.0)["text"] == "1:05"
    s = timer.paused(s, 1065.0)
    assert timer.reading(s, now=5000.0) == {**timer.reading(s, now=1065.0)}  # paused: it stays
    assert not timer.reading(s, now=5000.0)["counting"]
    s = timer.toggled(s, 6000.0)  # carries on from 1:05
    assert timer.reading(s, now=6010.0)["text"] == "1:15"
    s = timer.added(s, -500, 6010.0)  # never below 0
    assert timer.reading(s, now=6010.0)["text"] == "0:00"
    s = timer.reset(s)
    assert timer.reading(s, now=9999.0)["text"] == "0:00" and not timer.running(s)


def test_a_countdown_counts_down_ends_and_says_so(timer):
    s = {"mode": "countdown", "minutes": 5.0, "done_text": "Starting now"}
    assert timer.reading(s, now=0.0)["text"] == "5:00"  # not started: the whole length
    s = timer.started(s, 100.0)
    assert timer.reading(s, now=100.4)["text"] == "5:00"
    assert timer.reading(s, now=399.5)["text"] == "0:01"  # 0:01 stays up until it's really 0
    done = timer.reading(s, now=400.0)
    assert done["done"] and not done["counting"] and done["text"] == "Starting now"
    s = timer.added(s, 60, 400.0)  # a minute more
    assert timer.reading(s, now=400.0)["text"] == "1:00"
    s = timer.toggled(s, 430.0)  # pause
    assert timer.reading(s, now=900.0)["text"] == "0:30"
    s = timer.started(timer.paused(s, 430.0), 900.0)
    assert timer.reading(s, now=930.0)["done"]
    assert timer.reading(timer.started(s, 960.0), now=960.0)["text"] == "5:00"  # a finished one starts over


def test_a_countdown_to_a_time_of_day(timer):
    s = {"mode": "countdown", "until": "8:30 PM"}
    at = datetime(2026, 11, 16, 20, 0)
    read = timer.reading(s, now=at.timestamp(), now_dt=at)
    assert read["text"] == "30:00" and read["counting"]
    assert not timer.controllable(s)
    bad = timer.reading({"mode": "countdown", "until": "eightish", "minutes": 2.0}, now=0.0)
    assert bad["problem"] and bad["text"] == "2:00"


def test_hide_when_stopped(timer):
    plain = {"mode": "uptime", "hide_when_stopped": True}
    assert timer.hidden(plain, timer.reading(plain))
    plugin_hooks.output_changed("stream", time.monotonic())
    assert not timer.hidden(plain, timer.reading(plain))
    cd = {"mode": "countdown", "minutes": 1.0, "hide_when_stopped": True}
    assert timer.hidden(cd, timer.reading(cd, now=0.0))  # not started
    going = timer.started(cd, 1.0)
    assert not timer.hidden(going, timer.reading(going, now=10.0))
    assert timer.hidden(going, timer.reading(going, now=62.0))  # done, no Done text
    assert not timer.hidden({**going, "done_text": "Go"}, timer.reading({**going, "done_text": "Go"}, now=62.0))
    assert not timer.hidden({"mode": "uptime"}, timer.reading({"mode": "uptime", "count_from": "record"}))


def test_it_draws_only_when_the_time_on_it_changes(timer, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(timer.time, "time", lambda: now[0])
    source = create_source("stream_timer", "st", timer.started({"mode": "stopwatch", "label": "Just chatting"}, 1000.0))
    assert source.size == (640, 220)
    first = source.video_frame()
    assert first.shape == (220, 640, 4) and first[..., 3].any()
    now[0] = 1000.5
    assert source.video_frame() is None  # the same second: nothing drawn
    now[0] = 1001.0
    assert source.video_frame() is not None
    source.update({**source.settings, "hide_when_stopped": True, "run_since": 0.0, "banked": 0.0})
    gone = source.video_frame()
    assert gone is not None and not gone[..., 3].any()  # stopped: see-through
    assert source.video_frame() is None


def test_apps_start_pause_and_reset_it(timer, monkeypatch):
    now = [5000.0]
    monkeypatch.setattr(timer.time, "time", lambda: now[0])
    col, host, api = _api({"mode": "countdown", "minutes": 5.0})
    assert api.run("timer.get", {})["text"] == "5:00"
    out = api.run("timer.start", {})
    assert out["running"] and out["text"] == "5:00" and "timer" in host.commits  # kept with the blooms
    now[0] += 61
    assert api.run("timer.pause", {})["text"] == "3:59"
    now[0] += 500
    assert api.run("timer.get", {})["text"] == "3:59" and not api.run("timer.get", {})["running"]
    assert api.run("timer.add", {"seconds": "60"})["text"] == "4:59"  # web links send text
    assert api.run("timer.toggle", {})["running"]
    assert api.run("timer.reset", {"source": "Stream timer"})["text"] == "5:00"
    assert api.run("timer.start", {"minutes": 10})["text"] == "10:00"
    assert col.source("st-1").settings["minutes"] == 10
    # A restart: the timer made from the saved settings carries on where it was.
    now[0] += 30
    assert timer.reading(create_source("stream_timer", "st-1", col.source("st-1").settings).settings)["text"] == "9:30"
    with pytest.raises(ApiError):
        api.run("timer.add", {"seconds": "lots"})
    with pytest.raises(ApiError):
        api.run("timer.start", {"minutes": 0})
    with pytest.raises(ApiError):
        api.run("timer.start", {"source": "Cam"})  # not a timer


def test_time_live_says_why_it_cant_be_paused(timer):
    _col, _host, api = _api({"mode": "uptime"})
    with pytest.raises(ApiError) as err:
        api.run("timer.pause", {})
    assert "Countdown or Stopwatch" in err.value.message
    assert api.run("timer.get", {})["mode"] == "uptime"
    with pytest.raises(ApiError) as err:
        NativeApi(Host(_collection())).run("timer.start", {})
    assert err.value.code == "not_found"


def test_hotkeys_and_right_click(timer):
    from sprigcast.app import hotkeys  # noqa: F401  (the hotkey list reads plugin entries)

    col, host, api = _api({"mode": "stopwatch"})
    ids = [hid for hid, _ in plugin_hooks.hotkey_actions(col)]
    assert {"plugin:timer:toggle", "plugin:timer:reset", "plugin:timer:add"} <= set(ids)
    assert plugin_hooks.run_hotkey(api, "plugin:timer:toggle")
    assert timer.running(col.source("st-1").settings)
    entries = []
    plugin_hooks.fill_item_menu(api, col.source("st-1"), lambda label, fn: entries.append((label, fn)))
    assert [label for label, _ in entries] == ["Pause", "Reset", "Add a minute"]  # once, though added in each test
    entries[1][1]()
    assert not timer.running(col.source("st-1").settings)
    uptime = SourceConfig(id="up", kind="stream_timer", name="Up", settings={"mode": "uptime"})
    none = []
    plugin_hooks.fill_item_menu(api, uptime, lambda label, fn: none.append(label))
    assert none == []  # time live runs by itself: nothing to start


def test_a_countdown_starts_over_when_it_comes_on_screen(timer, monkeypatch):
    asked = []
    monkeypatch.setattr(timer, "_UI", type("Ui", (), {"restart": type("S", (), {"emit": lambda self, i: asked.append(i)})(),
                                                        "finished": type("S", (), {"emit": lambda self, i: None})()})())
    source = create_source("stream_timer", "st", {"mode": "countdown", "minutes": 1.0})
    source.start()
    assert asked == ["st"]
    assert source.video_frame() is None  # waits a moment for the fresh start rather than flash the old time
    source.update({**source.settings, "run_since": time.time()})
    assert source.video_frame() is not None
    source.sleep()
    source.wake()
    assert asked == ["st", "st"]
    off = create_source("stream_timer", "st2", {"mode": "countdown", "minutes": 1.0, "restart_on_show": False})
    off.wake()
    stopwatch = create_source("stream_timer", "st3", {"mode": "stopwatch"})
    stopwatch.wake()
    assert asked == ["st", "st"]


def test_apps_hear_when_a_countdown_reaches_zero(timer, monkeypatch):
    done = []
    monkeypatch.setattr(timer, "_UI", type("Ui", (), {"restart": type("S", (), {"emit": lambda self, i: None})(),
                                                        "finished": type("S", (), {"emit": lambda self, i: done.append(i)})()})())
    now = [100.0]
    monkeypatch.setattr(timer.time, "time", lambda: now[0])
    source = create_source("stream_timer", "st", {"mode": "countdown", "minutes": 1.0, "run_since": 100.0,
                                                 "restart_on_show": False})
    source.video_frame()
    now[0] = 161.0
    source.video_frame()
    source.video_frame()
    assert done == ["st"]  # once


def test_adding_it_puts_a_small_card_in_the_top_left_of_both_shapes():
    col = _collection()
    col.sources.append(SourceConfig(id="t", kind="stream_timer", name="Stream timer"))
    part = col.own_sprig(col.scene("chat-h"), "t", (640, 220), "Stream timer", "top_left")
    for shape in col.shapes(part):
        canvas = col.canvas(shape.canvas)
        item = shape.items[0]
        assert 640 * item.scale < canvas.width / 2 and item.y < canvas.height / 10
        assert item.x == pytest.approx(min(canvas.width, canvas.height) * 0.035)


def test_the_ui_thread_starts_it_over_and_tells_apps_it_ended(timer, app, monkeypatch):  # noqa: F811
    from PySide6.QtWidgets import QWidget

    col, host, api = _api({"mode": "countdown", "minutes": 2.0})
    events = []
    api.plugin_event = lambda name, data: events.append((name, data))

    class Window(QWidget):
        control = True

        @property
        def api(self):
            return api

    window = Window()
    try:
        assert timer._window_api() is not None  # the main window's API (another test's window may be first)
        monkeypatch.setattr(timer, "_window_api", lambda: api)
        ui = timer._ui()
        ui.on_restart("st-1")
        assert timer.running(col.source("st-1").settings) and "timer" in host.commits
        assert events[-1][0] == "timer.changed" and events[-1][1]["text"] == "2:00"
        ui.on_finished("st-1")
        assert events[-1] == ("timer.done", {"source": "Stream timer"})
    finally:
        window.deleteLater()
