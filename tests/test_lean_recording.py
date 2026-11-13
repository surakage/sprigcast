import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402

from sprigcast.app.main import lean_recording, lean_stream  # noqa: E402
from sprigcast.core.scene import RecordingSettings, SceneCollection  # noqa: E402


def _settings(tmp_path):
    QCoreApplication.setOrganizationName("SprigCastLeanTest")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    QSettings().clear()


def test_new_recordings_have_no_b_frames():
    assert RecordingSettings().encoder.b_frames == 0


def test_old_default_switched_once(tmp_path):
    _settings(tmp_path)
    c = SceneCollection(canvases=[])
    c.recording.encoder.b_frames = 2
    assert lean_recording(c) and c.recording.encoder.b_frames == 0
    c.recording.encoder.b_frames = 2  # the user turned them back on afterwards
    assert not lean_recording(c) and c.recording.encoder.b_frames == 2


def test_own_choice_kept(tmp_path):
    _settings(tmp_path)
    c = SceneCollection(canvases=[])
    c.recording.encoder.b_frames = 3
    assert not lean_recording(c) and c.recording.encoder.b_frames == 3


def test_streams_switched_once_and_own_choice_kept(tmp_path):
    _settings(tmp_path)
    assert SceneCollection(canvases=[]).stream_encoder.b_frames == 0  # new setups: the light encoder
    c = SceneCollection(canvases=[])
    c.stream_encoder.b_frames = 2  # the old default
    assert lean_stream(c) and c.stream_encoder.b_frames == 0
    c.stream_encoder.b_frames = 2  # turned back on afterwards
    assert not lean_stream(c) and c.stream_encoder.b_frames == 2
    _settings(tmp_path / "other")
    own = SceneCollection(canvases=[])
    own.stream_encoder.b_frames = 4
    assert not lean_stream(own) and own.stream_encoder.b_frames == 4
