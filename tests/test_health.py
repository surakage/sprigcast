"""Stream health: the reconnect / bitrate banner on Live, and the last stream's health page."""

import os
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.output.autopilot import Autopilot  # noqa: E402


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("HealthTest")
    QSettings().clear()
    yield app
    QSettings().clear()


def _streamer(name, **kw):
    base = dict(name=name, connected=True, status="live", frames=0, dropped=0, reconnects=0, autopilot=None,
                bitrate_kbps=6000)
    return types.SimpleNamespace(**{**base, **kw})


def test_notice_says_when_a_place_reconnects_or_its_bitrate_is_lowered():
    from sprigcast.app.health import notice

    assert notice([("horizontal", _streamer("Twitch"))]) == ("", "")
    pilot = Autopilot(6000)
    pilot.update(0.0, 2.0)  # far behind: a big step down
    lowered = _streamer("YouTube", autopilot=pilot)
    assert notice([("horizontal", lowered)]) == ("warning", f"YouTube: bitrate lowered to {pilot.current_kbps} kbps")
    dropped = _streamer("Twitch", connected=False, reconnects=2, status="reconnecting (try 2)")
    level, text = notice([("horizontal", dropped), ("horizontal", lowered)])
    assert level == "bad" and text.startswith("Reconnecting to Twitch… try 2  ·  YouTube: bitrate lowered")
    first = _streamer("Kick", connected=False, status="connecting")  # going live, not a reconnect
    assert notice([("vertical", first)]) == ("", "")


def test_session_health_counts_drops_reconnects_memory_and_hitches(app):
    from sprigcast.app import health

    pilot = Autopilot(6000)
    pilot.update(0.0, 2.0)
    st = _streamer("Twitch", frames=3600, dropped=12, reconnects=1, autopilot=pilot)
    session = types.SimpleNamespace(streamers=[("horizontal", st)])
    t = [100.0]
    memory = iter([300.0, 480.0, 410.0, 410.0])
    watch = health.SessionHealth(session, clock=lambda: t[0], memory_mb=lambda: next(memory))
    for late in (0, 0, 5, 6, 40):  # the first event only sets the start; +1 isn't a hitch, +34 is
        watch.render(types.SimpleNamespace(dropped_ticks=late))
    for _ in range(3):
        t[0] += 60
        watch.tick()
    record = watch.finish(QSettings())
    assert record.seconds == 180 and record.peak_mb == 480 and record.hitches == 2 and record.late_frames == 40
    place = record.places[0]
    assert (place.dropped, place.reconnects, place.steps_down) == (12, 1, 1)
    assert place.lowest_kbps == pilot.current_kbps and place.target_kbps == 6000
    assert record.verdict()[0] == "bad"  # a reconnect
    saved = health.load(QSettings())
    assert len(saved) == 1 and saved[0].places[0].name == "Twitch" and saved[0].peak_mb == 480
    for _ in range(7):
        health.save(QSettings(), health.HealthRecord(started="2026-11-15 20:00", seconds=60))
    assert len(health.load(QSettings())) == health.KEEP  # only the last few are kept


def test_health_page_shows_tiles_per_place_lines_and_an_empty_state(app):
    from sprigcast.app.health import HealthRecord, PlaceHealth
    from sprigcast.app.health_page import HealthPage, health_popup

    empty = HealthPage([])
    assert not hasattr(empty, "pick")
    smooth = HealthRecord(started="2026-11-14 19:00", seconds=3600 * 2 + 300, peak_mb=512,
                          places=[PlaceHealth(name="Twitch", frames=432000, target_kbps=6000)])
    rough = HealthRecord(started="2026-11-15 20:10", seconds=1200, peak_mb=620, hitches=4,
                         places=[PlaceHealth(name="Twitch", frames=72000, dropped=900, reconnects=6,
                                             lowest_kbps=1400, target_kbps=6000, steps_down=3)])
    pop = health_popup([rough, smooth])
    page = pop.page
    assert page.pick.count() == 2 and "Rough" in page.verdict.text()
    texts = [page.tiles.itemAt(i).widget().findChildren(type(page.verdict))[0].text() for i in range(page.tiles.count())]
    assert texts == ["20 min", "900", "6", "620 MB", "4"]
    line = page.places.itemAt(0).widget().findChildren(type(page.verdict))[1].text()
    assert "6 reconnects" in line and "dipped to 1400 of 6000 kbps" in line
    page.pick.setCurrentIndex(1)
    assert "Smooth" in page.verdict.text()


def test_live_shows_the_banner_and_the_garden_check_opens_the_last_stream(app, tmp_path, monkeypatch):
    import keyring

    from sprigcast.app import health
    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    w = MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")
    try:
        w.show()
        w._set_mode("live")
        card, side = w.live_stage.card, w.live_side
        card.set_notice("Reconnecting to Twitch… try 2", "bad")
        assert card.notice.isVisible() and "try 2" in card.notice.text()
        card.set_notice("")
        assert not card.notice.isVisible()
        w.last_resources = {"cpu": 4.6, "ram_mb": 209}
        w._refresh_garden()
        assert side.garden.numbers.text() == "CPU 4.6%  ·  209 MB  ·  0 dropped" and side.garden.level == "good"
        health.save(QSettings(), health.HealthRecord(started="2026-11-15 20:10", seconds=600))
        pop = w._open_health()  # Tools → Stream health: the Garden check, on the last streams
        assert pop.page.records[0].seconds == 600 and pop.garden.stack.currentWidget() is pop.page
        pop.close()
        side.garden.click()
        pop = w.findChildren(type(pop))[-1]
        garden = pop.garden
        assert garden.stack.currentIndex() == 0  # now: tiles, stream health and checks
        assert garden.values["cpu"].text() == "4.6%" and garden.values["ram"].text() == "209 MB"
        assert garden.values["recording"].text() == "Off" and garden.values["upload"].text() == "Not live"
        labels = [garden.checks.itemAt(i).widget().text() for i in range(garden.checks.count())]
        assert any("GPU encoder" in t for t in labels) and any("Disk has room" in t for t in labels)
        garden.history.click()
        assert garden.stack.currentWidget() is pop.page
        garden.done.click()
        assert not pop.isVisible()
    finally:
        w.close()


def test_live_canvases_have_no_caption_above_them(app, tmp_path, monkeypatch):
    import keyring

    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    w = MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")
    try:
        w.show()
        w._set_mode("live")
        w._refresh_previews()
        assert not any(p.show_title for p in w.previews.values())  # just the picture
        w._set_mode("build")
        w._refresh_previews()
        assert all(p.show_title == p.twin for p in w.previews.values())
    finally:
        w.close()


def test_header_shows_one_clock_and_the_details_beside_it(app, tmp_path, monkeypatch):
    """Streaming and recording at once: one unlabelled clock (the longer one), then both sets of details."""
    import keyring

    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    w = MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")
    try:
        w.show()
        w._set_mode("live")
        w._side_status("rec", 2299, "1473 frames dropped: encoder busy", True)
        assert "0:38:19" in w.mode_clock.text() and "REC" not in w.mode_clock.text()
        w._live_destinations()  # an offline refresh doesn't replace the clock while recording
        assert "0:38:19" in w.mode_clock.text()
        w._side_status("live", 2400, "2 of 2 sending")
        text = w.mode_clock.text()
        assert "0:40:00" in text and "2 of 2 sending" in text and "1473 frames dropped" in text
        w._side_status("live", None)
        w._side_status("rec", None)
        assert w.mode_clock.text().startswith("Offline")
    finally:
        w.close()
