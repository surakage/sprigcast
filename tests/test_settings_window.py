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
    combo = w.audio_page.device_combos["mic-1"]
    combo.setCurrentIndex(combo.findData("USB Mic"))
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


def test_bad_hotkey_blocks_save_in_the_hotkeys_window(app, monkeypatch):
    from PySide6.QtGui import QKeySequence
    from PySide6.QtWidgets import QMessageBox

    from sprigcast.app.hotkeys_dialog import HotkeysWindow

    col = _collection()
    w = HotkeysWindow(col)
    edits = list(w.page.edits.values())
    edits[0].setKeySequence(QKeySequence("Ctrl+Alt+1"))
    edits[1].setKeySequence(QKeySequence("Ctrl+Alt+1"))
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a[2])))
    w.save_button.click()
    assert "used twice" in shown[0] and col.hotkeys == {} and not w.result()
    edits[1].setKeySequence(QKeySequence("Ctrl+C"))  # every app copies with it
    w.save_button.click()
    assert "copy" in shown[1] and col.hotkeys == {}
    edits[1].setKeySequence(QKeySequence("Meta+D"))
    w.save_button.click()
    assert "Windows" in shown[2]
    edits[1].clear()
    w.save_button.click()
    assert len(shown) == 3 and col.hotkeys == {edits_action(w, edits[0]): "Ctrl+Alt+1"}


def edits_action(w, edit):
    return next(a for a, e in w.page.edits.items() if e is edit)


def test_live_disables_stream_and_video(app):
    w = _window(_collection(), streaming=True)
    assert not w.stream_page.isEnabled()
    assert not w.video_fields["horizontal"][1].isEnabled()
    assert w.recording_page.isEnabled()


def test_the_plugin_store_has_its_own_window(app):
    from sprigcast.app.plugin_store_page import PluginStoreWindow
    from sprigcast.plugins import PluginReport

    report = PluginReport(loaded=["countdown_example.py"], failed={"broken.py": "SyntaxError: oops"})
    w = PluginStoreWindow(None, report, lambda: None, _collection())
    mine = w.store.mine.text()  # your own plugins, with the ones that failed to load
    assert "countdown_example.py" in mine and "broken.py" in mine and "SyntaxError" in mine
    assert "slide_show" in w.store.rows  # and the store
    s = _window(_collection(), tab="Plugins")  # an old link lands on General, not on a missing page
    assert s.current_tab() == "General" and "Plugins" not in [s.tabs.item(i).text() for i in range(s.tabs.count())]


def test_stream_quality_and_recording_encoders(app):
    from sprigcast.core.scene import Output

    col = _collection()
    w = _window(col, tab="Output")  # the old name opens Stream → Quality
    assert w.current_tab() == "Stream" and w.stream_tabs.tabText(w.stream_tabs.currentIndex()) == "Quality"
    assert not w.stream_quality.banner.isVisibleTo(w)
    w.expert.setChecked(True)
    f = w.stream_quality.stream_form
    f.rate_control.setCurrentIndex(f.rate_control.findData("cqp"))
    assert f.cq_level.isVisibleTo(f)
    f.preset.setCurrentIndex(f.preset.findData("p7"))
    f.multipass.setCurrentIndex(f.multipass.findData("qres"))
    f.b_frames.setValue(0)
    f.lookahead.setChecked(True)
    f.x264_preset.setCurrentIndex(f.x264_preset.findData("slow"))  # whichever encoder this PC has
    assert f.quality.currentData() == "custom"
    w.recording_tabs.record_form.keyint.setValue(4)
    w.recording_tabs.record_form.audio.setCurrentIndex(w.recording_tabs.record_form.audio.findData(320))
    w.stream_quality.stream_delay.setValue(10)
    assert w.apply()
    enc = col.stream_encoder
    assert (enc.rate_control, enc.preset, enc.multipass, enc.b_frames, enc.lookahead) == ("cqp", "p7", "qres", 0, True)
    assert col.recording.encoder.keyint_sec == 4 and col.recording.encoder.rate_control == "vbr"
    assert col.recording.encoder.audio_kbps == 320
    from sprigcast.app.settings_window import stream_delay

    assert stream_delay() == 10

    col.outputs = [Output(id="t", type="stream", service="twitch", canvas="horizontal", enabled=True, enhanced=True)]
    w = _window(col, tab="Stream/Quality")
    assert w.stream_quality.banner.isVisibleTo(w)


def test_one_quality_choice_hides_the_expert_rows(app):
    from PySide6.QtCore import QSettings

    from sprigcast.app.settings_window import EXPERT

    col = _collection()
    w = _window(col, tab="Stream/Quality")
    w.show()
    app.processEvents()
    f = w.stream_quality.stream_form
    assert not w.expert.isChecked() and f.quality.isVisible() and not f.rate_control.isVisible()
    assert not f.preset.isVisible() and not f.audio.isVisible() and f.quality.currentData() == "balanced"
    f.quality.setCurrentIndex(f.quality.findData("lighter"))
    f.quality.activated.emit(f.quality.currentIndex())
    assert f.values().preset == "p3" and f.values().x264_preset == "superfast"
    w.expert.setChecked(True)
    app.processEvents()
    assert QSettings().value(EXPERT, type=bool) and f.rate_control.isVisible() and f.reset.isVisible()
    assert not w.recording_tabs.names.isHidden()  # pages made later follow the tick too
    f.reset.click()
    assert f.values() == type(f.values())() and f.quality.currentData() == "balanced"
    w.close()


def test_quality_steps_match_make_it_lighter():
    from sprigcast.app.output_page import quality_changes, quality_of
    from sprigcast.core.scene import EncoderSettings
    from sprigcast.output.encoder_load import lighter

    enc = EncoderSettings()
    assert quality_of(enc, "h264_nvenc") == "balanced" and quality_of(enc, "libx264") == "balanced"
    for key, value in lighter(enc, "h264_nvenc").items():
        setattr(enc, key, value)
    assert quality_of(enc, "h264_nvenc") == "lighter"
    best = EncoderSettings(**quality_changes("best"))
    assert quality_of(best, "h264_nvenc") == "best" and quality_of(best, "libx264") == "best"
    assert quality_of(EncoderSettings(preset="p2"), "h264_nvenc") == "custom"


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
    w = _window(col, tab="Clips")
    page = w.recording_tabs
    assert w.current_tab() == "Recording" and page.tabs.currentIndex() == 1
    assert page.clip_length.checkedId() == 45 and col.recording.clip_shapes == "both"
    page.clip_length.button(15).click()
    vertical = next(b for b in page.clip_shapes.buttons() if b.property("shape") == "vertical")
    vertical.click()
    page.clips_on_start.setChecked(True)
    assert w.apply()
    rec = col.recording
    assert (rec.replay_seconds, rec.clip_shapes, rec.clips_on_start) == (15, "vertical", True)


def test_settings_look_titles_pills_and_pages(app):
    from sprigcast.app.settings_window import ABOUT, TABS

    assert TABS == ["General", "Stream", "Recording", "Audio", "Video", "Connections"]
    w = _window(_collection(), tab="Stream/Quality")
    assert set(ABOUT) == set(TABS) and w.objectName() == "settingsWindow"
    assert [b.text() for b in (w.cancel_button, w.apply_button, w.save_button)] == ["Cancel", "Apply", "Save"]
    w.expert.setChecked(True)
    form = w.stream_quality.stream_form
    w.show()
    app.processEvents()
    assert form.rate_control.isVisible()
    x264 = form.encoder.findData("libx264")
    assert x264 >= 0 and form.encoder.findData("hevc_nvenc") < 0  # streams: H.264 encoders only
    form.encoder.setCurrentIndex(x264)
    app.processEvents()
    assert form.x264_preset.isVisible() and not form.multipass.isVisible()
    form.x264_preset.setCurrentIndex(form.x264_preset.findData("faster"))
    assert form.values().encoder == "libx264" and form.values().x264_preset == "faster"
    assert form.quality.currentData() == "better"
    tabs = lambda t: [t.tabText(i) for i in range(t.count())]  # noqa: E731
    assert tabs(w.stream_tabs) == ["Places", "Quality"]
    assert tabs(w.recording_tabs.tabs) == ["Recording", "Clips"]
    assert tabs(w.video_tabs) == ["Canvases", "Virtual camera"]
    assert tabs(w.audio_page.tabs) == ["Devices", "Tracks", "Sounds"]
    assert not hasattr(w, "snap_enabled") and not w.findChildren(type(w.tabs), "settingsColour")
    w.close()


def test_search_jumps_to_the_page_and_turns_expert_on(app):
    w = _window(_collection())
    w.find("stream delay")
    assert w.current_tab() == "Stream" and w.stream_tabs.tabText(w.stream_tabs.currentIndex()) == "Quality"
    w.find("virtual cam")
    assert w.current_tab() == "Video" and w.video_tabs.currentIndex() == 1
    assert not w.expert.isChecked()
    w.find("Downscale filter")  # an expert row: the tick goes on so it's there to find
    assert w.current_tab() == "Video" and w.expert.isChecked() and w.video_tabs.currentIndex() == 0
    w.find("zzqx")  # nothing: stays put
    assert w.current_tab() == "Video"
    from sprigcast.app.settings_window import SEARCH, TABS

    assert all(where.partition("/")[0] in TABS for _l, where, _x in SEARCH)


def test_this_pc_card_priority_sleep_and_preview(app):
    from sprigcast.app import lowend
    from sprigcast.app.settings_window import preview_choice, sleep_policy

    lowend.store_tier("light")
    w = _window(_collection())
    assert w.tier.text() == "Light" and w.optimize_button.isEnabled() and w.priority.count() > 0
    asked = []
    w.optimize_requested.connect(lambda: asked.append(1))
    w.preview_live.setCurrentIndex(w.preview_live.findData("off"))
    w.sleep_sources.setCurrentIndex(w.sleep_sources.findData("all"))
    assert w.apply() and preview_choice() == "off" and sleep_policy() == "all"
    w.optimize_button.click()
    assert asked == [1]
    assert not _window(_collection(), streaming=True).optimize_button.isEnabled()


def test_tier_from_the_pc():
    from sprigcast.app.lowend import PcFacts, tier

    assert tier(PcFacts(4, 8, "libx264")) == "light"
    assert tier(PcFacts(8, 16, "h264_nvenc", cpu_name="Intel(R) Core(TM) i5-4570S CPU")) == "light"  # older CPU
    assert tier(PcFacts(8, 16, "h264_nvenc")) == "balanced"
    assert tier(PcFacts(16, 32, "h264_nvenc")) == "best"


def test_video_page_base_and_output_size(app):
    col = _collection()
    w = _window(col, tab="Video")
    out, downscale = w.video_output["horizontal"]
    w.show()
    app.processEvents()
    assert not out.isVisible()  # an expert row while it's the canvas size
    w.expert.setChecked(True)
    app.processEvents()
    assert out.isVisible() and out.itemText(0).startswith("Same as base (1920×1080)")
    out.setCurrentIndex(out.findData(720))
    assert out.currentText() == "1280×720" and downscale.isEnabled()
    downscale.setCurrentIndex(downscale.findData("lanczos"))
    assert w.apply()
    assert col.canvases[0].output_short_side == 720 and col.canvases[0].downscale == "lanczos"
    assert col.canvases[0].output_size() == (1280, 720)
    w.close()
    w = _window(col, tab="Video")
    w.show()
    app.processEvents()
    assert w.video_output["horizontal"][0].isVisible()  # set to something: shown without expert
    w.close()


def test_file_names_live_with_recording(app):
    from datetime import datetime

    from PySide6.QtCore import QSettings

    from sprigcast.output import naming

    w = _window(_collection(), tab="Advanced")
    assert w.current_tab() == "General"
    names = w.recording_tabs.names
    assert names.isHidden()  # expert
    names.filename.setText("{date} live")
    assert names.filename_example.text().startswith("2026-11-14 live horizontal 19-30-05.mkv")
    names.replay_prefix.setText("Clip")
    try:
        assert w.apply()
        assert naming.recording_name("vertical", datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02 live vertical 03-04-05"
        assert naming.replay_name("vertical", datetime(2026, 1, 2, 3, 4, 5)).startswith("Clip 2026-01-02")
    finally:
        naming.set_patterns()
        for key in ("advanced/filename", "advanced/replay_prefix"):
            QSettings().remove(key)


def test_restart_bar_shows_while_canvas_changes_wait(app):
    w = _window(_collection(), tab="Video")
    w.show()
    app.processEvents()
    assert not w.restart_button.isVisible()
    w.video_fields["horizontal"][1].setValue(1280)
    restarts = []
    w.restart_requested.connect(lambda: restarts.append(1))
    assert w.apply()
    app.processEvents()
    assert w.restart_button.isVisible() and "restart" in w.restart_note.text()
    w.restart_button.click()
    assert restarts == [1]


def test_restart_command_keeps_arguments_but_not_safe_mode():
    import sys

    from sprigcast.app.main import restart_command

    assert restart_command(["SprigCast.exe", "--minimized", "--safe-mode"], frozen=True) == [
        sys.executable, "--minimized"]
    assert restart_command(["run.py", "--safe-mode"], frozen=False) == [sys.executable, "run.py"]


def test_repot_lives_with_the_recording_file_type(app):
    from PySide6.QtCore import QSettings

    from sprigcast.app.recording_dialog import REPOT, REPOT_DELETE, repot_choice

    w = _window(_collection(), tab="Recording")
    page = w.recording_page
    page.container.setCurrentIndex(page.container.findData("mkv"))
    assert page.repot.isEnabled() and not page.repot_delete.isEnabled()
    page.repot.setChecked(True)
    page.repot_delete.setChecked(True)
    page.container.setCurrentIndex(page.container.findData("mp4"))
    assert not page.repot.isEnabled()  # an MP4 recording needs no repotting
    page.container.setCurrentIndex(page.container.findData("mkv"))
    try:
        assert w.apply()
        assert repot_choice() == (True, True)
    finally:
        for key in (REPOT, REPOT_DELETE):
            QSettings().remove(key)


def test_hotkeys_page_cards_search_and_sound_keys(app):
    from PySide6.QtGui import QKeySequence

    from sprigcast.app.hotkeys import MUTE_ALL, SOUND_PREFIX, TOGGLE_CLIPS, TOGGLE_MODE, sections

    col = _collection()
    titles = [t for t, _ in sections(col)]
    assert titles[:2] == ["Going live", "Clips and screenshots"] and "Sound" in titles and "SprigCast" in titles
    from sprigcast.app.hotkeys_dialog import HotkeysWindow

    w = HotkeysWindow(col)
    page = w.page
    for action in (TOGGLE_CLIPS, TOGGLE_MODE, MUTE_ALL, f"{SOUND_PREFIX}mic-1:toggle"):
        assert action in page.edits
    page.search.setText("mic mute")
    shown = [r.action for r in page.rows if not r.isHidden()]
    assert shown and all(a.startswith(SOUND_PREFIX + "mic-1") for a in shown)
    page.edits[f"{SOUND_PREFIX}mic-1:toggle"].setKeySequence(QKeySequence("Ctrl+M"))
    assert page.count.text() == "1 key set"
    w.save_button.click()
    assert w.result() and col.hotkeys == {f"{SOUND_PREFIX}mic-1:toggle": "Ctrl+M"}


def test_virtual_camera_tab(app, monkeypatch):
    from sprigcast.app.output_page import camera_wanted
    from sprigcast.output import virtualcam

    # Pretend this PC can't have the camera, whatever it really is (CI, Windows 10, a Windows 11 dev copy).
    monkeypatch.setattr(virtualcam, "supported", lambda: (False, "The SprigCast camera needs Windows 11."))
    w = _window(_collection(), tab="Camera")
    tab = w.camera_tab
    assert w.current_tab() == "Video" and w.video_tabs.tabText(w.video_tabs.currentIndex()) == "Virtual camera"
    assert "Windows 11" in tab.status.text() and not tab.setup.isVisible() and not tab.on.isEnabled()
    tab.on.setEnabled(True)
    tab.on.setChecked(True)
    assert w.apply() and camera_wanted()


def test_only_the_shown_page_is_made_before_the_window_opens(app, monkeypatch):
    """Making every page before showing anything held the window 2.4 s on the PC (2026-11-16)."""
    from sprigcast.app import settings_window
    from sprigcast.app.settings_window import TABS

    made = []
    real = settings_window.SettingsWindow.ensure
    monkeypatch.setattr(settings_window.SettingsWindow, "ensure",
                        lambda self, name: (name not in self._built and made.append((self, name)), real(self, name))[1])
    w = _window(_collection(), tab="Audio/Sounds")
    made[:] = [name for who, name in made if who is w]
    assert made == ["Audio"] and w.current_tab() == "Audio"
    ducked = []
    w.on_ducking = ducked.append  # set after the page was made: still reaches it
    w.audio_page.ducking_button.click()
    assert ducked == [w.audio_page]
    for _ in range(len(TABS) * 3):  # then the rest, one per turn of the event loop
        app.processEvents()
    made[:] = [entry if isinstance(entry, str) else entry[1] for entry in made
               if isinstance(entry, str) or entry[0] is w]
    assert sorted(made) == sorted(TABS) and w.stack.count() == len(TABS)
    w.tabs.setCurrentRow(TABS.index("Connections"))
    assert w.stack.currentIndex() == TABS.index("Connections")
    from sprigcast.app.connections_page import ConnectionsPage

    assert w.stack.currentWidget().findChild(ConnectionsPage) is not None


def test_apply_and_attributes_make_pages_not_made_yet(app):
    from sprigcast.app.settings_window import TABS

    w = _window(_collection())
    assert w._built == {"General"}
    assert w.stream_page is not None and w._built == {"General", "Stream"}  # only the page asked for
    assert w.apply() and w._built == set(TABS)


def test_the_audio_page_waits_for_its_device_scan_on_a_worker(app):
    import threading
    from concurrent.futures import Future

    from sprigcast.app.settings_window import TABS

    scan: Future = Future()
    w = _window(_collection(), audio_devices=scan)
    for _ in range(len(TABS) * 3):
        app.processEvents()
    assert "Audio" not in w._built and len(w._built) == len(TABS) - 1  # the rest are made meanwhile
    threading.Timer(0.05, lambda: scan.set_result({"audio_input": ["USB Mic"], "audio_output": []})).start()
    w.tabs.setCurrentRow(TABS.index("Audio"))  # clicked before the scan is done: waits, the window drawing
    assert "Audio" in w._built and w.current_tab() == "Audio"
