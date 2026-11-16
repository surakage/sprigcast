import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


def test_build_shows_sound_strips_under_the_canvas(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    _app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastStripsTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SourceConfig

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        sources=[SourceConfig(id="mic", kind="audio_input", name="Mic", settings={"device": "default"}),
                 SourceConfig(id="desk", kind="audio_output", name="Desktop", settings={"device": "default"})],
        scenes=[Scene(id="a", name="A", canvas="horizontal")],
        active_scenes={"horizontal": "a"},
    )
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w._refresh_audio()
        strips = w.sound_strips.strips
        assert list(strips) == ["desk", "mic"]  # A to Z
        strips["mic"].fader._set_from(strips["mic"].fader._y(-12.0))
        assert col.source("mic").settings["volume_db"] == pytest.approx(-12.0, abs=1.5)
        strips["mic"].mute.click()
        assert col.source("mic").settings["muted"] is True
        assert w.sound_strips.strips["mic"].db.text() == "muted"
        w.sound_strips.set_levels({"desk": (-6.0, -6.0)})
        assert w.sound_strips.strips["desk"].fader.level > 0.8
        strips = w.sound_strips.strips
        strips["desk"].name.click()
        assert w.selected_sound_id == "desk" and w.build_stage.right_stack.currentIndex() == 1
        w.sound_strips.set_open(False)
        assert not w.sound_strips.scroll.isVisibleTo(w) and not QSettings().value("build/sound_strips_open", type=bool)
    finally:
        w.close()
        QSettings().clear()


def test_a_sound_change_updates_in_place_and_refreshes_once(tmp_path, monkeypatch):
    """Muting or a filter edit used to rebuild every strip and Sound card row per change (up to 2 s frozen)."""
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    _app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastStripsTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SourceConfig

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        sources=[SourceConfig(id="mic", kind="audio_input", name="Mic", settings={"device": "default"}),
                 SourceConfig(id="desk", kind="audio_output", name="Desktop", settings={"device": "default"})],
        scenes=[Scene(id="a", name="A", canvas="horizontal")],
        active_scenes={"horizontal": "a"},
    )
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w._refresh_audio()
        card = w.live_side.sound
        strips, rows = dict(w.sound_strips.strips), dict(card.rows)
        refreshes = []
        real = w._refresh_audio
        monkeypatch.setattr(w, "_refresh_audio", lambda: (refreshes.append(1), real()))
        for on in (True, False, True):  # three quick changes
            w._on_audio_changed("mic", {"muted": on})
        assert refreshes == []  # nothing yet: it waits for changes to settle
        QTest.qWait(200)
        assert refreshes == [1]  # then once
        assert w.sound_strips.strips == strips and card.rows == rows  # the same widgets, updated in place
        assert w.sound_strips.strips["mic"].db.text() == "muted"
        assert card.notes["mic"].text() == "muted" and card.meters["mic"].muted
    finally:
        w.close()
