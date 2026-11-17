"""Three ideas from the Settings review: a recording stops itself before the drive fills, the doctor checks
all places against the upload together, and Clips and the virtual camera can be switched off entirely."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.core.scene import Canvas, Output, SceneCollection  # noqa: E402
from sprigcast.output import storage  # noqa: E402

GB = 1024 ** 3


@pytest.fixture
def app(monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("DiskUploadFeaturesTest")
    QSettings().clear()
    yield app
    QSettings().clear()


# Low disk ----------------------------------------------------------------------------------

def test_the_watch_says_stop_only_when_the_drive_is_about_to_fill(tmp_path):
    free = [50 * GB]
    watch = storage.SpaceWatch(tmp_path, 8000, free=lambda _p: free[0])
    assert not watch.must_stop()  # not checked yet
    watch.check()
    assert not watch.must_stop()
    free[0] = storage.FLOOR_BYTES + 1024  # a few seconds left above the floor
    watch.check()
    assert watch.must_stop()
    unknown = storage.SpaceWatch(tmp_path, 8000, free=lambda _p: None)
    unknown.check()
    assert not unknown.must_stop()  # can't tell: never stops a recording on a guess


def test_room_to_copy_keeps_the_floor(tmp_path):
    f = tmp_path / "rec.mp4"
    f.write_bytes(b"x" * 1000)
    assert storage.room_to_copy(f, free=lambda _p: storage.FLOOR_BYTES + 5000)
    assert not storage.room_to_copy(f, free=lambda _p: storage.FLOOR_BYTES + 500)
    assert storage.room_to_copy(f, free=lambda _p: None)
    assert not storage.room_to_copy(tmp_path / "gone.mp4", free=lambda _p: 100 * GB)


def test_finishing_an_mp4_is_skipped_without_room(tmp_path, monkeypatch):
    from sprigcast.output import naming

    f = tmp_path / "rec.mp4"
    f.write_bytes(b"not really an mp4")
    monkeypatch.setattr(storage, "room_to_copy", lambda p, free=None: False)
    assert naming.finish_mp4(f) is False
    assert f.read_bytes() == b"not really an mp4" and not list(tmp_path.glob("*.finishing.mp4"))


# Upload --------------------------------------------------------------------------------------

def _places(*outs):
    return SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)], sources=[], scenes=[],
                           outputs=list(outs))


def _out(oid, kbps, **kw):
    kw.setdefault("enabled", True)
    return Output(id=oid, type="stream", service="twitch", canvas="horizontal", key_ref=oid, bitrate_kbps=kbps, **kw)


def test_planned_kbps_adds_every_place_that_is_on():
    from sprigcast.app.doctor import planned_kbps

    col = _places(_out("a", 6000), _out("b", 4000), _out("c", 9000, enabled=False),
                  _out("d", 2500, enhanced=True, enhanced_max_kbps=8000))
    audio = col.stream_encoder.audio_kbps
    assert planned_kbps(col) == 6000 + 4000 + 8000 + 3 * audio


def test_usable_upload_uses_the_measured_test_or_a_clean_stream(app):
    from sprigcast.app import optimize
    from sprigcast.app.doctor import usable_upload

    q = QSettings()
    assert usable_upload(q) is None
    optimize.remember_measured(10_000)
    assert usable_upload(QSettings()) == int(10_000 * optimize.UPLOAD_SHARE)
    q.setValue(optimize.PROVEN, 12_000)
    assert usable_upload(q) == 12_000
    optimize.remember_measured(None)  # a failed test leaves the last one
    assert int(QSettings().value(optimize.MEASURED)) == 10_000


def test_all_places_together_against_the_upload():
    from sprigcast.app.doctor import Checkup, _all_places, planned_kbps

    col = _places(_out("a", 6000), _out("b", 6000))
    total = planned_kbps(col)
    check = lambda up: _all_places(Checkup(collection=col, has_key=lambda r: True, upload_kbps=up))  # noqa: E731
    assert check(None) == [] and check(total) == []
    warn = check(int(total / 1.1))
    assert warn[0].level == "warning" and "Your 2 places add up to" in warn[0].text
    bad = check(total // 2)
    assert bad[0].level == "bad" and "fewer places" in bad[0].text
    one = _all_places(Checkup(collection=_places(_out("a", 6000)), has_key=lambda r: True, upload_kbps=3000))
    assert "This stream needs" in one[0].text and "lower its bitrate" in one[0].text


# Features ------------------------------------------------------------------------------------

def test_feature_switches_round_trip(app):
    from sprigcast.app import features

    assert features.on("clips") and features.on("camera")
    features.set_off({"clips", "nonsense"})
    assert not features.on("clips") and features.on("camera") and features.off() == {"clips"}
    features.set_off(set())
    assert features.on("clips")


@pytest.fixture
def window(app, tmp_path):
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_places(), tmp_path / "scenes.json")
    yield w
    w.close()


def test_clips_off_hides_the_button_and_loads_nothing(window, monkeypatch):
    from sprigcast.app import features

    stopped = []

    class Replay:
        def stop(self):
            stopped.append(True)

    window.replay = Replay()
    features.set_off({"clips"})
    window._after_settings()
    assert stopped and window.replay is None
    assert window.live_side.clip.isHidden()
    window._toggle_replay(True)  # a hotkey, the API or the command list
    assert window.replay is None and "Clips are off" in window.statusBar().currentMessage()
    assert window._save_replay() == []
    names = {c.title for c in window._palette_commands()}
    assert "Save replay" not in names and "Clips ready" not in names
    features.set_off(set())
    window._after_settings()
    assert not window.live_side.clip.isHidden()
    assert "Save replay" in {c.title for c in window._palette_commands()}


def test_camera_off_hides_it_and_never_starts_it(window, monkeypatch):
    from sprigcast.app import features
    from sprigcast.app.output_page import CAMERA_ON
    from sprigcast.output import virtualcam

    monkeypatch.setattr(virtualcam, "supported", lambda: (True, ""))
    monkeypatch.setattr(virtualcam, "installed", lambda: True)
    started = []

    class Cam:
        error = ""

        def __init__(self, *a, **k):
            pass

        def start(self):
            started.append(True)
            return True

        def stop(self):
            started.append(False)

    monkeypatch.setattr(virtualcam, "VirtualCamera", Cam)
    QSettings().setValue(CAMERA_ON, True)
    features.set_off({"camera"})
    window._after_settings()
    assert window.camera is None and not started
    assert window.live_side.camera.isHidden() and window.live_side.camera_settings.isHidden()
    window._toggle_camera()
    assert window.camera is None and not started
    features.set_off(set())
    window._after_settings()
    assert window.camera is not None and started == [True]
    assert not window.live_side.camera.isHidden()
    features.set_off({"camera"})
    window._after_settings()
    assert window.camera is None and started == [True, False]


def test_settings_general_has_the_feature_ticks(app):
    from sprigcast.app import features
    from sprigcast.app.settings_window import SettingsWindow

    w = SettingsWindow(_places(), default_recordings_folder="C:/Videos/SprigCast", encoder="NVENC",
                       audio_devices={"audio_input": ["USB Mic"]})
    try:
        w.ensure("General")
        assert set(w.feature_ticks) == {"clips", "camera"}
        w.feature_ticks["clips"].setChecked(False)
        assert w.apply()
        assert features.off() == {"clips"}
    finally:
        w.deleteLater()
