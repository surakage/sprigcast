"""The Settings review (2026-11-17): what moved out of Settings, settings files, Preview while live, the
first overload's one-tap note, thread names in the memory line and the cap on Chromium page processes."""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.core.scene import Canvas, RecordingSettings, SceneCollection  # noqa: E402


@pytest.fixture
def app(monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("SettingsReviewTest")
    QSettings().clear()
    yield app
    QSettings().clear()


def _collection():
    return SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)], sources=[], scenes=[])


def test_settings_file_travels_without_secrets(app, tmp_path):
    from sprigcast.app import settings_file

    q = QSettings()
    q.setValue("general/theme", "light")
    q.setValue("control/password", "hunter2")  # stays on this PC
    q.setValue("control/obs_door", True)
    q.setValue("general/pc_tier", "best")  # this PC's, not the other one's
    q.setValue("window/geometry", "x")
    col = _collection()
    col.stream_encoder.preset = "p3"
    col.recording.folder = "D:/Mine"
    col.recording.split_minutes = 60
    col.hotkeys = {"stream.toggle": "Ctrl+Alt+S"}
    path = tmp_path / "settings.json"
    settings_file.export_settings(col, path)
    text = path.read_text()
    assert "hunter2" not in text and "D:/Mine" not in text and "pc_tier" not in text and "geometry" not in text
    data = json.loads(text)
    assert data["format"] == "sprigcast-settings" and data["options"]["general/theme"] == "light"

    q.clear()
    other = _collection()
    other.recording.folder = "E:/Theirs"
    done = settings_file.import_settings(other, path)
    assert done == ["Stream encoder", "Recording and clips", "Hotkeys", "App options"]
    assert other.stream_encoder.preset == "p3" and other.recording.split_minutes == 60
    assert other.recording.folder == "E:/Theirs" and other.hotkeys == {"stream.toggle": "Ctrl+Alt+S"}
    assert QSettings().value("general/theme") == "light" and QSettings().value("control/password") is None

    bad = tmp_path / "bad.json"
    bad.write_text('{"hello": 1}')
    with pytest.raises(ValueError, match="isn't a SprigCast settings file"):
        settings_file.import_settings(other, bad)
    bad.write_text("not json")
    with pytest.raises(ValueError, match="Can't read"):
        settings_file.import_settings(other, bad)


def test_same_as_stream_is_on_for_new_setups():
    assert RecordingSettings().same_as_stream


def test_thread_names_group_the_numbered_ones():
    from sprigcast.core.pacing import thread_names

    class T:
        def __init__(self, name):
            self.name = name

    names = thread_names([T("MainThread"), T("sprigcast-worker-1"), T("sprigcast-worker-2"), T("Thread-3 (run)"),
                          T("Thread-4 (run)"), T("sprigcast-audio")])
    assert names == "MainThread, Thread (run) ×2, sprigcast-audio, sprigcast-worker ×2"
    assert "python threads" in __import__("sprigcast.core.pacing", fromlist=["x"]).memory_line()


def test_chromium_gets_a_cap_on_page_processes():
    from sprigcast.app.browser_host import chromium_flags

    auto = "--autoplay-policy=no-user-gesture-required --enable-precise-memory-info"
    assert chromium_flags("") == f"{auto} --renderer-process-limit=2"
    assert chromium_flags("--enable-gpu") == f"--enable-gpu {auto} --renderer-process-limit=2"
    assert chromium_flags("--renderer-process-limit=5") == f"--renderer-process-limit=5 {auto}"  # set by hand: kept


@pytest.fixture
def window(app, tmp_path):
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_collection(), tmp_path / "scenes.json")
    yield w
    w.close()


def test_hotkeys_and_plugins_open_their_own_windows(window, monkeypatch):
    opened = []
    monkeypatch.setattr(window, "open_hotkeys", lambda: opened.append("hotkeys"))
    monkeypatch.setattr(window, "open_plugin_store", lambda: opened.append("plugins"))
    window._open_settings("Hotkeys")
    window._open_settings("Plugins")
    assert opened == ["hotkeys", "plugins"]
    tools = next(m for m in window.menuBar().findChildren(type(window.menuBar().addMenu("x")))
                 if m.title() == "&Tools")
    assert {"Hotkeys…", "Plugin store…"} <= {a.text() for a in tools.actions()}


def test_snapping_lives_in_builds_right_click(window):
    window._set_mode("build")
    menu = window._preview_menu("horizontal", -1)
    snap = next(a.menu() for a in menu.actions() if a.text() == "Snapping")
    texts = [a.text() for a in snap.actions()]
    assert texts[:1] == ["Snap"] and "To the center" in texts and "Sensitivity" in texts
    edges = next(a for a in snap.actions() if a.text() == "To canvas edges")
    edges.setChecked(False)
    from sprigcast.app.preview import snap_settings

    assert snap_settings()["edges"] is False
    reach = next(a.menu() for a in snap.actions() if a.text() == "Sensitivity")
    next(a for a in reach.actions() if a.text() == "20 px").trigger()
    assert snap_settings()["distance"] == 20.0
    menu.deleteLater()


def test_preview_while_live(window, app, monkeypatch):
    from sprigcast.app.settings_window import PREVIEW_LIVE

    window.show()
    app.processEvents()
    window._poll_engine()
    assert window._preview_mode() == "smooth" and window.engine.preview_fps == 30
    QSettings().setValue(PREVIEW_LIVE, "light")
    window._after_settings()
    assert window._preview_mode() == "smooth"  # nothing on air: always smooth
    window.recording = object()
    try:
        assert window._preview_mode() == "light"
    finally:
        window.recording = None
    mode = ["light"]
    monkeypatch.setattr(window, "_preview_mode", lambda: mode[0])
    window._poll_engine()
    assert window.engine.preview_fps == 15 and not window.previews["horizontal"].paused
    mode[0] = "off"
    window._poll_engine()
    assert window.previews["horizontal"].paused and "horizontal" not in window.engine.previews
    mode[0] = "smooth"
    window._poll_engine()
    assert not window.previews["horizontal"].paused and window.engine.preview_fps == 30


def test_the_first_overload_offers_the_fix_once(window, monkeypatch):
    notes = []
    monkeypatch.setattr(window, "_saved_note", lambda title, *a, **k: notes.append((title, k)))
    monkeypatch.setattr(window, "_make_encoding_lighter", lambda: "Lighter settings saved.")
    window._offer_lighter(["stream"])
    window._offer_lighter(["recording"])
    assert len(notes) == 1 and "stream" in notes[0][0] and notes[0][1]["link"] == "Make it lighter"
    notes[0][1]["on_click"]()
    assert notes[-1][0] == "Lighter settings saved."


def test_restart_now_waits_for_the_stream(window, app):
    window.streaming = object()
    try:
        window.restart_app()
        assert not app.property("sprigcast_restart")
    finally:
        window.streaming = None
    window.restart_app()
    assert app.property("sprigcast_restart")
    app.setProperty("sprigcast_restart", False)


def test_the_camera_tab_lists_speakers_off_the_window_thread(app, monkeypatch):
    """Waiting for PortAudio (busy with Settings' device scan) froze the window 300 ms on the PC."""
    import threading
    import time

    from sprigcast.app.output_page import CameraTab
    from sprigcast.sources import audio

    asked_on = []
    monkeypatch.setattr(audio, "list_playback_devices",
                        lambda rescan=False: asked_on.append(threading.current_thread()) or ["CABLE Input"])
    tab = CameraTab(_collection())
    end = time.monotonic() + 3
    while tab.sound.findData("CABLE Input") < 0 and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert tab.sound.findData("CABLE Input") > 0 and asked_on and asked_on[0] is not threading.main_thread()
