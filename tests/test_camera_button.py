"""Live's Virtual camera button and its gear pop-up (the same Shows setting as Settings → Output)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.core.scene import Canvas, Scene, SceneCollection, SourceConfig  # noqa: E402


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("CameraButtonTest")
    QSettings().clear()
    yield app
    QSettings().clear()


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        sources=[SourceConfig(id="cam", kind="color", name="Facecam"),
                 SourceConfig(id="mic", kind="audio_input", name="Mic")],
        scenes=[Scene(id="talk", name="Talking", canvas="horizontal"),
                Scene(id="tall", name="Tall", canvas="vertical")],
        active_scenes={"horizontal": "talk", "vertical": "tall"},
    )


def test_shows_picks_a_canvas_bloom_or_source_and_saves_the_shared_setting(app):
    from sprigcast.app.camera_popup import CameraPicker
    from sprigcast.app.output_page import camera_choice

    picker = CameraPicker(_collection())
    picker.show()
    labels = [picker.shows.itemText(i) for i in range(picker.shows.count())]
    assert labels == ["Live output", "Horizontal", "Vertical", "A bloom", "A source"]
    assert picker.choice() == "auto" and not picker.which.isVisible()  # no second drop-down yet

    picker.shows.setCurrentIndex(picker.shows.findData("bloom"))
    assert picker.which.isVisible() and picker.which.count() == 2
    picker.which.setCurrentIndex(1)
    assert camera_choice() == "bloom:tall"

    picker.shows.setCurrentIndex(picker.shows.findData("source"))
    assert [picker.which.itemText(i) for i in range(picker.which.count())] == ["Facecam"]  # no sound sources
    assert camera_choice() == "source:cam"

    picker.shows.setCurrentIndex(picker.shows.findData("canvas:vertical"))
    assert camera_choice() == "canvas:vertical" and not picker.which.isVisible()


def test_picker_opens_on_the_choice_made_in_settings(app):
    from sprigcast.app.camera_popup import camera_popup
    from sprigcast.app.output_page import CAMERA_SHOWS, CameraTab

    QSettings().setValue(CAMERA_SHOWS, "bloom:tall")
    pop = camera_popup(_collection())
    assert pop.picker.shows.currentData() == "bloom" and pop.picker.which.currentData() == "tall"
    pop.picker.shows.setCurrentIndex(pop.picker.shows.findData("source"))
    tab = CameraTab(_collection())  # Settings → Output → Virtual camera agrees
    assert tab.shows.currentData() == "source:cam"


def test_button_shows_on_and_a_short_reason_when_it_cant_start(app):
    from sprigcast.app.live_stage import CARD_PILL_STYLE, CARD_SELECTED_STYLE, LiveSide

    clicks = []
    side = LiveSide(lambda: None, lambda: None, lambda: None, lambda _id: None,
                    on_camera=lambda: clicks.append("cam"), on_camera_settings=lambda: clicks.append("gear"))
    side.show()
    side.camera.click()
    side.camera_settings.click()
    assert clicks == ["cam", "gear"]
    side.set_camera(True)
    assert side.camera.text() == "Cam on" and side.camera.styleSheet() == CARD_SELECTED_STYLE
    assert not side.camera_note.isVisible()
    side.set_camera(False, "Windows refused the camera (0x80070005).")
    assert side.camera.text() == "Virtual cam" and side.camera.styleSheet() == CARD_PILL_STYLE
    assert side.camera_note.isVisible() and "refused" in side.camera_note.text()


def test_main_window_button_turns_the_camera_off_again_when_start_fails(app, tmp_path, monkeypatch):
    import keyring

    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow
    from sprigcast.app.output_page import camera_wanted
    from sprigcast.output import virtualcam

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    monkeypatch.setattr(virtualcam, "supported", lambda: (True, ""))
    monkeypatch.setattr(virtualcam, "installed", lambda: True)

    class Refused:
        error = "Windows refused the camera (0x80070005)."

        def __init__(self, *a, **k):
            pass

        def start(self):
            return False

    monkeypatch.setattr(virtualcam, "VirtualCamera", Refused)
    w = MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")
    try:
        w._toggle_camera()
        assert w.camera is None and not camera_wanted()
        assert w.live_side.camera_note.text() == Refused.error
        monkeypatch.setattr(virtualcam, "installed", lambda: False)
        w._toggle_camera()
        assert "isn't set up" in w.live_side.camera_note.text()
    finally:
        w.close()
