"""The Live count store plugin (user, 2026-11-27): how many watch across the platforms you're live on, from
SprigCast's own viewers.counts (YouTube, Twitch, Kick) plus any platform an app sends in with livecount.set."""

from types import SimpleNamespace

import pytest

from sprigcast import plugin_store
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def lc(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    plugin = next(p for p in plugin_store.catalog() if p.id == "live_count")
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    import sys

    module = sys.modules["sprigcast_plugin_live_count"]
    module._read, module._others = {}, {}
    yield module
    module._read, module._others = {}, {}
    plugin_store.remove(tmp_path / "plugins", "live_count")
    assert "live_count" not in known_kinds()


def test_it_adds_up_the_platforms_live_now(lc):
    read = {"youtube": {"live": True, "viewers": 4}, "twitch": {"live": True, "viewers": 1205},
            "kick": {"live": False}}
    got = lc.counted({}, read, {"Facebook": 3})
    assert got["total"] == 1212 and got["live"] and not got["waiting"]
    assert got["each"] == [("YouTube", 4), ("Twitch", 1205), ("Facebook", 3)]
    assert lc.words({}, got) == ("1,212", "watching")
    assert lc.words({"each_platform": True}, got)[1] == "YouTube 4  ·  Twitch 1,205  ·  Facebook 3"
    assert lc.counted({"twitch": False, "other_apps": False}, read, {"Facebook": 3})["total"] == 4
    waiting = lc.counted({}, {"kick": {"live": True, "viewers": None}}, {})
    assert waiting["live"] and waiting["waiting"] and lc.words({}, waiting)[0] == "–"
    assert not lc.counted({}, {"kick": {"live": False}}, {})["live"]


def test_hidden_while_not_live_and_drawn_when_the_number_changes(lc):
    source = create_source("live_count", "c", {})
    clear = source.video_frame()
    assert clear is not None and not clear[..., 3].any()  # not live: a clear card
    assert source.video_frame() is None  # and nothing more while that stays so
    lc._read = {"twitch": {"live": True, "viewers": 5}}
    first = source.video_frame()
    assert first is not None and first.shape[0] == 220 and first[..., 3].any()
    assert source.video_frame() is None  # same number: nothing drawn
    lc._read = {"twitch": {"live": True, "viewers": 6}}
    assert source.video_frame() is not None


def test_apps_send_other_platforms_and_read_the_count(lc, monkeypatch):
    from sprigcast.control.native import ApiError, _PLUGIN_ACTIONS

    api = SimpleNamespace()
    got = _PLUGIN_ACTIONS["livecount.set"].run(api, {"platform": "Facebook", "viewers": "12"})
    assert got == {"total": 12, "platforms": {"Facebook": 12}, "live": True}
    with pytest.raises(ApiError):
        _PLUGIN_ACTIONS["livecount.set"].run(api, {"platform": "Facebook", "viewers": "lots"})
    now = lc.time.monotonic()
    monkeypatch.setattr(lc.time, "monotonic", lambda: now + lc.OTHER_KEEP_S + 1)
    assert _PLUGIN_ACTIONS["livecount.get"].run(api, {})["total"] == 0  # that app went quiet
    _PLUGIN_ACTIONS["livecount.set"].run(api, {"platform": "Facebook", "viewers": -1})
    assert "Facebook" not in lc._others


def test_the_window_asks_only_while_a_live_count_is_in_the_garden(lc):
    asked = []
    api = SimpleNamespace(col=SimpleNamespace(sources=[]),
                          viewers_counts=lambda data: asked.append(data) or {"platforms": {"kick": {"live": False}}})
    lc.look_now(api)
    assert asked == [] and lc._read == {}
    api.col.sources = [SimpleNamespace(kind="live_count")]
    lc.look_now(api)
    assert asked == [{}] and lc._read == {"kick": {"live": False}}


def test_the_eye_blinks_now_and_then_while_live(lc, monkeypatch):
    assert lc.openness(0.0) == 0.5 and lc.openness(0.07) == 0.0 and lc.openness(1.0) == 1.0
    clock = [100.0]
    monkeypatch.setattr(lc.time, "monotonic", lambda: clock[0])
    lc._read = {"twitch": {"live": True, "viewers": 5}}
    source = create_source("live_count", "b", {})
    open_ = source.video_frame()
    assert open_ is not None and source.video_frame() is None  # open: nothing more to draw
    clock[0] = source._blink_at + 0.07
    shut = source.video_frame()
    assert shut is not None and not (shut.shape == open_.shape and (shut == open_).all())  # the lid closed
    clock[0] = source._blink_at + 0.5
    assert source.video_frame() is not None  # open again, and the next blink is some seconds away
    assert 0.1 <= source._blink_at - clock[0] <= lc.BLINK_EVERY[1]
    source.update({**source.settings, "blink": False})
    clock[0] = source._blink_at + 0.07
    source.video_frame()
    assert source.video_frame() is None  # Blink off: the eye stays open


def test_the_eye_stays_shut_with_nobody_watching(lc, monkeypatch):
    seen = []
    real = lc.paint_fitted
    monkeypatch.setattr(lc, "paint_fitted", lambda *a: seen.append(a[-1]) or real(*a))
    lc._read = {"twitch": {"live": True, "viewers": 0}}
    source = create_source("live_count", "z", {"hide_when_offline": False})
    for _ in range(3):
        source.video_frame()
    lc._read = {"twitch": {"live": True, "viewers": 3}}
    source.video_frame()
    lc._read = {}
    source.video_frame()  # not live, still shown: no one watching
    assert seen[0] == 0.0 and seen[1] == 1.0 and seen[-1] == 0.0
