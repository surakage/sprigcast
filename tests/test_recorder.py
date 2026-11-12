import time
from datetime import datetime
from pathlib import Path

import av
import numpy as np
import pytest

from sprigcast.output.recorder import VIDEO_QUEUE, Recorder, RecordingSession, recording_path
from sprigcast.render.engine import Frame

pytestmark = pytest.mark.skipif("libx264" not in av.codecs_available, reason="needs libx264 in PyAV")


def _record(path: Path, container: str, seconds: float = 1.0, fps: int = 30) -> Recorder:
    w, h = 320, 180
    start = 1000.0
    rec = Recorder(path, w, h, fps, 2000, "libx264", start, container)
    rec.start()
    for i in range(int(seconds * fps)):
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        rgba[..., 0] = (i * 8) % 256  # red ramps so frames differ
        rgba[..., 3] = 255
        rec.push_frame(Frame("horizontal", w, h, rgba.tobytes(), start + i / fps))
        if i % 2 == 0:  # roughly real-time audio: 30 blocks of 1024 per second ≈ 0.64 s here, fine
            rec.push_audio(np.full((1024, 2), 0.1, dtype=np.float32))
        time.sleep(0.001)
    rec.stop()
    return rec


@pytest.mark.parametrize("container", ["mkv", "mp4"])
def test_recorder_writes_playable_file_with_video_and_audio(tmp_path, container):
    path = tmp_path / f"test.{container}"
    rec = _record(path, container)
    assert rec.error is None
    assert rec.frames == 30 and rec.dropped == 0
    with av.open(str(path)) as f:
        kinds = sorted(s.type for s in f.streams)
        assert kinds == ["audio", "video"]
        v = f.streams.video[0]
        assert (v.codec_context.width, v.codec_context.height) == (320, 180)
        assert v.codec_context.name == "h264"
        decoded = sum(1 for _ in f.decode(video=0))
    assert decoded == 30


def test_frames_rendered_before_start_are_skipped(tmp_path):
    rec = Recorder(tmp_path / "a.mkv", 64, 64, 30, 1000, "libx264", 10.0)
    rec.start()
    data = bytes(64 * 64 * 4)
    rec.push_frame(Frame("h", 64, 64, data, 9.5))  # before start
    rec.push_frame(Frame("h", 64, 64, data, 10.0))
    rec.push_frame(Frame("h", 64, 64, data, 10.001))  # same 1/30 s slot as the previous one
    rec.push_frame(Frame("h", 64, 64, data, 10.1))
    rec.stop()
    assert rec.error is None and rec.frames == 2


def test_recording_path_never_overwrites(tmp_path):
    when = datetime(2026, 11, 10, 19, 30, 5)
    first = recording_path(tmp_path, "vertical", when, "mkv")
    assert first.name == "SprigCast 2026-11-10 19-30-05 vertical.mkv"
    first.write_bytes(b"x")
    assert recording_path(tmp_path, "vertical", when, "mkv").name == "SprigCast 2026-11-10 19-30-05 vertical (2).mkv"


class FakeMixer:
    def __init__(self):
        self.taps = []

    def add_tap(self, cb):
        self.taps.append(cb)

    def remove_tap(self, cb):
        self.taps.remove(cb)


class FakeEngine:
    def __init__(self):
        self.mixer = FakeMixer()
        self.taps = {}

    def add_frame_tap(self, canvas_id, cb, nv12_size=None):
        self.taps.setdefault(canvas_id, []).append(cb)

    def remove_frame_tap(self, canvas_id, cb):
        self.taps[canvas_id].remove(cb)


class C:
    def __init__(self, id, w, h):
        self.id, self.width, self.height, self.fps = id, w, h, 30


def test_session_records_both_canvases_and_detaches_on_stop(tmp_path):
    engine = FakeEngine()
    session = RecordingSession.start(engine, [C("horizontal", 64, 36), C("vertical", 36, 64)], tmp_path, 1000, codec="libx264")
    assert set(engine.taps) == {"horizontal", "vertical"} and len(engine.mixer.taps) == 2
    t = time.perf_counter()
    for i in range(5):
        for canvas_id, (w, h) in (("horizontal", (64, 36)), ("vertical", (36, 64))):
            for cb in engine.taps[canvas_id]:
                cb(Frame(canvas_id, w, h, bytes(w * h * 4), t + 0.01 + i / 30))
        for cb in engine.mixer.taps:
            cb(np.zeros((1024, 2), dtype=np.float32))
    session.stop()
    assert engine.mixer.taps == [] and all(not taps for taps in engine.taps.values())
    assert session.errors == []
    assert sorted(p.name.split()[-1] for p in session.paths) == ["horizontal.mkv", "vertical.mkv"]
    for path in session.paths:
        with av.open(str(path)) as f:
            assert sum(1 for _ in f.decode(video=0)) == 5


def test_scales_and_halves_frame_rate(tmp_path):
    """A 60 fps 320x180 canvas encoded at 160x90, 30 fps keeps every other frame, evenly."""
    start = 100.0
    rec = Recorder(tmp_path / "s.mkv", 320, 180, 30, 1000, "libx264", start, out_size=(160, 90))
    rec.start()
    data = bytes(320 * 180 * 4)
    for i in range(60):  # one second at 60 fps, with a little jitter
        rec.push_frame(Frame("h", 320, 180, data, start + i / 60 + (0.002 if i % 3 else -0.001)))
        time.sleep(0.004)  # don't outrun the queue; this test is about timestamps
    rec.stop()
    assert rec.error is None and rec.dropped == 0 and rec.frames == 30
    with av.open(str(tmp_path / "s.mkv")) as f:
        v = f.streams.video[0].codec_context
        assert (v.width, v.height) == (160, 90)
        pts = [frame.pts for frame in f.decode(video=0)]
    steps = {round(b - a) for a, b in zip(pts, pts[1:])}
    assert max(steps) - min(steps) <= 1  # evenly spaced (MKV stores milliseconds: 33 or 34)


def test_colours_survive_encoding(tmp_path):
    w, h = 64, 64
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[...] = (232, 164, 184, 255)  # the Sprig pink
    rec = Recorder(tmp_path / "c.mkv", w, h, 30, 2000, "libx264", 0.0)
    rec.start()
    for i in range(10):
        rec.push_frame(Frame("h", w, h, rgba.tobytes(), i / 30))
        time.sleep(0.002)
    rec.stop()
    with av.open(str(tmp_path / "c.mkv")) as f:
        pixel = next(f.decode(video=0)).to_ndarray(format="rgb24")[32, 32]
    assert np.allclose(pixel, (232, 164, 184), atol=4)


def test_surplus_renders_are_skipped_before_the_queue(tmp_path):
    """A 60 fps canvas into a 30 fps output: half the renders are skipped, never counted as drops."""
    rec = Recorder(tmp_path / "q.mkv", 16, 16, 30, 500, "libx264", 0.0)  # not started: nothing drains the queue
    for i in range(2 * VIDEO_QUEUE):  # exactly fills the queue at 30 fps
        rec.push_frame(Frame("h", 16, 16, bytes(16 * 16 * 4), i / 60))
    assert rec._video.qsize() == VIDEO_QUEUE
    assert rec.dropped == 0
    rec.push_frame(Frame("h", 16, 16, bytes(16 * 16 * 4), VIDEO_QUEUE / 30))  # a genuinely new frame with a full queue
    assert rec.dropped == 1


def test_encoder_settings_map_to_ffmpeg_options():
    from sprigcast.core.scene import EncoderSettings
    from sprigcast.output.recorder import video_options

    nv = video_options("h264_nvenc", EncoderSettings(multipass="qres", lookahead=True, tuning="ll"), 6000)
    assert nv["rc"] == "cbr" and nv["maxrate"] == "6000k" and nv["multipass"] == "qres"
    assert nv["tune"] == "ll" and nv["rc-lookahead"] == "16" and nv["preset"] == "p5"
    cq = video_options("h264_nvenc", EncoderSettings(rate_control="cqp", cq_level=18, adaptive_quant=False), 6000)
    assert cq["rc"] == "constqp" and cq["qp"] == "18" and cq["spatial-aq"] == "0" and "maxrate" not in cq
    x = video_options("libx264", EncoderSettings(preset="p7", tuning="ull"), 4000)
    assert x["preset"] == "fast" and x["tune"] == "zerolatency" and x["nal-hrd"] == "cbr"


def test_encoder_settings_reach_the_file(tmp_path):
    import av

    from sprigcast.core.scene import EncoderSettings

    enc = EncoderSettings(rate_control="cqp", cq_level=30, keyint_sec=1, b_frames=0, audio_kbps=96)
    rec = Recorder(tmp_path / "e.mkv", 64, 64, 30, 1000, "libx264", 0.0, encoder=enc)
    assert rec._video_options()["crf"] == "30"
    container, video, audio = rec._open()
    assert video.codec_context.gop_size == 30 and video.codec_context.max_b_frames == 0
    assert audio.bit_rate == 96000
    container.close()
