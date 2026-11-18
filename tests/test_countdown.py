"""The Countdown store plugin and SprigCast's clock card: boxes that hug the card, and stay put on the canvas."""

import sys
import time

import pytest

from sprigcast import plugin_hooks
from sprigcast.control.native import ApiError, NativeApi
from sprigcast.core.scene import CARD_W, SceneItem, SourceConfig
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")

from test_control_api import _collection  # noqa: E402
from test_store_effects import Host, added, app  # noqa: E402,F401


@pytest.fixture
def countdown(added):
    added("countdown")
    return sys.modules["sprigcast_plugin_countdown"]


def _api(settings, name="Countdown"):
    col = _collection()
    col.sources.append(SourceConfig(id="cd-1", kind="countdown", name=name, settings=settings))
    host = Host(col)
    return col, host, NativeApi(host)


def test_its_only_there_once_added():
    assert "countdown" not in known_kinds()
    assert "countdown.start" not in NativeApi(Host(_collection())).all_actions()


def test_it_counts_down_ends_and_its_vine_shrinks(countdown):
    s = {"minutes": 5.0, "done_text": "Starting now"}
    read = countdown.reading(s, now=0.0)
    assert read["text"] == "5:00" and read["vine"] == 1.0 and not read["counting"]
    s = countdown.started(s, 100.0)
    half = countdown.reading(s, now=250.0)
    assert half["text"] == "2:30" and half["vine"] == pytest.approx(0.5) and half["counting"]
    assert countdown.reading(s, now=399.5)["text"] == "0:01"
    done = countdown.reading(s, now=400.0)
    assert done["done"] and done["text"] == "Starting now"
    s = countdown.added(s, 60, 400.0)
    assert countdown.reading(s, now=400.0)["text"] == "1:00"
    assert countdown.started(countdown.paused(s, 430.0), 900.0)["run_since"] == 900.0


def test_a_countdown_to_a_time_of_day_runs_by_itself(countdown):
    from datetime import datetime

    at = datetime(2026, 11, 16, 20, 0)
    read = countdown.reading({"until": "8:30 PM"}, now=at.timestamp(), now_dt=at)
    assert read["text"] == "30:00" and read["counting"] and read["vine"] is None
    assert countdown.to_a_time({"until": "8:30 PM"})
    assert countdown.reading({"until": "soon", "minutes": 2.0}, now=0.0)["problem"]


def test_its_box_is_the_card_and_stays_put_while_it_counts(countdown, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(countdown.time, "time", lambda: now[0])
    cls = countdown.CountdownSource
    source = create_source("countdown", "cd", countdown.started({**cls.defaults, "minutes": 10.0}, 1000.0))
    first = source.video_frame()
    w, h = source.size
    assert h == 220 and first.shape == (220, w, 4)
    assert first[:, :8, 3].any() and first[:, -8:, 3].any()  # the card reaches both sides of its box
    assert first[:2, w // 2, 3].max() < 40  # and nothing above it but its soft edge
    now[0] = 1000.4
    assert source.video_frame() is None  # the same second: nothing drawn
    for t in (1001.0, 1500.0, 1599.0):  # 9:59 has a digit less than 10:00: the box keeps the room
        now[0] = t
        assert source.video_frame() is not None
        assert source.size == (w, h)
    five = create_source("countdown", "cd5", {**cls.defaults, "minutes": 5.0})
    five.video_frame()
    assert five.size[0] < w  # a shorter countdown, a narrower card


def test_apps_hotkeys_and_right_click(countdown, monkeypatch):
    from sprigcast.app import hotkeys  # noqa: F401  (the hotkey list reads plugin entries)

    now = [5000.0]
    monkeypatch.setattr(countdown.time, "time", lambda: now[0])
    col, host, api = _api({"minutes": 5.0})
    events = []
    api.plugin_event = lambda name, data: events.append(name)
    assert api.run("countdown.get", {})["text"] == "5:00"
    assert api.run("countdown.start", {})["running"] and "countdown" in host.commits
    now[0] += 61
    assert api.run("countdown.pause", {})["text"] == "3:59"
    assert api.run("countdown.add", {"seconds": "60"})["text"] == "4:59"
    assert api.run("countdown.reset", {"source": "Countdown"})["text"] == "5:00"
    assert api.run("countdown.start", {"minutes": 10})["text"] == "10:00"
    assert events and set(events) == {"countdown.changed"}
    with pytest.raises(ApiError):
        api.run("countdown.start", {"minutes": 0})
    with pytest.raises(ApiError):
        api.run("countdown.start", {"source": "Cam"})  # not a countdown
    with pytest.raises(ApiError) as err:
        NativeApi(Host(_collection())).run("countdown.start", {})
    assert err.value.code == "not_found"

    ids = [hid for hid, _ in plugin_hooks.hotkey_actions(col)]
    assert {"plugin:countdown:toggle", "plugin:countdown:reset", "plugin:countdown:add"} <= set(ids)
    assert plugin_hooks.run_hotkey(api, "plugin:countdown:toggle")
    entries = []
    plugin_hooks.fill_item_menu(api, col.source("cd-1"), lambda label, fn: entries.append(label))
    assert entries == ["Start", "Reset", "Add a minute"]  # the hotkey paused it


def test_it_starts_over_when_it_comes_on_screen_and_says_when_it_ends(countdown, monkeypatch):
    asked, done = [], []
    signal = lambda out: type("S", (), {"emit": lambda self, i: out.append(i)})()  # noqa: E731
    monkeypatch.setattr(countdown, "_UI", type("Ui", (), {"restart": signal(asked), "finished": signal(done)})())
    source = create_source("countdown", "cd", {"minutes": 1.0})
    source.start()
    assert asked == ["cd"] and source.video_frame() is None  # waits for the fresh start
    source.update({**source.settings, "run_since": time.time() - 61})
    source.video_frame()
    assert done == []  # already done when it first drew: not "just ended"
    source.update({**source.settings, "run_since": time.time() - 30})
    source.video_frame()
    source.update({**source.settings, "run_since": time.time() - 61})
    source.video_frame()
    assert done == ["cd"]


def test_a_card_from_before_keeps_its_place_on_the_canvas():
    """Local time's card sat at the right of a 640-wide box: with the box now the card, its right edge stays."""
    from sprigcast.core import source as source_mod

    class Clock:
        card_box = (640, "right")

    class Timer:
        card_box = (640, "left")

    col = _collection()
    canvas = col.canvases[0]
    canvas.width, canvas.height = 1920, 1080
    col.sources += [SourceConfig(id="lt", kind="x_clock", name="a"), SourceConfig(id="st", kind="x_timer", name="b"),
                    SourceConfig(id="new", kind="x_clock", name="c", settings={CARD_W: 603.0})]
    scene = next(s for s in col.scenes if s.canvas == canvas.id)
    lt = SceneItem(source="lt", x=canvas.width - 50 - 640 * 0.5, y=20, scale=0.5)
    st = SceneItem(source="st", x=30, y=20, scale=0.5)
    new = SceneItem(source="new", x=canvas.width / 2 - 603 * 0.25, y=500, scale=0.5)
    scene.items += [lt, st, new]
    real = source_mod.source_class
    try:
        source_mod.source_class = lambda kind: {"x_clock": Clock, "x_timer": Timer}.get(kind) or real(kind)
        moved, touched = col.keep_cards_put({"lt": (603, 220), "st": (416, 220), "new": (603, 220)})
        assert moved and {c.id for c in touched} == {"lt", "st"}
        assert lt.x + 603 * 0.5 == pytest.approx(canvas.width - 50)  # right edge kept
        assert st.x == 30  # left edge kept
        assert col.source("lt").settings[CARD_W] == 603
        assert col.keep_cards_put({"lt": (603, 220), "st": (416, 220), "new": (603, 220)}) == (False, [])
        middle = new.x + 603 * 0.25
        moved, _ = col.keep_cards_put({"new": (703, 220)})  # a setting made it wider: in the middle, it grows both ways
        assert moved and new.x + 703 * 0.25 == pytest.approx(middle)
        moved, touched = col.keep_cards_put({"st": (500, 220)})  # on the left: only its width is noted
        assert not moved and [c.id for c in touched] == ["st"] and st.x == 30
    finally:
        source_mod.source_class = real


def test_adding_one_notes_the_width_it_was_laid_out_for(countdown):
    col = _collection()
    col.sources.append(SourceConfig(id="c", kind="countdown", name="Countdown"))
    col.own_sprig(col.scene("chat-h"), "c", countdown.CountdownSource.own_sprig, "Countdown", "top_left")
    assert col.source("c").settings[CARD_W] == countdown.W


def test_the_cards_start_as_wide_as_they_draw(countdown, added):
    """own_sprig (where a new card is placed) matches the box the card draws, so it starts snug in its corner."""
    added("local_time")
    added("stream_timer")
    lt, st = sys.modules["sprigcast_plugin_local_time"], sys.modules["sprigcast_plugin_stream_timer"]
    for kind, cls in (("countdown", countdown.CountdownSource), ("local_time", lt.LocalTimeSource),
                      ("stream_timer", st.StreamTimerSource)):
        source = create_source(kind, "x", dict(cls.defaults))
        source.video_frame()
        assert abs(source.size[0] - cls.own_sprig[0]) <= 2, kind


def test_countdowns_made_before_are_the_card_too(countdown):
    """Version 1's kind (countdown_timer) and the old example's (countdown, with its text settings) both draw the card."""
    from sprigcast.core.source import source_class

    assert source_class("countdown").label == "Countdown" and source_class("countdown_timer").label == ""  # not listed twice
    old_example = {"text": "", "minutes": 5, "label": "Starting in ", "font": "segoeui.ttf", "size": 72, "outline": True}
    for kind, settings in (("countdown", old_example), ("countdown_timer", {"minutes": 5.0})):
        source = create_source(kind, "x", settings)
        frame = source.video_frame()
        assert frame is not None and frame.shape[:2] == (220, source.size[0]), kind
    col, host, api = _api({"minutes": 5.0})
    col.source("cd-1").kind = "countdown_timer"
    assert api.run("countdown.get", {})["text"] == "5:00"
