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
