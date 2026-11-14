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


def test_plugins_have_their_own_tab(app):
    from sprigcast.plugins import PluginReport

    report = PluginReport(loaded=["countdown_example.py"], failed={"broken.py": "SyntaxError: oops"})
    w = _window(_collection(), tab="Plugins", plugin_report=report, open_plugins_folder=lambda: None)
    assert w.current_tab() == "Plugins"
    rows = [w.plugin_list.item(i).text() for i in range(w.plugin_list.count())]
    assert any("countdown_example.py" in r for r in rows) and any("broken.py" in r for r in rows)
    assert not hasattr(w, "plugin_status")  # nothing about plugins left on General


def test_output_tab_encoder_settings(app):
    from sprigcast.core.scene import Output

    col = _collection()
    w = _window(col, tab="Recording")  # old name opens Output → Recording
    assert w.current_tab() == "Output" and w.output_page.tabs.currentIndex() == 1
    assert not w.output_page.banner.isVisibleTo(w)
    f = w.output_page.stream_form
    f.rate_control.setCurrentIndex(f.rate_control.findData("cqp"))
    assert f.cq_level.isVisibleTo(f)
    f.preset.setCurrentIndex(f.preset.findData("p7"))
    f.multipass.setCurrentIndex(f.multipass.findData("qres"))
    f.b_frames.setValue(0)
    f.lookahead.setChecked(True)
    w.output_page.record_form.keyint.setValue(4)
    assert w.apply()
    enc = col.stream_encoder
    assert (enc.rate_control, enc.preset, enc.multipass, enc.b_frames, enc.lookahead) == ("cqp", "p7", "qres", 0, True)
    assert col.recording.encoder.keyint_sec == 4 and col.recording.encoder.rate_control == "vbr"

    col.outputs = [Output(id="t", type="stream", service="twitch", canvas="horizontal", enabled=True, enhanced=True)]
    w = _window(col, tab="Output")
    assert w.output_page.banner.isVisibleTo(w)


def test_output_tab_locked_while_live(app):
    w = _window(_collection(), tab="Output", streaming=True)
    assert not w.output_page.stream_form.isEnabled() and w.output_page.record_form.isEnabled()


def test_startup_options_are_saved(app):
    from sprigcast.app import autostart

    w = _window(_collection())
    assert not w.start_minimized.isChecked()
    assert w.start_with_windows.isEnabled() == autostart.supported()
    w.start_minimized.setChecked(True)
    assert w.apply()
    assert autostart.start_minimized(["sprigcast"])


def test_clip_length_shapes_and_ready_on_open(app):
    col = _collection()
    col.recording.replay_seconds = 40  # an older odd length shows as the nearest choice
    w = _window(col, tab="Recording")
    page = w.output_page
    assert page.clip_length.checkedId() == 45 and col.recording.clip_shapes == "both"
    page.clip_length.button(15).click()
    vertical = next(b for b in page.clip_shapes.buttons() if b.property("shape") == "vertical")
    vertical.click()
    page.clips_on_start.setChecked(True)
    assert w.apply()
    rec = col.recording
    assert (rec.replay_seconds, rec.clip_shapes, rec.clips_on_start) == (15, "vertical", True)
