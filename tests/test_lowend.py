import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from sprigcast.app import lowend  # noqa: E402
from sprigcast.core.scene import Canvas, EncoderSettings, Output, SceneCollection  # noqa: E402


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080, fps=60),
                  Canvas(id="vertical", width=1080, height=1920, fps=60)],
        outputs=[Output(id="tw", type="stream", service="twitch", canvas="horizontal", bitrate_kbps=6000),
                 Output(id="tt", type="stream", service="tiktok", canvas="vertical", bitrate_kbps=3000, fps=60)],
        stream_encoder=EncoderSettings(preset="p5", lookahead=True, multipass="qres", b_frames=2),
    )


def test_a_pc_with_little_to_spare_is_noticed():
    assert lowend.weak_reasons(lowend.PcFacts(16, 32.0, "h264_nvenc")) == []
    assert lowend.weak_reasons(lowend.PcFacts(8, 16.0, "h264_qsv")) == []  # Intel graphics encoder is fine
    reasons = lowend.weak_reasons(lowend.PcFacts(4, 7.8, "libx264"))
    assert reasons == ["4 CPU threads", "8 GB of memory", "no graphics-card encoder, so the CPU encodes"]
    assert lowend.weak_reasons(lowend.PcFacts(8, 0.0, "h264_amf")) == []  # memory unknown: not held against it


def test_light_size_keeps_the_shape():
    assert lowend.light_size(1920, 1080) == (1280, 720)
    assert lowend.light_size(1080, 1920) == (720, 1280)
    assert lowend.light_size(2560, 1440) == (1280, 720)
    assert lowend.light_size(1280, 720) is None and lowend.light_size(640, 360) is None


def test_light_settings_list_then_apply_and_leave_scenes_alone():
    col = _collection()
    lines, fps = lowend.light_changes(col)
    assert fps == {"horizontal": 30, "vertical": 30} and "30 fps instead of 60" in lines[0]
    assert any("1280×720 and 720×1280" in line for line in lines) and "Stream bitrate 4000 kbps" in lines
    assert col.outputs[0].size is None and col.stream_encoder.lookahead  # only listed, nothing changed yet

    lowend.light_changes(col, apply=True)
    tw, tt = col.outputs
    assert tw.size == (1280, 720) and tw.bitrate_kbps == 4000
    assert tt.size == (720, 1280) and tt.bitrate_kbps == 3000 and tt.fps is None  # lower bitrate kept
    assert (col.canvases[0].width, col.canvases[0].height, col.canvases[0].fps) == (1920, 1080, 60)
    for enc in (col.stream_encoder, col.recording.encoder):
        assert (enc.preset, enc.lookahead, enc.multipass, enc.b_frames) == ("p3", False, "disabled", 0)
    assert col.recording.bitrate_kbps == 8000

    again, fps = lowend.light_changes(col, pending_fps={"horizontal": 30, "vertical": 30})
    assert again == [] and fps == {}  # already light once the frame rate is waiting for a restart


def test_a_faster_preset_is_not_slowed_down():
    col = _collection()
    col.stream_encoder = EncoderSettings(preset="p1", b_frames=0)
    lowend.light_changes(col, apply=True)
    assert col.stream_encoder.preset == "p1"


def test_frame_rate_waits_for_the_next_start():
    from PySide6.QtCore import QCoreApplication, QSettings

    from sprigcast.app.settings_window import apply_pending_video

    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("LowEndTest")
    QSettings().clear()
    col = _collection()
    lowend.store_fps({"horizontal": 30})
    assert lowend.waiting_fps() == {"horizontal": 30}
    assert apply_pending_video(col)
    assert [c.fps for c in col.canvases] == [30, 60] and col.canvases[0].width == 1920
    QSettings().clear()


def test_the_suggestion_shows_once_and_only_on_a_slower_pc(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("LowEndWindowTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_collection(), tmp_path / "scenes.json")
    try:
        asked = []
        monkeypatch.setattr(w, "use_light_settings", lambda why="": asked.append(why))
        monkeypatch.setattr(lowend, "pc_facts", lambda: lowend.PcFacts(16, 32.0, "h264_nvenc"))
        w.suggest_light_settings()
        assert asked == []  # a strong PC is never asked
        QSettings().remove(lowend.SUGGESTED)
        monkeypatch.setattr(lowend, "pc_facts", lambda: lowend.PcFacts(4, 8.0, "libx264"))
        w.suggest_light_settings()
        w.suggest_light_settings()
        assert len(asked) == 1 and "4 CPU threads" in asked[0]
    finally:
        w.close()
        QSettings().clear()


def test_recordings_shrink_and_cpu_encoding_gets_superfast():
    from sprigcast.output.recorder import shrink_to

    assert shrink_to(1920, 1080, 720) == (1280, 720) and shrink_to(1080, 1920, None) is None
    assert shrink_to(1280, 720, 1080) is None
    col = _collection()
    col.recording.canvases = ["horizontal"]
    lines, _ = lowend.light_changes(col, apply=True, cpu_encoding=True)
    assert "Recordings and replays saved at 720p" in lines and col.recording.short_side == 720
    assert col.stream_encoder.preset == "p2" and col.recording.encoder.preset == "p2"
    assert "superfast" in " ".join(lines)
    again, _ = lowend.light_changes(col, pending_fps={"horizontal": 30, "vertical": 30}, cpu_encoding=True)
    assert again == []
