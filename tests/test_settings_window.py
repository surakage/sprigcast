import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.core.scene import Canvas, SceneCollection, SourceConfig  # noqa: E402


@pytest.fixture
def app(monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("SettingsWindowTest")
    QSettings().clear()
    yield app
    QSettings().clear()


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        sources=[SourceConfig(id="mic-1", kind="audio_input", name="Mic", settings={"device": "default"})],
    )


def _window(col, **kw):
    from sprigcast.app.settings_window import SettingsWindow

    args = dict(default_recordings_folder="C:/Videos/SprigCast", encoder="NVENC", audio_devices={"audio_input": ["USB Mic"]})
    args.update(kw)
    return SettingsWindow(col, **args)


def test_tabs_and_apply_writes_everything(app):
    from sprigcast.app.settings_window import CONFIRM_STREAM, TABS, apply_pending_video, confirm_enabled

    col = _collection()
    w = _window(col, tab="Video")
    assert [w.tabs.item(i).text() for i in range(w.tabs.count())] == TABS
    assert w.current_tab() == "Video"
    fired = []
    w.applied.connect(lambda: fired.append(True))

    w.recording_page.canvas_boxes["vertical"].setChecked(False)
    w.recording_page.container.setCurrentIndex(w.recording_page.container.findData("mp4"))
    w.audio_combos["mic-1"].setCurrentIndex(w.audio_combos["mic-1"].findData("USB Mic"))
    w.confirm_stream.setChecked(False)
    w.theme.setCurrentIndex(w.theme.findData("system"))
    enabled, width, height, fps = w.video_fields["horizontal"]
    width.setValue(1280)
    height.setValue(720)
    fps.setCurrentIndex(fps.findData(30))
    assert w.apply() and fired

    assert col.recording.canvases == ["horizontal"] and col.recording.container == "mp4"
    assert col.sources[0].settings["device"] == "USB Mic" and col.sources[0].name == "Mic (USB Mic)"
    assert not confirm_enabled(CONFIRM_STREAM)
    assert QSettings().value("general/theme") == "system"
    assert (col.canvases[0].width, col.canvases[0].fps) == (1920, 60)  # canvas waits for a restart
    assert apply_pending_video(col)
    assert (col.canvases[0].width, col.canvases[0].height, col.canvases[0].fps) == (1280, 720, 30)
    assert not apply_pending_video(col)  # applied once


def test_bad_hotkey_blocks_apply(app, monkeypatch):
    from PySide6.QtGui import QKeySequence

    col = _collection()
    w = _window(col)
    edits = list(w.hotkeys_page.edits.values())
    edits[0].setKeySequence(QKeySequence("Ctrl+Alt+1"))
    edits[1].setKeySequence(QKeySequence("Ctrl+Alt+1"))
    from PySide6.QtWidgets import QMessageBox

    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    assert not w.apply()
    assert w.current_tab() == "Hotkeys" and "used twice" in shown[0]
    assert col.hotkeys == {}


def test_live_disables_stream_and_video(app):
    w = _window(_collection(), streaming=True)
    assert not w.stream_page.isEnabled()
    assert not w.video_fields["horizontal"][1].isEnabled()
    assert w.recording_page.isEnabled()
