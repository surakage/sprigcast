import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.core.scene import Canvas, SceneCollection, SourceConfig  # noqa: E402
from sprigcast.app.settings_window import TABS, SettingsWindow  # noqa: E402


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
    fps.buttons[30].click()
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
    assert not w.expert.isChecked() and f.quick.isVisible() and not f.quality.isVisible()
    assert not f.rate_control.isVisible()
    assert not f.preset.isVisible() and not f.audio.isVisible() and f.quality.currentData() == "balanced"
    assert f.quick.value() == "balanced" and f.chip.text().startswith(("GPU", "CPU"))
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

    assert TABS == ["General", "Appearance", "Stream", "Audio", "Video", "Recording", "Connections"]
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
    assert tabs(w.audio_page.tabs) == ["Devices", "Sounds", "Tracks"]
    assert not w.findChildren(type(w.tabs), "settingsColour")
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
    assert out.isVisible() and out.itemText(0).startswith("Same as the canvas (1920×1080)")
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
    for _ in range(len(TABS) * 3):  # the others wait for their tab: making them all held SprigCast up while live
        app.processEvents()
    made[:] = [entry if isinstance(entry, str) else entry[1] for entry in made
               if isinstance(entry, str) or entry[0] is w]
    assert made == ["Audio"] and w._built == {"Audio"}
    w.tabs.setCurrentRow(TABS.index("Connections"))
    assert w.stack.currentIndex() == TABS.index("Connections")
    from sprigcast.app.connections_page import ConnectionsPage

    assert w.stack.currentWidget().findChild(ConnectionsPage) is not None


def test_apply_and_attributes_make_pages_not_made_yet(app):
    from sprigcast.app.settings_window import TABS

    w = _window(_collection())
    assert w._built == {"General"}
    assert w.stream_page is not None and w._built == {"General", "Stream"}  # only the page asked for
    assert w.apply() and w._built == {"General", "Stream"}  # Save writes the pages made, makes no others
    assert w.confirm_stream is not None and w._built == {"General", "Stream"}  # General's own: nothing more made


def test_the_audio_page_waits_for_its_device_scan_on_a_worker(app):
    import threading
    from concurrent.futures import Future

    from sprigcast.app.settings_window import TABS

    scan: Future = Future()
    w = _window(_collection(), audio_devices=scan)
    for _ in range(len(TABS) * 3):
        app.processEvents()
    assert w._built == {"General"}  # nothing else is made meanwhile
    threading.Timer(0.05, lambda: scan.set_result({"audio_input": ["USB Mic"], "audio_output": []})).start()
    w.tabs.setCurrentRow(TABS.index("Audio"))  # clicked before the scan is done: waits, the window drawing
    assert "Audio" in w._built and w.current_tab() == "Audio"


def test_general_ends_with_clear_all_my_data(app):
    w = _window(_collection())
    asked = []
    w.on_clear_data = asked.append
    w.clear_data_button.click()
    assert asked == [w] and w.clear_data_button.text() == "Clear all my data…"


def test_save_leaves_pages_never_opened_as_they_were(app):
    """Only General was opened: Save writes General and leaves the canvases, places and sound untouched."""
    from sprigcast.app.settings_window import CONFIRM_STREAM

    col = _collection()
    before = [(c.width, c.height, c.fps, c.enabled) for c in col.canvases]
    w = _window(col)
    w.confirm_stream.setChecked(not w.confirm_stream.isChecked())
    assert w.apply() and w._built == {"General"}
    assert QSettings().value(CONFIRM_STREAM, type=bool) == w.confirm_stream.isChecked()
    assert [(c.width, c.height, c.fps, c.enabled) for c in col.canvases] == before


def test_the_window_shows_first_and_its_page_fills_in_just_after(app):
    """PC 2026-11-18: clicking a destination took 352 ms to show anything (the Stream page made before the window).
    Opened from the app, the window is up at once and the page is made a moment later; asks for the page
    (pick this place) wait for it."""
    import time

    col = _collection()
    w = _window(col, tab="Stream", fill_after_show=True)
    picked = []
    w.soon(lambda: picked.append(w.current_tab()))
    assert "Stream" not in w._built and w.current_tab() == "Stream" and not picked
    w.show()
    deadline = time.monotonic() + 3
    while "Stream" not in w._built and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert "Stream" in w._built and picked == ["Stream"]
    w.soon(lambda: picked.append("now"))  # made already: straight away
    assert picked[-1] == "now"
    w.close()


def test_a_window_closed_before_its_page_fills_makes_nothing(app):
    w = _window(_collection(), tab="Audio", fill_after_show=True)
    w.show()
    w.reject()
    w._fill()
    assert not w._built


def test_a_page_picked_before_the_window_fills_in_stays_picked(app):
    """PC 2026-11-18 (blooms switching every 15 s): the first Settings window wouldn't switch pages. A page picked
    before the window had filled in its first page was taken back to General when that fill came."""
    import time

    w = _window(_collection(), tab="General", fill_after_show=True)
    w.show()
    w.tabs.setCurrentRow(TABS.index("Connections"))  # before the fill's timer
    deadline = time.monotonic() + 3
    while (w._fill_tab is not None or w._slices) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert w.current_tab() == "Connections" and w.stack.currentIndex() == TABS.index("Connections")
    assert not w._stand_in(TABS.index("Connections"))
    w.close()


def test_a_picked_page_waiting_on_held_back_timers_is_put_in_anyway(app, caplog):
    """Its parts wait on timers a busy UI can hold back (or a part can fail): the picked page still shows."""
    w = _window(_collection(), tab="General")
    w.show()
    app.processEvents()
    w._slice(lambda: 1 / 0)  # a part that fails never stops the parts after it
    w._flush_slices()
    w.ensure("Connections")  # made, waiting to be put in
    w._slices.insert(0, w._slices.pop())  # (as a slice that never got its tick)
    w.tabs.setCurrentRow(TABS.index("Connections"))
    assert w._stand_in(TABS.index("Connections"))
    w._make_sure_shown(TABS.index("Connections"))
    assert not w._stand_in(TABS.index("Connections")) and w.stack.currentIndex() == TABS.index("Connections")
    w.close()


def test_the_gear_opens_settings_while_the_warm_up_one_is_off_screen(app, tmp_path, monkeypatch):
    """The off-screen warm-up Settings counted as "already open": a gear click in its 2 s did nothing."""
    from PySide6.QtCore import Qt

    from sprigcast.app import main_window as mw

    shown = []
    monkeypatch.setattr(mw, "modal", lambda window: shown.append(window))
    window = mw.MainWindow(_collection(), tmp_path / "scenes.json")
    try:
        window.show()
        window.warm_settings()
        warm = [x for x in app.topLevelWidgets() if isinstance(x, SettingsWindow) and x.isVisible()]
        assert warm and warm[0].testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window._open_settings()
        assert len(shown) == 1 and shown[0] is not warm[0]
    finally:
        for x in shown:
            x.deleteLater()
        window.close()


def test_hotkey_cards_are_made_a_tick_apart_and_saving_early_keeps_them_all(app):
    """Made all at once (each row with a style sheet of its own) the list held the window 427 ms on the PC."""
    import time

    from PySide6.QtGui import QKeySequence

    from sprigcast.app.hotkeys import sections
    from sprigcast.app.hotkeys_dialog import HotkeysPage, HotkeysWindow

    col = _collection()
    total = len(sections(col))
    assert total > 2
    page = HotkeysPage(col, cards_later=True)
    assert len(page.cards) == 1 and page.styleSheet() and not page.rows[0].edit.styleSheet()
    done = []
    page.on_done = lambda: done.append(len(page.cards))
    end = time.monotonic() + 5
    while not done and time.monotonic() < end:
        app.processEvents()
    assert done == [total]

    w = HotkeysWindow(col, fill_after_show=True)
    w._fill()
    assert len(w.page.cards) == 1
    first = next(iter(w.page.edits))
    w.page.edits[first].setKeySequence(QKeySequence("Ctrl+Alt+9"))
    w.save_button.click()  # before the other cards are made: they're made then, and nothing is lost
    assert len(w.page.cards) == total and w.result() and col.hotkeys == {first: "Ctrl+Alt+9"}


def test_plugin_store_window_shows_first_and_fills_after(app):
    """Made before it showed, the store held the window 94 ms on the PC while live."""
    from sprigcast.app.plugin_store_page import PluginStoreWindow
    from sprigcast.plugins import PluginReport

    w = PluginStoreWindow(None, PluginReport(), None, _collection(), fill_after_show=True)
    assert "store" not in w.__dict__
    seen = []
    w.changed.connect(lambda: seen.append(1))
    w._fill()
    w.store.changed.emit()
    assert seen == [1] and "slide_show" in w.store.rows


def test_plugin_store_reads_its_files_off_the_window_thread(app, monkeypatch):
    """A file read per plugin on the window's thread was 113-167 ms of the open on the PC while live."""
    import threading
    import time

    from sprigcast import plugin_store
    from sprigcast.app.plugin_store_page import PluginStoreWindow
    from sprigcast.plugins import PluginReport

    threads = []
    real = plugin_store.installed_version
    monkeypatch.setattr(plugin_store, "installed_version",
                        lambda d, pid: threads.append(threading.current_thread()) or real(d, pid))
    w = PluginStoreWindow(None, PluginReport(), None, _collection(), fill_after_show=True)
    w.show()
    end = time.monotonic() + 5
    while "store" not in w.__dict__ and time.monotonic() < end:
        app.processEvents()
    w.store.finish()
    assert "slide_show" in w.store.rows and w.store.rows["slide_show"].button.text() == "Add"
    assert threads and threading.main_thread() not in threads
    w.close()


def test_the_recording_page_shows_its_boxes(app):
    """Its tabs were taken out and put back in scroll areas, and taking a tab out hides its page: the Recording
    page was blank (found going over Settings, 2026-11-19)."""
    w = _window(_collection(), tab="Recording")
    w.resize(1000, 760)
    w.show()
    w._flush_slices()
    app.processEvents()
    page = w.recording_tabs.tabs.widget(0).widget()  # the scroll area's holder
    inner = page.layout().itemAt(0).widget()
    assert not inner.isHidden() and inner.isVisible() and inner.height() > 100
    assert w.recording_tabs.same_as_stream.isVisible()
    w.close()


def test_sections_are_titled_cards_with_a_leaf_and_fields_with_an_edge(app):
    from sprigcast.app import theme
    from sprigcast.app.settings_window import window_style

    style = window_style()
    assert theme.SECTION in style and theme.SECTION_EDGE in style and theme.FIELD_EDGE in style
    assert theme.LABEL in style and f"font-size: {theme.SECTION_TITLE_PX}px" in style
    assert theme.nature_pictures()["section_leaf"] in style


def test_form_labels_sit_level_with_their_boxes(app):
    from PySide6.QtWidgets import QFormLayout, QPlainTextEdit

    w = _window(_collection(), tab="General")
    w.resize(1000, 760)
    w.show()
    w.ensure_all()
    app.processEvents()
    checked = 0
    for form in w.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            label = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            if label is None or field is None or not label.widget() or not label.widget().isVisible():
                continue
            box = field.geometry()
            if box.height() > 48 or isinstance(field.widget(), QPlainTextEdit):
                continue
            middle = label.widget().geometry().center().y()
            assert abs(middle - box.center().y()) <= 2, label.widget().text()
            checked += 1
    assert checked >= 5
    w.close()


def test_recording_ticks_line_up_with_the_boxes(app):
    w = _window(_collection(), tab="Recording")
    w.resize(1000, 760)
    w.show()
    w.ensure_all()
    app.processEvents()
    from PySide6.QtWidgets import QCheckBox, QComboBox

    tick = next(t for t in w.findChildren(QCheckBox) if t.text() == "Same as stream while live")
    split = next(c for c in w.findChildren(QComboBox) if c.toolTip().startswith("A new file starts"))
    assert abs(tick.mapTo(w, tick.rect().topLeft()).x() - split.mapTo(w, split.rect().topLeft()).x()) <= 2
    w.close()


def test_accounts_is_gone_and_old_links_land_on_stream_places(app):
    w = _window(_collection(), tab="Accounts")
    w.show()
    w.ensure_all()
    app.processEvents()
    assert "Accounts" not in TABS and w.current_tab() == "Stream"
    assert w.stream_tabs.tabText(w.stream_tabs.currentIndex()) == "Places"
    w.close()


def test_quality_tab_quick_picks_places_and_delay(app):
    from sprigcast.core.scene import Output

    col = _collection()
    col.outputs = [Output(id="t", type="stream", service="twitch", canvas="horizontal", enabled=True,
                          bitrate_kbps=6000, auto_bitrate=True),
                   Output(id="y", type="stream", service="youtube", canvas="vertical", enabled=True, bitrate_kbps=4500,
                          auto_bitrate=False),
                   Output(id="k", type="stream", service="kick", canvas="horizontal", enabled=False)]
    w = _window(col, tab="Stream/Quality")
    w.show()
    app.processEvents()
    q = w.stream_quality
    f = q.stream_form
    f.quick.buttons["lighter"].click()  # Faster
    assert f.quality.currentData() == "lighter" and f.values().preset == "p3"
    texts = [q.places_grid.itemAt(i).widget().text() for i in range(q.places_grid.count())]
    assert "Twitch" in texts and "YouTube" in texts and "Kick" not in texts and "6,000 kbps" in texts
    assert "Vertical 1080×1920" in texts and "On" in texts and "Off" in texts
    assert "10.8 Mbps" in q.upload.text()  # 6000 + 4500 + 2 × 160 audio
    q.audio_quick.buttons[192].click()
    q.delay_quick.buttons[30].click()
    assert not f.load_row.isVisibleTo(f)  # no reading off air
    w.stream_page.outputs[1].enabled = False  # an unsaved change on Places shows on Quality
    w.stream_tabs.setCurrentIndex(0)
    w.stream_tabs.setCurrentIndex(1)
    texts = [q.places_grid.itemAt(i).widget().text() for i in range(q.places_grid.count())]
    assert "YouTube" not in texts
    q.change_places.click()
    assert w.stream_tabs.currentIndex() == 0
    assert w.apply()
    from sprigcast.app.settings_window import stream_delay

    assert col.stream_encoder.audio_kbps == 192 and stream_delay() == 30
    f.set_room(40.0, True, "Encoder 40% busy · room for about 2 more")
    assert f.load_row.isVisibleTo(f) and f.load_bar.value() == 40
    w.close()


def test_fine_tuning_is_its_own_card_two_to_a_row_with_expert(app):
    w = _window(_collection(), tab="Stream/Quality")
    w.show()
    app.processEvents()
    f = w.stream_quality.stream_form
    assert f.fine.title() == "Fine tuning" and not f.fine.isVisible()
    w.expert.setChecked(True)
    app.processEvents()
    assert f.fine.isVisible() and f.rate_control.parentWidget() is f.fine
    grid = f._fine_grid
    columns = {grid.getItemPosition(grid.indexOf(f.rate_control))[1], grid.getItemPosition(grid.indexOf(f.keyint))[1]}
    assert columns == {1, 3}  # side by side
    assert not f.audio.isVisible()  # Stream's audio bitrate is in its Sound card
    assert w.recording_tabs.record_form.fine.title() == "Fine tuning"
    w.close()


def test_general_going_live_snapping_popouts_and_privacy_are_saved(app):
    from PySide6.QtCore import QSettings

    from sprigcast.app import privacy, projector
    from sprigcast.app.preview import snap_settings
    from sprigcast.app.settings_window import CONFIRM_GO_LIVE, KEEP_RECORDING, REPLAY_ON_LIVE

    w = _window(_collection(), tab="General")
    w.ensure_all()
    from PySide6.QtWidgets import QGroupBox

    titles = [b.title() for b in w.findChildren(QGroupBox)]
    for title in ("Going live", "Snapping", "Pop-out views", "Privacy"):
        assert title in titles
    assert not w.confirm_go_live.isChecked() and w.snap_on.isChecked() and w.projector_hide_pointer.isChecked()
    w.confirm_go_live.setChecked(True)
    w.keep_recording.setChecked(True)
    w.replay_on_live.setChecked(True)
    w.snap_distance.setCurrentIndex(w.snap_distance.findData(20.0))
    w.snap_to["center"].setChecked(False)
    w.snap_on.setChecked(False)
    assert not w.snap_distance.isEnabled()
    w.projector_on_top.setChecked(True)
    w.projector_hide_pointer.setChecked(False)
    w.hide_from_capture.setChecked(True)
    assert w.apply()
    q = QSettings()
    assert q.value(CONFIRM_GO_LIVE, type=bool) and q.value(KEEP_RECORDING, type=bool) and q.value(REPLAY_ON_LIVE, type=bool)
    snap = snap_settings()
    assert not snap["enabled"] and snap["distance"] == 20.0 and not snap["center"] and snap["edges"]
    assert projector.on_top() and not projector.hide_pointer() and privacy.hidden()
    w.close()


def test_privacy_does_nothing_off_windows(app, monkeypatch):
    import sys

    from sprigcast.app import privacy

    monkeypatch.setattr(sys, "platform", "linux")
    assert not privacy.supported() and not privacy.apply_to(object(), True)


def test_appearance_page_saves_text_size_and_colors_and_asks_for_a_restart(app):
    from PySide6.QtCore import QSettings

    from sprigcast.app import theme
    from sprigcast.app.settings_window import appearance_waiting

    w = _window(_collection(), tab="Appearance")
    w.show()
    app.processEvents()
    assert w.current_tab() == "Appearance" and w.text_size.value() == "normal" and w.color_preset.value() == "default"
    assert not appearance_waiting() and not w.restart_note.isVisible()
    w.color_preset.buttons["colorblind"].click()
    assert w.color_preview.colors["ACCENT"] == theme.PRESET_COLORS["colorblind"]["ACCENT"]
    w.text_size.buttons["large"].click()
    assert w.apply()
    q = QSettings()
    assert q.value(theme.COLORS_KEY) == "colorblind" and q.value(theme.TEXT_SIZE_KEY) == "large"
    assert theme.chosen_colors() == "colorblind" and theme.colors_in_use == "default"  # drawn after a restart
    assert w.restart_note.isVisible() and "restart" in w.restart_note.text()
    w.close()


def test_color_presets_only_change_known_theme_names():
    from sprigcast.app import theme

    for name, changes in theme.PRESET_COLORS.items():
        assert name in theme.COLOR_PRESETS
        for key, value in changes.items():
            assert hasattr(theme, key) and value.startswith("#") and len(value) == 7
    assert set(theme.preset_colors("contrast")) == {"ACCENT", "WARN", "BAD"}


def test_quality_made_but_not_yet_in_its_tab_goes_with_the_window(app):
    """Full test runs, 2026-11-19: the warm-up Settings went between Quality being made and being put in its tab,
    and Quality stayed behind as a window of its own."""
    w = _window(_collection(), tab="Stream")
    w.show()
    app.processEvents()
    while w._slices and "stream_quality" not in w.__dict__:
        w._slices.pop(0)()  # the part that makes Quality; putting it in its tab still waits
    quality = w.stream_quality
    assert quality.parentWidget() is not None and quality.window() is w and not quality.isWindow()
    w.close()


def test_canvas_cards_pick_a_size_and_a_frame_rate(app):
    """Settings redesign (2026-11-19): a card per canvas with its shape, a size list and 30 / 60 quick picks."""
    col = _collection()
    w = _window(col, tab="Video")
    w.show()
    app.processEvents()
    enabled, width, height, fps = w.video_fields["horizontal"]
    size, (big, small) = w.video_sizes["horizontal"], w.video_heads["horizontal"]
    assert size.currentText() == "1920×1080 (1080p)" and not width.isVisible()  # the boxes are for Custom
    assert big.text() == "1920×1080" and small.text().startswith("Landscape")
    assert [f for f, b in fps.buttons.items() if not b.isHidden()] == sorted({30, 60, fps.value()})
    size.setCurrentIndex(size.findText("1280×720 (720p)"))
    assert (width.value(), height.value()) == (1280, 720) and big.text() == "1280×720"
    size.setCurrentIndex(size.findText("Custom"))
    app.processEvents()
    assert width.isVisible()
    width.setValue(1000)
    assert big.text() == "1000×720" and size.currentText() == "Custom"  # typing never jumps the list about
    fps.buttons[30].click()
    assert small.text().endswith("30 fps") and fps.currentData() == 30
    other = w.video_other_fps["horizontal"]
    assert not other.isVisible()  # the rarer frame rates are for experts
    w.expert.setChecked(True)
    app.processEvents()
    assert other.isVisible()
    other.setCurrentIndex(other.findData(50))
    assert fps.currentData() == 50 and not fps.buttons[50].isHidden() and other.currentIndex() == 0
    assert small.text().endswith("50 fps")
    assert w.video_sizes["vertical"].currentText() == "1080×1920 (1080p)"
    assert w.video_heads["vertical"][1].text().startswith("Portrait")
    w.close()


def test_a_canvas_size_of_your_own_opens_on_custom(app):
    col = _collection()
    col.canvases[0].width, col.canvases[0].height = 1600, 900
    w = _window(col, tab="Video")
    w.show()
    app.processEvents()
    assert w.video_sizes["horizontal"].currentText() == "Custom" and w.video_fields["horizontal"][1].isVisible()
    assert w.apply() and (col.canvases[0].width, col.canvases[0].height) == (1600, 900)
    w.close()


def test_one_scaling_for_every_canvas_only_changes_them_when_it_is_changed(app):
    col = _collection()
    col.canvases[0].downscale, col.canvases[1].downscale = "bicubic", "lanczos"
    w = _window(col, tab="Video")
    w.show()
    app.processEvents()
    assert not w.sent_out.isVisible()  # Sent out at is for experts while everything goes out at canvas size
    assert w.apply()
    assert [c.downscale for c in col.canvases[:2]] == ["bicubic", "lanczos"]  # untouched: each keeps its own
    w.expert.setChecked(True)
    app.processEvents()
    assert w.sent_out.isVisible() and w.sent_out.badge.text() == "Expert"
    w.scaling.setCurrentIndex(w.scaling.findData("bilinear"))
    assert w.apply()
    assert [c.downscale for c in col.canvases[:2]] == ["bilinear", "bilinear"]
    w.close()


def test_themes_turn_the_hue_and_keep_everything_as_readable():
    from sprigcast.app import theme

    canopy = theme.palette("slothbloom")
    assert canopy["ACCENT"] == "#4fd88f" and canopy["BG"] == "#0e1311"  # Night Canopy is untouched
    pairs = [("TEXT", "BG"), ("ACCENT_TEXT", "ACCENT"), ("BUTTON_EDGE", "PANEL"), ("LABEL", "SECTION"),
             ("FIELD_EDGE", "SECTION"), ("SELECT_TEXT", "SELECT_BG"), ("MUTED", "PANEL")]
    for name in theme.THEME_LOOKS:
        assert name in theme.THEMES and name in theme.THEME_ABOUT
        colors = theme.palette(name)
        assert set(colors) == set(canopy)
        for kept in ("LIVE", "LIVE_HOVER", "LIVE_EDGE", "WARN", "BAD"):  # pink is for Live; warnings never move
            assert colors[kept] == canopy[kept], (name, kept)
        for a, b in pairs:
            assert abs(theme.contrast(colors[a], colors[b]) - theme.contrast(canopy[a], canopy[b])) < 0.25, (name, a, b)
    assert theme.palette("moonlit")["ACCENT"] != canopy["ACCENT"] and theme.palette("stone")["ACCENT"] == canopy["ACCENT"]
    assert theme.palette("moonlit", "colorblind")["ACCENT"] == theme.PRESET_COLORS["colorblind"]["ACCENT"]


def test_appearance_picks_a_theme_from_its_tile_and_previews_it(app):
    from PySide6.QtCore import QSettings

    from sprigcast.app import theme
    from sprigcast.app.settings_window import appearance_waiting

    w = _window(_collection(), tab="Appearance")
    w.show()
    app.processEvents()
    assert set(w.theme_tiles) == set(theme.THEMES) and w.theme_tiles["slothbloom"].isChecked()
    assert not w.theme.isVisibleTo(w)  # the tiles are how you pick
    w.theme_tiles["orchid"].click()
    assert w.theme.currentData() == "orchid" and w.color_preview.theme_name == "orchid"
    assert w.color_preview.colors["ACCENT"] == theme.palette("orchid")["ACCENT"]
    w.text_size.buttons["large"].click()
    assert w.color_preview.text_size == "large"
    w.color_preview.grab()  # paints
    w.theme_tiles["system"].grab()
    assert w.apply()
    assert QSettings().value(theme.THEME_KEY) == "orchid" and theme.theme_in_use == "slothbloom"
    assert appearance_waiting() and w.restart_note.isVisible() and "Theme" in w.restart_note.text()
    w.close()
