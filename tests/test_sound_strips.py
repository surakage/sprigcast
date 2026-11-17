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


def test_a_new_sound_keeps_the_other_strips(monkeypatch):
    """PC test 2026-11-17: a sound coming or going made every strip again (0.3 s while live). Now only it."""
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    from sprigcast.app.sound_card import Sound
    from sprigcast.app.sound_strips import SoundStrips

    strips = SoundStrips(lambda *a: None, lambda *a: None)
    mic, desk, music = Sound(id="mic", name="Mic"), Sound(id="desk", name="Desktop"), Sound(id="m", name="Music")
    kinds = {"mic": "audio_input", "desk": "audio_output", "m": "media"}
    strips.show_sound([desk, mic], kinds)
    first = dict(strips.strips)
    strips.show_sound([desk, music, mic], kinds)
    assert strips.strips["desk"] is first["desk"] and strips.strips["mic"] is first["mic"]
    order = [strips.row.itemAt(i).widget() for i in range(strips.row.count() - 1)]
    assert order == [strips.strips["desk"], strips.strips["m"], strips.strips["mic"]]
    strips.show_sound([mic], kinds)
    assert list(strips.strips) == ["mic"] and strips.strips["mic"] is first["mic"]
    assert strips.row.count() == 2  # the strip and the stretch


def test_changes_save_once_they_settle_and_closing_saves_what_waits(tmp_path, monkeypatch):
    """2026-11-17 heavy run: every bloom switch and hotkey wrote the whole garden on the window's thread."""
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    _app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastStripsTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import Canvas, Scene, SceneCollection

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                          scenes=[Scene(id="a", name="A", canvas="horizontal")], active_scenes={"horizontal": "a"})
    path = tmp_path / "scenes.json"
    w = MainWindow(col, path)
    try:
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        col.scenes[0].name = "Renamed"
        w._changed()
        w._changed()
        assert w._save_timer.isActive()
        assert (path.read_text(encoding="utf-8") if path.exists() else "") == before  # not yet
        w._save_pending()  # what closing does
        assert not w._save_timer.isActive()
        assert SceneCollection.load(path).scenes[0].name == "Renamed"
    finally:
        w.close()


def test_gear_clicks_are_logged_and_a_window_opened_early_is_made_whole(tmp_path, monkeypatch, caplog):
    """PC test 2026-11-17: after a relaunch, clicks on the Settings gear did nothing and the log said nothing."""
    import logging

    import keyring
    from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QSettings, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastStripsTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import Canvas, Scene, SceneCollection

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                          scenes=[Scene(id="a", name="A", canvas="horizontal")], active_scenes={"horizontal": "a"})
    w = MainWindow(col, tmp_path / "scenes.json")
    opened = []
    monkeypatch.setattr(w, "_open_settings", lambda *a, **k: opened.append(a))
    try:
        with caplog.at_level(logging.INFO, logger="sprigcast.app.main_window"):
            press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
                                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            app.sendEvent(w.settings_button, press)
            w.settings_button.click()
        assert "settings: gear pressed" in caplog.text and "settings: gear clicked" in caplog.text
        assert opened == [()]
        w.show_minimized_drawn(settle_ms=10_000)  # a minimized start, still being drawn...
        w.showMaximized()  # ...when another app opens it
        app.processEvents()
        assert w.windowOpacity() == 1.0
        assert not w.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    finally:
        w.close()
