"""SprigCast's own NVENC path (output/nvenc_d3d11.py) feeds FFmpeg's muxers with Annex B packets.

There's no NVIDIA card in CI, so a stand-in encoder makes the same kind of output with x264: Annex B
H.264, SPS/PPS inline on keyframes, no B-frames, one packet per frame.
"""

import time
from fractions import Fraction

import av
import numpy as np
import pytest

from sprigcast.output import nvenc_d3d11, recorder
from sprigcast.output.recorder import VIDEO_QUEUE, Recorder
from sprigcast.render.engine import Frame

pytestmark = pytest.mark.skipif("libx264" not in av.codecs_available, reason="needs libx264 in PyAV")


class StandInEncoder:
    made: list = []

    def __init__(self, width, height, fps, preset=None, **settings):
        self.width, self.height, self.settings = width, height, settings
        self.ctx = av.CodecContext.create("libx264", "w")
        self.ctx.width, self.ctx.height, self.ctx.pix_fmt = width, height, "yuv420p"
        self.ctx.time_base, self.ctx.framerate = Fraction(1, fps), fps
        self.ctx.gop_size = settings.get("gop_frames") or 60
        self.ctx.max_b_frames = 0
        self.ctx.options = {"tune": "zerolatency", "preset": "ultrafast"}
        self.n, self.closed = 0, False
        StandInEncoder.made.append(self)

    def encode_frame(self, nv12, keyframe=False):
        if nv12 is None:
            return b"", False
        vf = av.VideoFrame(self.width, self.height, "nv12")
        y = self.width * self.height
        vf.planes[0].update(bytes(nv12[:y]))
        vf.planes[1].update(bytes(nv12[y:]))
        vf = vf.reformat(format="yuv420p")
        vf.pts = self.n
        if keyframe:
            from av.video.frame import PictureType

            vf.pict_type = PictureType.I
        self.n += 1
        packets = self.ctx.encode(vf)
        return b"".join(bytes(p) for p in packets), any(p.is_keyframe for p in packets)

    def encode(self, nv12, keyframe=False):
        return self.encode_frame(nv12, keyframe)[0]

    def set_bitrate(self, kbps, max_kbps=0, buffer_kbits=0):
        self.settings.setdefault("bitrate_changes", []).append((kbps, max_kbps, buffer_kbits))

    def close(self):
        self.closed = True


@pytest.fixture
def direct(monkeypatch):
    StandInEncoder.made = []
    monkeypatch.setattr(nvenc_d3d11, "D3D11Encoder", StandInEncoder)
    monkeypatch.setattr(recorder, "direct_nvenc_ok", lambda: True)
    return StandInEncoder


def _nv12_frames(rec, w, h, n, fps=30, start=1000.0):
    # Frames pushed while the encoder is still opening are trimmed to the newest few (on purpose, see
    # Recorder.push_frame); on a busy machine that made the frame counts below come up one short.
    assert rec._opened.wait(10), "the encoder never opened"
    for i in range(n):
        data = bytes([(i * 8) % 256]) * (w * h) + bytes([128]) * (w * h // 2)
        rec.push_frame(Frame("horizontal", w, h, data, start + i / fps, "nv12"))
        if i % 2 == 0:
            rec.push_audio(np.full((1024, 2), 0.1, dtype=np.float32))
        while rec._video.qsize() > VIDEO_QUEUE // 2:
            time.sleep(0.001)


@pytest.mark.parametrize("container", ["mkv", "mp4", "flv"])
def test_recording_through_the_direct_encoder_plays_back(tmp_path, direct, container):
    from sprigcast.core.scene import EncoderSettings

    path = tmp_path / f"d.{container}"
    rec = Recorder(path, 320, 180, 30, 3000, "h264_nvenc", 1000.0, container,
                   encoder=EncoderSettings(rate_control="cbr", b_frames=0))
    assert rec.uses_direct()
    rec.start()
    _nv12_frames(rec, 320, 180, 45)
    rec.stop()
    assert rec.error is None and rec.frames == 45
    enc = direct.made[0]
    assert enc.closed and enc.settings["bitrate_kbps"] == 3000 and enc.settings["rate_control"] == "cbr"
    assert enc.settings["gop_frames"] == 60
    with av.open(str(path)) as f:
        v = f.streams.video[0]
        assert (v.codec_context.width, v.codec_context.height) == (320, 180)
        first = next(f.demux(video=0))
        assert first.is_keyframe  # the file starts on a keyframe
    with av.open(str(path)) as f:
        assert sum(1 for _ in f.decode(video=0)) == 45


def test_b_frames_and_lookahead_stay_on_ffmpeg(tmp_path, direct):
    from sprigcast.core.scene import EncoderSettings

    assert not Recorder(tmp_path / "a.mkv", 64, 64, 30, 1000, "h264_nvenc", 0.0,
                        encoder=EncoderSettings(b_frames=2)).uses_direct()
    assert not Recorder(tmp_path / "b.mkv", 64, 64, 30, 1000, "h264_nvenc", 0.0,
                        encoder=EncoderSettings(b_frames=0, lookahead=True)).uses_direct()
    assert not Recorder(tmp_path / "c.mkv", 64, 64, 30, 1000, "libx264", 0.0,
                        encoder=EncoderSettings(b_frames=0)).uses_direct()


def test_streams_use_the_direct_encoder_and_change_bitrate_in_place(tmp_path, direct, monkeypatch):
    """A stream with no B-frames goes through SprigCast's own NVENC; auto-pilot reconfigures it."""
    from sprigcast.core.scene import EncoderSettings
    from sprigcast.output import stream as stream_mod

    out = tmp_path / "s.flv"
    monkeypatch.setattr(stream_mod, "stream_url", lambda server, key, bandwidth_test=False: str(out))
    st = stream_mod.Streamer("test", "", "key", 320, 180, 30, 3000, "h264_nvenc", 1000.0,
                             encoder=EncoderSettings(rate_control="cbr", b_frames=0))
    assert st.uses_direct()
    st.start()
    _nv12_frames(st, 320, 180, 15)
    st._want_kbps = 2000  # what auto-pilot asks for when the upload struggles
    _nv12_frames(st, 320, 180, 15, start=1000.5)
    st.stop()
    assert st.error is None
    assert direct.made[0].settings["bitrate_changes"] == [(2000, 2000, 2000)]  # CBR: max and buffer follow
    with av.open(str(out)) as f:
        assert sum(1 for _ in f.decode(video=0)) == 30
    stream_b = stream_mod.Streamer("b", "", "key", 64, 64, 30, 1000, "h264_nvenc", 0.0,
                                   encoder=EncoderSettings(b_frames=2))
    assert not stream_b.uses_direct()  # B-frames still need FFmpeg's encoder


def test_shared_recording_and_replay_through_the_direct_encoder(tmp_path, direct):
    from test_shared_encoder import FakeEngine

    from sprigcast.core.scene import EncoderSettings
    from sprigcast.output.shared import EncoderPool, SharedRecording, SharedReplay

    class C:
        id, width, height, fps = "horizontal", 64, 36, 30

    engine = FakeEngine()
    pool = EncoderPool(engine)
    lean = EncoderSettings(rate_control="vbr", b_frames=0)
    replay = SharedReplay.start(pool, [C], tmp_path, 10, 800, codec="h264_nvenc", encoder=lean)
    t = time.perf_counter()

    def feed(first, n):
        for i in range(first, first + n):
            while any(enc._video.qsize() > 5 for enc in list(pool.encoders.values())):
                time.sleep(0.002)
            for cb in engine.taps.get("horizontal", []):
                cb(Frame("horizontal", 64, 36, bytes(64 * 36) + bytes([128]) * (64 * 18), t + 0.01 + i / 30, "nv12"))
            for cb in engine.audio:
                cb(np.zeros((1600, 2), dtype=np.float32))
            time.sleep(0.003)
        time.sleep(0.3)

    feed(0, 30)
    rec = SharedRecording.start(pool, [C], tmp_path, 800, codec="h264_nvenc", encoder=lean)
    feed(30, 70)
    rec.stop()
    assert rec.errors == [] and len(direct.made) == 1  # one encoder for both
    with av.open(str(rec.paths[0])) as f:
        assert sum(1 for _ in f.decode(video=0)) >= 30
    clip = replay.save()[0]
    with av.open(str(clip)) as f:
        assert sum(1 for _ in f.decode(video=0)) >= 60
    replay.stop()


def test_encoder_check_waits_instead_of_racing(monkeypatch):
    """A stream and a recording starting together: both must see the check's answer, not a half-done False."""
    import threading

    class Slow:
        def __init__(self, *a, **k):
            time.sleep(0.2)

        def encode(self, nv12, keyframe=False):
            return b"\x00\x00\x00\x01"

        def close(self):
            pass

    monkeypatch.setattr(nvenc_d3d11, "D3D11Encoder", Slow)
    monkeypatch.setattr(recorder, "_direct_ok", None)
    monkeypatch.setattr(recorder, "nvenc_device", lambda: "direct")
    answers = []
    threads = [threading.Thread(target=lambda: answers.append(recorder.direct_nvenc_ok())) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert answers == [True, True, True]


def test_nvenc_works_without_cuda_when_our_encoder_does(monkeypatch):
    monkeypatch.setattr(recorder, "_nvenc_ok", None)
    monkeypatch.setattr(recorder, "nvenc_device", lambda: "direct")
    monkeypatch.setattr(recorder, "direct_nvenc_ok", lambda: True)
    monkeypatch.setattr(recorder, "_try_encoder", lambda *a, **k: pytest.fail("FFmpeg's NVENC was opened"))
    assert recorder.nvenc_works()
