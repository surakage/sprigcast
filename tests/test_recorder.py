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
        while rec._video.qsize() > VIDEO_QUEUE // 2:  # faster than real time: let the encoder keep up
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

    def add_tap(self, cb, tracks=("stream",)):
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


def test_nvenc_shared_context_is_optional(monkeypatch):
    from sprigcast.output import recorder

    assert recorder.encoder_hwaccel("libx264") is None  # only NVENC uses a GPU context
    monkeypatch.setenv("SPRIGCAST_NVENC_OWN_CONTEXT", "1")
    assert recorder.nvenc_hwaccel() is None
    monkeypatch.delenv("SPRIGCAST_NVENC_OWN_CONTEXT")
    monkeypatch.setattr(recorder, "_shared_gpu_ok", None)
    monkeypatch.setattr(recorder, "nvenc_works", lambda: False)
    assert recorder.nvenc_hwaccel() is None and recorder._shared_gpu_ok is False  # no NVENC: nothing to share
    monkeypatch.setattr(recorder, "_shared_gpu_ok", True)
    accel = recorder.nvenc_hwaccel()
    assert accel is not None and accel.options == {"primary_ctx": "1"}


def test_nvenc_without_b_frames_keeps_two_surfaces_and_no_delay():
    from sprigcast.core.scene import EncoderSettings
    from sprigcast.output.recorder import video_options

    lean = video_options("h264_nvenc", EncoderSettings(b_frames=0), 6000)
    assert lean["delay"] == "0" and lean["surfaces"] == "2"
    for enc in (EncoderSettings(b_frames=2), EncoderSettings(b_frames=0, lookahead=True)):
        opts = video_options("h264_nvenc", enc, 6000)
        assert "delay" not in opts and "surfaces" not in opts


def test_timer_jitter_leaves_no_holes_but_real_lateness_does(tmp_path):
    import random

    class F:
        def __init__(self, t):
            self.time = t

    def numbers(fps, render_fps, times):
        rec = Recorder(tmp_path / "x.mkv", 64, 64, fps, 1000, "libx264", 0.0)
        out = []
        for t in times:
            pts = rec._assign_pts(F(t))
            if pts is not None:
                out.append(pts)
        return out

    rng = random.Random(1)
    # 60 fps ticks sitting right on a slot boundary (phase 0.74), each up to 1 ms early or late
    ticks = [(i + 0.74) / 60 + rng.uniform(-0.001, 0.001) for i in range(600)]
    got = numbers(60, 60, ticks)
    assert got == list(range(got[0], got[0] + len(got)))  # no gaps, no repeats
    # a 60 fps canvas into a 30 fps output keeps every other render, evenly
    halved = numbers(30, 60, [i / 60 + rng.uniform(-0.001, 0.001) for i in range(120)])
    assert halved == list(range(60))
    # a tick that's really late (a whole frame) leaves one gap, and the numbering stays on the clock
    late = [i / 60 for i in range(10)] + [(i + 1) / 60 for i in range(10, 20)]
    got = numbers(60, 60, late)
    assert got[-1] == 20 and len(got) == 20
    # a render loop running a bit slow (59.5 fps) skips a render now and then instead of drifting
    slow = numbers(60, 60, [i / 59.5 for i in range(595)])
    assert abs(slow[-1] - 594 / 59.5 * 60) < 1


def test_frames_arriving_while_the_encoder_opens_dont_pile_up(tmp_path):
    from sprigcast.output.recorder import OPENING_QUEUE

    rec = Recorder(tmp_path / "o.mkv", 16, 16, 60, 500, "libx264", 0.0)
    rec.opening_queue = OPENING_QUEUE
    rec._opening = True  # as after start(), before the encoder is ready
    for i in range(40):
        rec.push_frame(Frame("h", 16, 16, bytes(16 * 16 * 4), i / 60))
    assert rec._video.qsize() == OPENING_QUEUE and rec.dropped == 0 and rec.skipped_opening == 38
    assert [pts for pts, _ in rec._video.queue] == [38, 39]  # the newest ones


def test_an_item_whose_source_is_gone_is_ignored():
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig

    col = SceneCollection(canvases=[Canvas(id="h", width=64, height=36)],
                          sources=[SourceConfig(id="gone", kind="color", name="Gone")],
                          scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source="gone")])])
    col.sources.clear()  # removed while a scene still places it
    assert col.parts_inside(col.scenes[0]) == [] and col.inner_shapes(col.scenes[0]) == []


def _fresh_pick(monkeypatch, working):
    """pick_codec() on a PC where only the encoders in `working` open."""
    from sprigcast.output import recorder

    monkeypatch.setattr(recorder, "_picked", None)
    monkeypatch.setattr(recorder, "encoder_works", lambda codec: codec in working)
    monkeypatch.setattr(recorder, "nvenc_works", lambda: "h264_nvenc" in working)
    return recorder.pick_codec()


def test_the_best_working_encoder_is_picked(monkeypatch):
    monkeypatch.delenv("SPRIGCAST_ENCODER", raising=False)
    assert _fresh_pick(monkeypatch, {"h264_nvenc", "h264_qsv", "libx264"}) == "h264_nvenc"
    assert _fresh_pick(monkeypatch, {"h264_amf", "h264_mf", "libx264"}) == "h264_amf"  # AMD card
    assert _fresh_pick(monkeypatch, {"h264_qsv", "h264_mf", "libx264"}) == "h264_qsv"  # Intel graphics
    assert _fresh_pick(monkeypatch, {"h264_mf", "libx264"}) == "h264_mf"
    assert _fresh_pick(monkeypatch, {"libx264"}) == "libx264"  # no card encoder: the CPU does it
    monkeypatch.setenv("SPRIGCAST_ENCODER", "libx264")
    assert _fresh_pick(monkeypatch, {"h264_nvenc", "libx264"}) == "libx264"  # asked for by name
    monkeypatch.setenv("SPRIGCAST_ENCODER", "h264_qsv")
    assert _fresh_pick(monkeypatch, {"h264_nvenc", "libx264"}) == "h264_nvenc"  # asked for one that doesn't work


def test_amd_intel_and_windows_encoders_get_their_own_options(monkeypatch):
    from sprigcast.output import recorder
    from sprigcast.core.scene import EncoderSettings
    from sprigcast.output.recorder import codec_label, codec_short, rate_limits, video_options

    cbr = EncoderSettings(rate_control="cbr", tuning="ll", preset="p2", profile="baseline")
    amf = video_options("h264_amf", cbr, 4500)
    assert amf["rc"] == "cbr" and amf["maxrate"] == "4500k" and amf["usage"] == "lowlatency"
    assert amf["quality"] == "speed" and amf["profile"] == "constrained_baseline"
    assert video_options("h264_amf", EncoderSettings(rate_control="cqp", cq_level=20), 4500)["qp_p"] == "20"
    qsv = video_options("h264_qsv", cbr, 4500)
    assert qsv["maxrate"] == qsv["bufsize"] == "4500k" and qsv["async_depth"] == "1" and qsv["preset"] == "veryfast"
    assert video_options("h264_qsv", EncoderSettings(rate_control="cqp", cq_level=20), 4500)["global_quality"] == "20"
    mf = video_options("h264_mf", cbr, 4500)
    assert mf == {"hw_encoding": "1", "rate_control": "cbr", "scenario": "live_streaming"}
    assert rate_limits("h264_amf", EncoderSettings(rate_control="vbr"), 4000) == (6000, 8000)
    assert not {"nal-hrd", "crf", "multipass"} & (set(amf) | set(qsv) | set(mf))  # nothing from x264 or NVENC
    assert codec_short("h264_qsv") == "Quick Sync" and "AMD" in codec_label("h264_amf")
    assert recorder.is_hardware("h264_mf") and not recorder.is_hardware("libx264")
    monkeypatch.setattr(recorder, "_plain", {"h264_mf", "h264_amf"})  # a driver that refuses our options
    assert video_options("h264_mf", cbr, 4500) == {"hw_encoding": "1"} and video_options("h264_amf", cbr, 4500) == {}


def test_an_encoder_that_refuses_the_options_is_used_plain(monkeypatch):
    from sprigcast.output import recorder

    monkeypatch.setattr(recorder, "_works", {})
    monkeypatch.setattr(recorder, "_plain", set())
    monkeypatch.setattr(recorder, "_try_encoder", lambda codec, hwaccel=None, options=None: not options)
    assert recorder.encoder_works("h264_amf") and "h264_amf" in recorder._plain
    monkeypatch.setattr(recorder, "_try_encoder", lambda codec, hwaccel=None, options=None: False)
    assert not recorder.encoder_works("h264_qsv")


def test_enhanced_broadcasting_options_follow_the_encoder():
    from sprigcast.output.enhanced import encoder_options
    from sprigcast.output.enhanced_stream import _sw_format

    amf = encoder_options("h264_amf", {"profile": "main"}, 3000)
    assert amf["rc"] == "cbr" and amf["profile"] == "main" and amf["maxrate"] == "3000k"
    assert _sw_format("h264_qsv") == "nv12" and _sw_format("libx264") == "yuv420p"
