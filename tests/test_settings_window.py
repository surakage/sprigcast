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


def test_settings_look_titles_pills_and_folded_encoder(app):
    from sprigcast.app.settings_window import ABOUT, TABS

    w = _window(_collection(), tab="Output")
    assert set(ABOUT) == set(TABS) and w.objectName() == "settingsWindow"
    assert [b.text() for b in (w.cancel_button, w.apply_button, w.save_button)] == ["Cancel", "Apply", "Save"]
    form = w.output_page.stream_form
    w.show()
    app.processEvents()
    assert form.rate_control.isVisible() and not hasattr(form, "more")  # everything shows, no fold
    x264 = form.encoder.findData("libx264")
    assert x264 >= 0 and form.encoder.findData("hevc_nvenc") < 0  # streams: H.264 encoders only
    form.encoder.setCurrentIndex(x264)
    app.processEvents()
    assert form.x264_preset.isVisible() and not form.multipass.isVisible()
    form.x264_preset.setCurrentIndex(form.x264_preset.findData("faster"))
    assert form.values().encoder == "libx264" and form.values().x264_preset == "faster"
    assert [w.output_page.tabs.tabText(i) for i in range(w.output_page.tabs.count())] == [
        "Streaming", "Recording", "Clips (replay buffer)"]
    w.close()


def test_video_page_base_and_output_size(app):
    col = _collection()
    w = _window(col, tab="Video")
    out, downscale = w.video_output["horizontal"]
    assert out.itemText(0).startswith("Same as base (1920×1080)")
    out.setCurrentIndex(out.findData(720))
    assert out.currentText() == "1280×720" and downscale.isEnabled()
    downscale.setCurrentIndex(downscale.findData("lanczos"))
    assert w.apply()
    assert col.canvases[0].output_short_side == 720 and col.canvases[0].downscale == "lanczos"
    assert col.canvases[0].output_size() == (1280, 720)


def test_advanced_page_names_remux_and_priority(app):
    from datetime import datetime

    from PySide6.QtCore import QSettings

    from sprigcast.app.settings_window import remux_choice
    from sprigcast.output import naming

    w = _window(_collection(), tab="Advanced")
    assert w.current_tab() == "Advanced" and w.priority.count() > 0
    w.filename.setText("{date} live")
    assert w.filename_example.text().startswith("2026-11-14 live horizontal 19-30-05.mkv")
    w.replay_prefix.setText("Clip")
    assert not w.remux_delete.isEnabled()
    w.remux.setChecked(True)
    w.remux_delete.setChecked(True)
    try:
        assert w.apply()
        assert remux_choice() == (True, True)
        assert naming.recording_name("vertical", datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02 live vertical 03-04-05"
        assert naming.replay_name("vertical", datetime(2026, 1, 2, 3, 4, 5)).startswith("Clip 2026-01-02")
    finally:
        naming.set_patterns()
        for key in ("advanced/filename", "advanced/replay_prefix", "advanced/remux_mp4", "advanced/remux_delete_mkv"):
            QSettings().remove(key)


def test_hotkeys_page_cards_search_and_sound_keys(app):
    from PySide6.QtGui import QKeySequence

    from sprigcast.app.hotkeys import MUTE_ALL, SOUND_PREFIX, TOGGLE_CLIPS, TOGGLE_MODE, sections

    col = _collection()
    titles = [t for t, _ in sections(col)]
    assert titles[:2] == ["Going live", "Clips and screenshots"] and "Sound" in titles and "SprigCast" in titles
    w = _window(col, tab="Hotkeys")
    page = w.hotkeys_page
    for action in (TOGGLE_CLIPS, TOGGLE_MODE, MUTE_ALL, f"{SOUND_PREFIX}mic-1:toggle"):
        assert action in page.edits
    page.search.setText("mic mute")
    shown = [r.action for r in page.rows if not r.isHidden()]
    assert shown and all(a.startswith(SOUND_PREFIX + "mic-1") for a in shown)
    page.edits[f"{SOUND_PREFIX}mic-1:toggle"].setKeySequence(QKeySequence("Ctrl+M"))
    assert page.count.text() == "1 key set"
    assert w.apply() and col.hotkeys == {f"{SOUND_PREFIX}mic-1:toggle": "Ctrl+M"}
