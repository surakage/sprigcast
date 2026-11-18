"""The Local time store plugin: the streamer's own time on a small card, drawn only when it changes."""

from datetime import datetime

import pytest

from sprigcast import plugin_store
from sprigcast.core.scene import SourceConfig
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")

from test_control_api import _collection  # noqa: E402


@pytest.fixture
def clock(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    plugin = next(p for p in plugin_store.catalog() if p.id == "local_time")
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    import sys

    yield sys.modules["sprigcast_plugin_local_time"]
    plugin_store.remove(tmp_path / "plugins", "local_time")
    assert "local_time" not in known_kinds()


def test_words_follow_the_settings(clock):
    evening = datetime(2026, 11, 14, 20, 3, 7)
    assert clock.words(evening, {}) == ("8:03 PM", "Sat, Nov 14")
    assert clock.words(datetime(2026, 11, 15, 0, 5), {"show_date": False}) == ("12:05 AM", "")
    assert clock.words(evening, {"hour_24": True, "seconds": True, "show_date": False}) == ("20:03:07", "")
    _, under = clock.words(evening, {"show_date": False, "time_zone": True})
    assert under == clock.zone_name() and 0 < len(under) <= 5


def test_it_draws_only_when_the_words_change(clock, monkeypatch):
    times = iter([datetime(2026, 11, 14, 20, 3, s) for s in (1, 2, 3)] + [datetime(2026, 11, 14, 20, 4, 0)])
    monkeypatch.setattr(clock, "datetime", type("Fixed", (), {"now": staticmethod(lambda: next(times))}))
    source = create_source("local_time", "t", {})
    w, h = source.size
    assert h == 220 and 300 < w < 640  # the box is the card (the old box was 640 wide with room beside it)
    first = source.video_frame()
    assert first.shape == (220, w, 4) and first[..., 3].any()
    assert first[:, :8, 3].any() and first[:, -8:, 3].any()  # the card reaches both sides of its box
    assert source.video_frame() is None and source.video_frame() is None  # same minute: nothing drawn
    assert source.video_frame() is not None  # 8:04
    source.update({**source.settings, "card_color": "#00000000"})
    monkeypatch.setattr(clock, "datetime", type("Fixed", (), {"now": staticmethod(lambda: datetime(2026, 11, 14, 20, 4))}))
    assert source.video_frame() is not None  # a new look is drawn straight away


def test_adding_it_puts_a_small_clock_in_the_corner_of_both_shapes():
    col = _collection()
    col.sources.append(SourceConfig(id="t", kind="local_time", name="Local time"))
    part = col.own_sprig(col.scene("chat-h"), "t", (640, 220), "Local time", "top_right")
    for shape in col.shapes(part):
        canvas = col.canvas(shape.canvas)
        item = shape.items[0]
        w = 640 * item.scale
        top = 0.12 * canvas.height if canvas.height > canvas.width else min(canvas.width, canvas.height) * 0.035
        assert w < canvas.width / 2 and item.y == pytest.approx(top)  # small, at the top (under TikTok's tabs)
        assert item.x + w == pytest.approx(canvas.width - min(canvas.width, canvas.height) * 0.035)  # right edge
