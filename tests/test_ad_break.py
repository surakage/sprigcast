"""The Ad break countdown store plugin (user, 2026-11-27): Twitch's ad schedule, read with SprigCast's own
sign-in (channel:read:ads), as "Ads in 0:45" before and "Back in 0:30" during."""

from types import SimpleNamespace

import pytest

from sprigcast import plugin_hooks, plugin_store
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def ad(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    plugin = next(p for p in plugin_store.catalog() if p.id == "ad_break")
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    import sys

    module = sys.modules["sprigcast_plugin_ad_break"]
    module.SCHEDULE = module.Schedule()
    yield module
    plugin_hooks.output_changed("stream", None)
    plugin_store.remove(tmp_path / "plugins", "ad_break")
    assert "ad_break" not in known_kinds()


def test_before_and_during_an_ad(ad):
    s = ad.SCHEDULE
    s.take({"next_ad_at": 1000, "last_ad_at": 0, "duration": 90, "snooze_count": 3}, 0.0)
    assert s.state(900.0, 60) is None
    assert s.state(955.0, 60) == ("soon", 45.0, 60)
    assert s.state(1030.0, 60) == ("during", 60.0, 90.0)  # began before the next read
    s.take({"next_ad_at": 4600, "last_ad_at": 1000, "duration": 90}, 1.0)
    assert s.state(1060.0, 60) == ("during", 30.0, 90.0) and s.state(1100.0, 60) is None
    w = ad.card_words({"during_text": "Ads are playing"}, "during", 30.0, 90.0)
    assert w.big == "Back in 0:30" and w.small == "Ads are playing" and w.badge == "hourglass"
    assert ad.card_words({}, "soon", 44.2, 60).big == "Ads in 0:45"


def test_read_only_while_live_with_a_countdown_and_more_often_near_an_ad(ad, monkeypatch):
    from sprigcast.output import twitch_auth

    calls = []
    monkeypatch.setattr(twitch_auth, "helix_get", lambda path_for, scope=None: calls.append((path_for("42"), scope))
                        or {"data": [{"next_ad_at": 10_000, "duration": 60}]})
    started = []
    monkeypatch.setattr(ad.threading, "Thread", lambda target, **k: SimpleNamespace(start=lambda: started.append(1)
                                                                                      or target()))
    api = SimpleNamespace(col=SimpleNamespace(sources=[]))
    ad.look_now(api)
    api.col.sources = [SimpleNamespace(kind="ad_break")]
    ad.look_now(api)  # off air
    assert calls == []
    plugin_hooks.output_changed("stream", 1.0)
    ad.look_now(api)
    assert calls == [("/channels/ads?broadcaster_id=42", "channel:read:ads")] and ad.SCHEDULE.next_ad == 10_000
    ad.look_now(api)
    assert len(calls) == 1  # not again so soon
    assert ad.SCHEDULE.due(ad.SCHEDULE.read_at + 11, 10_000 - 60)  # close to the ad: every 10 s
    assert not ad.SCHEDULE.due(ad.SCHEDULE.read_at + 11, 5_000)


def test_signed_in_before_the_permission_says_so(ad, monkeypatch):
    from sprigcast.output import twitch_auth

    monkeypatch.setattr(twitch_auth, "_load", lambda: {"scope": ["user:write:chat"], "user_id": "1",
                                                       "access_token": "t", "expires_at": 9e12})
    ad._read()
    assert "Connect Twitch again" in ad.SCHEDULE.problem
    source = create_source("ad_break", "a", {})
    clear = source.video_frame()
    assert not clear[..., 3].any() and "Connect Twitch again" in source.status


def test_the_card_shows_around_an_ad(ad, monkeypatch):
    now = [5000.0]
    monkeypatch.setattr(ad.time, "time", lambda: now[0])
    ad.SCHEDULE.take({"next_ad_at": 5030, "duration": 60}, 0.0)
    source = create_source("ad_break", "a", {})
    card = source.video_frame()
    assert card is not None and card[..., 3].any()
    assert source.video_frame() is None
    now[0] = 5200.0
    assert not source.video_frame()[..., 3].any()
