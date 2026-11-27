"""The Break & End Guard store plugin (user, 2026-11-27): breaks while live on your Break bloom, then back, and a
heads-up before the stream limit. Never stops the stream; off air ends everything."""

from types import SimpleNamespace

import pytest

from sprigcast import plugin_hooks, plugin_store
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")

M = 60.0


@pytest.fixture
def bg(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    plugin = next(p for p in plugin_store.catalog() if p.id == "break_guard")
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    import sys

    module = sys.modules["sprigcast_plugin_break_guard"]
    module.GUARD = module.Guard()
    yield module
    plugin_hooks.output_changed("stream", None)
    plugin_store.remove(tmp_path / "plugins", "break_guard")
    assert "break_guard" not in known_kinds()


def kinds(events):
    return [e[0] for e in events]


def test_a_break_every_hour_with_a_warning_then_back(bg):
    g, s = bg.GUARD, {"every": 60, "warn": 5, "break_minutes": 5, "limit": 240, "end_warn": 15}
    assert g.tick(s, None, 0) == []
    assert g.tick(s, 0.0, 10) == []
    assert kinds(g.tick(s, 0.0, 55 * M)) == ["break_soon"]
    assert g.card(s, 55 * M) == ("soon", 5 * M)
    assert g.tick(s, 0.0, 56 * M) == []  # warned once
    assert kinds(g.tick(s, 0.0, 60 * M)) == ["break_start"]
    assert g.card(s, 62 * M)[0] == "break"
    assert kinds(g.tick(s, 0.0, 65 * M)) == ["break_end"] and g.card(s, 65 * M) is None
    assert g.next_break == 125 * M  # an hour after the break ended


def test_the_end_warning_one_more_hour_and_skipped_breaks(bg):
    g, s = bg.GUARD, {"every": 60, "warn": 5, "break_minutes": 5, "limit": 120, "end_warn": 15}
    g.tick(s, 0.0, 1)
    for t in (55, 60, 65):
        g.tick(s, 0.0, t * M)
    assert g.next_break is None  # the next one would run into the end warning
    assert kinds(g.tick(s, 0.0, 105 * M)) == ["end_soon"]
    g.later(3600, 106 * M, end=True)
    assert g.tick(s, 0.0, 120 * M) == []
    assert kinds(g.tick(s, 0.0, 165 * M)) == ["end_soon"]
    assert kinds(g.tick(s, 0.0, 180 * M)) == ["end"]
    assert g.tick(s, 0.0, 181 * M) == []


def test_asks_first_when_not_by_itself_and_off_air_ends_it(bg):
    g, s = bg.GUARD, {"every": 30, "auto_break": False, "limit": 0}
    g.tick(s, 0.0, 1)
    assert kinds(g.tick(s, 0.0, 30 * M)) == ["ask_break"]
    assert g.tick(s, 0.0, 31 * M) == [] and g.next_break == 60 * M  # asked again in 30 minutes
    g.start(s, 32 * M)
    assert kinds(g.tick(s, None, 33 * M)) == ["break_over"] and g.card(s, 33 * M) is None


def _api(bg, active):
    bloom = lambda i, n: SimpleNamespace(id=i, name=n, kind="show")  # noqa: E731
    blooms = [bloom("main", "Main"), bloom("brb", "Be right back (break)")]
    shown = []
    col = SimpleNamespace(sources=[SimpleNamespace(kind="break_guard", settings={})], scenes=blooms,
                          active_scenes=dict(active))
    host = SimpleNamespace(commit=lambda what: shown.append(dict(col.active_scenes)), _saved_note=lambda *a, **k: None)

    def show(data):
        col.active_scenes = {"h": data["bloom"]}
        shown.append(dict(col.active_scenes))

    api = SimpleNamespace(col=col, host=host, _blooms=lambda: blooms, bloom_show=show,
                          _bloom=lambda data: next(b for b in blooms if b.name == data["bloom"]))
    return api, shown


def test_a_break_shows_the_break_bloom_then_puts_things_back(bg, monkeypatch):
    from sprigcast.control.native import _PLUGIN_ACTIONS

    monkeypatch.setattr(bg, "send_event", lambda *a: None)
    api, shown = _api(bg, {"h": "main"})
    got = _PLUGIN_ACTIONS["break.start"].run(api, {"minutes": 2})
    assert got["onBreak"] and 100 < got["breakLeft"] <= 120 and shown[-1] == {"h": "brb"}
    got = _PLUGIN_ACTIONS["break.end"].run(api, {})
    assert not got["onBreak"] and shown[-1] == {"h": "main"}


def test_the_card_counts_down_with_a_vine(bg, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(bg.time, "monotonic", lambda: clock[0])
    source = create_source("break_guard", "b", {})
    clear = source.video_frame()
    assert clear is not None and not clear[..., 3].any()
    bg.GUARD.start({}, 5000.0, 300.0)
    words = bg.card_words({"text": "Back in", "label": "Be right back"}, "break", 272.0, 300.0)
    assert words.big == "Back in 4:32" and words.small == "Be right back" and 0.9 < words.vine < 0.91
    first = source.video_frame()
    assert first is not None and first[..., 3].any()
    assert source.video_frame() is None  # same second
    clock[0] += 1
    assert source.video_frame() is not None
