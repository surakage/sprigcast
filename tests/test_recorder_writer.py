"""Recordings on the native engine go through the engine's own writer (output/recorder.py uses_writer):
encoding and writing on a thread of the engine's, never holding Python's lock."""

from __future__ import annotations

import logging
import time

import av
import numpy as np
import pytest
from test_native_media import (
    player_lib,  # noqa: F401 (builds or finds the engine's media library)
)

from sprigcast import sprigcore
from sprigcast.output import recorder as recorder_module
from sprigcast.output.recorder import VIDEO_QUEUE, Recorder
from sprigcast.render.engine import Frame

pytestmark = pytest.mark.skipif("libx264" not in av.codecs_available, reason="needs libx264 in PyAV")

W, H, FPS = 64, 48, 30


def _nv12(i: int) -> bytes:
    picture = np.full((H * 3 // 2, W), 128, np.uint8)
    picture[:H] = 16 + 4 * i
    return picture.tobytes()


def _record(path, container="mkv", kind="nv12", tracks=("stream",), frames=30) -> Recorder:
    start = 1000.0
    rec = Recorder(path, W, H, FPS, 800, "libx264", start, container, tracks=tracks)
    rec.start()
    assert rec.wait_open()
    for i in range(frames):
        if kind == "nv12":
            frame = Frame("h", W, H, _nv12(i), start + i / FPS, fmt="nv12")
        else:
            rgba = np.zeros((H, W, 4), np.uint8)
            rgba[..., 1] = 8 * i
            rgba[..., 3] = 255
            frame = Frame("h", W, H, rgba.tobytes(), start + i / FPS)
        rec.push_frame(frame)
        block = np.full((1600, 2), 0.1, np.float32)
        rec.push_audio(tuple(block for _ in tracks) if len(tracks) > 1 else block)
        while rec._video.qsize() > VIDEO_QUEUE // 2:  # faster than real time: let it keep up
            time.sleep(0.001)
    rec.stop()
    return rec


@pytest.fixture
def native(monkeypatch, player_lib):  # noqa: F811
    monkeypatch.setenv("SPRIGCAST_ENGINE", "native")
    monkeypatch.delenv("SPRIGCAST_NO_WRITER", raising=False)
    assert sprigcore.use_native_media()


@pytest.mark.parametrize(("container", "kind"), [("mkv", "nv12"), ("mp4", "rgba")])
def test_a_recording_on_the_native_engine_is_written_by_the_engine(native, tmp_path, caplog, container, kind):
    caplog.set_level(logging.INFO, logger="sprigcast.output.recorder")
    path = tmp_path / f"rec.{container}"
    rec = _record(path, container, kind, tracks=("stream", "voice"))
    assert rec.error is None
    assert any("written by the engine" in r.getMessage() for r in caplog.records)
    assert rec.frames == 30 and rec.dropped == 0 and rec.bytes_out > 0 and rec.writer is None
    with av.open(str(path)) as f:
        assert len(f.streams.audio) == 2
        if container == "mkv":
            assert [s.metadata.get("title") for s in f.streams.audio] == ["Stream", "Voice"]
        assert sum(1 for _ in f.decode(video=0)) == 30
    with av.open(str(path)) as f:
        seconds = sum(fr.samples for fr in f.decode(audio=0)) / 48000
    assert abs(seconds - 30 * 1600 / 48000) < 0.1
    if container == "mp4":
        assert b"moof" in path.read_bytes()  # in fragments: a crash leaves a playable file


def test_a_writer_that_cant_start_leaves_the_recording_to_pyav(native, tmp_path, monkeypatch, caplog):
    def broken(*a, **k):
        raise sprigcore.NativeError(-2, "the engine's writer isn't available")

    monkeypatch.setattr(sprigcore, "Writer", broken)
    rec = _record(tmp_path / "fallback.mkv")
    assert rec.error is None and rec.frames == 30
    assert any("recording through PyAV" in r.getMessage() for r in caplog.records)
    with av.open(str(tmp_path / "fallback.mkv")) as f:
        assert sum(1 for _ in f.decode(video=0)) == 30


def test_only_files_on_the_native_engine_use_it(native, monkeypatch, tmp_path):
    rec = Recorder(tmp_path / "x.mkv", W, H, FPS, 800, "libx264", 0.0)
    assert rec.uses_writer()
    monkeypatch.setenv("SPRIGCAST_NO_WRITER", "1")
    assert not rec.uses_writer()
    monkeypatch.delenv("SPRIGCAST_NO_WRITER")
    monkeypatch.setenv("SPRIGCAST_ENGINE", "python")
    assert not rec.uses_writer()
    monkeypatch.setenv("SPRIGCAST_ENGINE", "native")
    monkeypatch.setattr(Recorder, "uses_direct", lambda self: True)  # SprigCast's own NVENC takes card frames
    assert rec.uses_writer()  # with FFmpeg's NVENC on the engine's device (_card_device)
    monkeypatch.setenv("SPRIGCAST_NO_CARD_WRITER", "1")
    assert not rec.uses_writer()
    monkeypatch.delenv("SPRIGCAST_NO_CARD_WRITER")
    monkeypatch.setenv("SPRIGCAST_CPU_CONVERT", "1")  # no NV12 from the engine: no frames on the card either
    assert not rec.uses_writer()
    from sprigcast.output.replay import ReplayBuffer
    from sprigcast.output.stream import Streamer

    assert not Streamer.allow_writer and recorder_module.Recorder.allow_writer
    assert ReplayBuffer.packets_mode and not recorder_module.Recorder.packets_mode  # its packets come back


class _LinkEngine:
    """Stands in for sprigcore.Engine with outputs that can be linked to a writer: feed() is the frame thread."""

    def __init__(self, **_):
        self.links, self.unlinks = {}, []

    def stats(self):
        return {"rebuilds": 0, "faulted": 0, "hung": 0}

    def set_output(self, index, slot, width, height, scale="bilinear", share=False):
        if not width:
            self.links.pop((index, slot), None)

    def take_output(self, index, slot, width, height):
        return None

    def link_output(self, index, slot, writer, start, fps, last_pts=-1, keep=False, fill_seconds=0.0):
        self.links[(index, slot)] = (writer, start, fps, last_pts, keep)

    def unlink_output(self, index, slot):
        self.unlinks.append((index, slot))
        link = self.links.pop((index, slot), None)
        return (link[0].stats().last_pts, True) if link else (-1, False)

    def feed(self, picture, pts):
        for writer, *_ in self.links.values():
            while writer.stats().queued >= 4:  # the test feeds faster than real time
                time.sleep(0.002)
            writer.video(picture, pts)

    def close(self):
        pass


def test_a_linked_recording_takes_the_engines_frames_and_python_goes_on_after_them(native, tmp_path, monkeypatch):
    from test_native_bridge import _collection

    from sprigcast.render import native_bridge

    monkeypatch.setattr(sprigcore, "Engine", _LinkEngine)
    bridge = native_bridge.NativeCanvases(_collection())
    bridge._slots["h"] = 0
    key = ((W, H), False)
    bridge.set_outputs("h", [key])
    start = 1000.0
    rec = Recorder(tmp_path / "linked.mkv", W, H, FPS, 800, "libx264", start, "mkv")
    rec.start()
    assert rec.wait_open()

    def push(i):
        rec.push_frame(Frame("h", W, H, _nv12(i), start + i / FPS, fmt="nv12"))

    for i in range(10):
        push(i)
    deadline = time.monotonic() + 10
    while not rec._video.empty() and time.monotonic() < deadline:
        assert bridge.link_taps("h", [(rec.push_frame, key)]) == set()  # Python's frames first
        time.sleep(0.01)
    time.sleep(0.1)  # the last one taken off the queue is written
    assert bridge.link_taps("h", [(rec.push_frame, key)]) == {id(rec)} and rec.linked
    _writer, link_start, fps, last, keep = bridge.engine.links[(0, 0)]
    assert (link_start, fps, last, keep) == (start, FPS, 9, False)
    for i in range(10, 20):
        bridge.engine.feed(_nv12(i), i)
        push(i)  # a stray frame from Python meanwhile: not taken
    assert rec._video.empty()
    bridge.link_taps("h", [(rec.push_frame, key)])  # nothing changed: not linked again
    assert bridge.engine.unlinks == []
    bridge.set_outputs("h", ())  # a stinger: Python's compositor draws the canvas again
    assert bridge.engine.unlinks == [(0, 0)] and not rec.linked and rec._last_queued == 19
    for i in range(20, 30):
        push(i)
    rec.stop()
    assert rec.error is None and rec.frames == 30 and rec.dropped == 0
    with av.open(str(tmp_path / "linked.mkv")) as f:
        lumas = [int(fr.to_ndarray()[:H].mean()) for fr in f.decode(video=0)]
    assert len(lumas) == 30 and all(abs(v - (16 + 4 * i)) <= 4 for i, v in enumerate(lumas))


def test_a_paused_or_stopping_recording_is_unlinked(native, tmp_path, monkeypatch):
    from test_native_bridge import _collection

    from sprigcast.render import native_bridge

    monkeypatch.setattr(sprigcore, "Engine", _LinkEngine)
    bridge = native_bridge.NativeCanvases(_collection())
    bridge._slots["h"] = 0
    key = ((W, H), False)
    bridge.set_outputs("h", [key])
    rec = Recorder(tmp_path / "p.mkv", W, H, FPS, 800, "libx264", 0.0, "mkv")
    other = Recorder(tmp_path / "pyav.mkv", W, H, FPS, 800, "libx264", 0.0, "mkv")  # no writer open: Python's
    rec.start()
    assert rec.wait_open()
    taps = [(rec.push_frame, key), (other.push_frame, key)]
    assert bridge.link_taps("h", taps) == {id(rec)}
    assert bridge.engine.links[(0, 0)][4] is True  # the other recording still takes the frames
    rec.pause()
    assert bridge.link_taps("h", taps) == set() and not rec.linked
    rec.resume()
    assert bridge.link_taps("h", taps) == {id(rec)}
    monkeypatch.setenv("SPRIGCAST_NO_LINK", "1")
    assert bridge.link_taps("h", taps) == set()
    monkeypatch.delenv("SPRIGCAST_NO_LINK")
    rec.stop()
    assert bridge.link_taps("h", taps) == set() and not rec.linked and len(bridge.engine.unlinks) == 2
    bridge.fail("test")  # nothing linked is left behind
    assert rec.error is None


class _CardWriter:
    """Stands in for a sprigcore.Writer whose encoder takes the engine's frames on the card."""

    on_card = True

    def stats(self):
        class _S:
            last_pts = 5

        return _S()


def _card_recorder(tmp_path, name="card.mkv"):
    rec = Recorder(tmp_path / name, W, H, FPS, 800, "h264_nvenc", 0.0, "mkv")
    rec.writer, rec._opening = _CardWriter(), False
    return rec


def test_frames_on_the_card_go_only_to_a_card_writer_that_takes_them_alone(native, tmp_path, monkeypatch):
    from test_native_bridge import _collection

    from sprigcast.render import native_bridge

    monkeypatch.setattr(sprigcore, "Engine", _LinkEngine)
    bridge = native_bridge.NativeCanvases(_collection())
    bridge._slots["h"], bridge.can_share = 0, True
    card, memory = ((W, H), True), ((W, H), False)
    bridge.set_outputs("h", [card, memory])
    rec = _card_recorder(tmp_path)
    assert rec.wants_gpu_frames()
    assert bridge.link_taps("h", [(rec.push_frame, card)]) == {id(rec)} and rec.linked
    assert bridge.engine.links[(0, bridge._outputs[0][card])][4] is False  # nothing else takes them
    # A recording through PyAV's writer of the same size can't take card frames from the engine.
    plain = Recorder(tmp_path / "plain.mkv", W, H, FPS, 800, "libx264", 0.0, "mkv")
    plain.writer, plain._opening = object(), False
    rec2 = _card_recorder(tmp_path, "two.mkv")
    rec2.writer = plain.writer
    assert bridge.link_taps("h", [(plain.push_frame, card)]) == set()
    # Two take the same card frames (a stream on SprigCast's own NVENC): the card writer takes memory frames.
    other = Recorder(tmp_path / "stream.mkv", W, H, FPS, 800, "h264_nvenc", 0.0, "mkv")
    assert bridge.link_taps("h", [(rec.push_frame, card), (other.push_frame, card)]) == set()
    assert not rec.linked and not rec.wants_gpu_frames()
    assert bridge.link_taps("h", [(rec.push_frame, memory), (other.push_frame, card)]) == {id(rec)}


def test_a_rebuilt_engine_device_sends_a_card_writer_memory_frames(native, tmp_path, monkeypatch):
    from test_native_bridge import _collection

    from sprigcast.render import native_bridge

    monkeypatch.setattr(sprigcore, "Engine", _LinkEngine)
    bridge = native_bridge.NativeCanvases(_collection())
    bridge._slots["h"], bridge.can_share = 0, True
    card = ((W, H), True)
    bridge.set_outputs("h", [card])
    rec = _card_recorder(tmp_path)
    assert bridge.link_taps("h", [(rec.push_frame, card)]) == {id(rec)}
    bridge._rebuilds += 1  # its textures are another device's now
    assert bridge.link_taps("h", [(rec.push_frame, card)]) == set()
    assert not rec.linked and not rec.wants_gpu_frames() and rec._last_queued == 5


def test_a_refused_card_link_sends_memory_frames_and_keeps_linking(native, tmp_path, monkeypatch):
    from test_native_bridge import _collection

    from sprigcast.render import native_bridge

    class _Refusing(_LinkEngine):
        def link_output(self, index, slot, writer, *a, **k):
            if self.share.get((index, slot)):
                raise sprigcore.NativeError(-1, "frames on the card go only to a writer that takes them there")
            super().link_output(index, slot, writer, *a, **k)

        def set_output(self, index, slot, width, height, scale="bilinear", share=False):
            self.share = getattr(self, "share", {})
            self.share[(index, slot)] = share
            super().set_output(index, slot, width, height, scale, share)

    monkeypatch.setattr(sprigcore, "Engine", _Refusing)
    bridge = native_bridge.NativeCanvases(_collection())
    bridge._slots["h"], bridge.can_share = 0, True
    card, memory = ((W, H), True), ((W, H), False)
    bridge.set_outputs("h", [card, memory])
    rec = _card_recorder(tmp_path)
    assert bridge.link_taps("h", [(rec.push_frame, card)]) == set() and not rec.wants_gpu_frames()
    assert bridge.link_taps("h", [(rec.push_frame, memory)]) == {id(rec)}


def test_own_nvenc_with_no_engine_device_records_through_pyav(native, tmp_path, monkeypatch):
    from sprigcast.render import gpu_share

    monkeypatch.setattr(Recorder, "uses_direct", lambda self: True)
    monkeypatch.setattr(gpu_share, "native_device", lambda: None)
    opened = []
    monkeypatch.setattr(sprigcore, "Writer", lambda *a, **k: opened.append(k))
    rec = Recorder(tmp_path / "x.mkv", W, H, FPS, 800, "h264_nvenc", 0.0, "mkv")
    assert rec.uses_writer() and rec._writer_session() is False and opened == []


def test_own_nvenc_opens_the_writer_on_the_engines_device(native, tmp_path, monkeypatch):
    import ctypes

    from sprigcast.render import gpu_share

    monkeypatch.setattr(Recorder, "uses_direct", lambda self: True)
    monkeypatch.setattr(gpu_share, "native_device", lambda: ctypes.c_void_p(0x1234))
    released, opened = [], []
    monkeypatch.setattr(sprigcore, "com_release", released.append)

    def refuse(*a, **k):
        opened.append(k.get("device"))
        raise sprigcore.NativeError(-2, "no NVENC here")

    monkeypatch.setattr(sprigcore, "Writer", refuse)
    rec = Recorder(tmp_path / "x.mkv", W, H, FPS, 800, "h264_nvenc", 0.0, "mkv")
    assert rec._writer_session() is False  # PyAV records it with SprigCast's own NVENC instead
    assert opened == [0x1234] and released == [0x1234]


class _CameraEngine(_LinkEngine):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.cameras = []

    def link_camera(self, index, slot, name, width, height, keep=False):
        self.cameras.append((index, slot, name, width, height, keep))
        self.links[(index, slot)] = ("camera", name)


def test_the_virtual_camera_is_fed_by_the_engine_itself(monkeypatch):
    """Phase 4: the engine writes the camera's frames into its shared block itself (sc_engine_output_link_camera);
    a recording sharing the output keeps it, and the camera goes back to Python's frames when it stops."""
    from test_native_bridge import _collection

    from sprigcast.output.virtualcam import SharedBlock, VirtualCamera
    from sprigcast.render import native_bridge

    monkeypatch.setattr(sprigcore, "Engine", _CameraEngine)
    monkeypatch.delenv("SPRIGCAST_NO_LINK", raising=False)
    bridge = native_bridge.NativeCanvases(_collection())
    bridge._slots["h"] = 0
    key = ((W, H), False)
    bridge.set_outputs("h", [key])
    cam = VirtualCamera(None, list)
    assert bridge.link_taps("h", [(cam._on_frame, key)]) == set()  # no app watching yet
    cam.block = SharedBlock(0, name="Local\\SprigCastCamera")
    cam.tap = ("h", (W, H), (W + 16, H))
    assert bridge.link_taps("h", [(cam._on_frame, key)]) == {id(cam)} and cam.linked
    assert bridge.engine.cameras == [(0, 0, "Local\\SprigCastCamera", W + 16, H, False)]
    bridge.link_taps("h", [(cam._on_frame, key)])  # nothing changed: not linked again
    assert len(bridge.engine.cameras) == 1
    cam.tap = ("h", (W, H), (W, H + 16))  # the app asked for another size
    bridge.link_taps("h", [(cam._on_frame, key)])
    assert bridge.engine.unlinks == [(0, 0)] and bridge.engine.cameras[-1][3:5] == (W, H + 16) and cam.linked
    cam.block.closed = True  # the app closed the camera
    assert bridge.link_taps("h", [(cam._on_frame, key)]) == set()
    assert bridge.engine.unlinks == [(0, 0), (0, 0)] and not cam.linked


def test_two_card_writers_of_one_size_each_get_their_own_output_on_the_card(native, tmp_path, monkeypatch):
    """A recording beside a stream's encoder of the same size: each is handed its frames on the card by the engine
    (PC 2026-11-18: sharing one output sent the recording through Python, horizontal.mkv at 57.17 fps)."""
    from test_native_bridge import _collection

    from sprigcast.render import native_bridge
    from sprigcast.render.engine import Engine as RenderEngine

    monkeypatch.setattr(sprigcore, "Engine", _LinkEngine)
    bridge = native_bridge.NativeCanvases(_collection())
    bridge._slots["h"], bridge.can_share = 0, True
    rec, stream = _card_recorder(tmp_path), _card_recorder(tmp_path, "stream.mkv")
    for r in (rec, stream):
        monkeypatch.setattr(r, "uses_writer", lambda: True)
    taps = [(rec.push_frame, (W, H)), (stream.push_frame, (W, H))]
    keys = RenderEngine._output_keys(bridge, taps)
    assert keys[0] != keys[1] and all(k[:2] == ((W, H), True) for k in keys)
    bridge.set_outputs("h", keys)
    assert bridge.link_taps("h", [(cb, key) for (cb, _size), key in zip(taps, keys)]) == {id(rec), id(stream)}
    assert rec.wants_gpu_frames() and stream.wants_gpu_frames()  # neither lost the card
    assert all(link[4] is False for link in bridge.engine.links.values()) and len(bridge.engine.links) == 2


def test_more_writers_than_engine_outputs_share_outputs_again(native, tmp_path, monkeypatch):
    from test_native_bridge import _collection

    from sprigcast.render import native_bridge
    from sprigcast.render.engine import Engine as RenderEngine

    monkeypatch.setattr(sprigcore, "Engine", _LinkEngine)
    bridge = native_bridge.NativeCanvases(_collection())
    bridge._slots["h"], bridge.can_share = 0, True
    recs = [_card_recorder(tmp_path, f"r{i}.mkv") for i in range(sprigcore.ENGINE_OUTPUTS + 1)]
    for r in recs:
        monkeypatch.setattr(r, "uses_writer", lambda: True)
    keys = RenderEngine._output_keys(bridge, [(r.push_frame, (W, H)) for r in recs])
    assert set(keys) == {((W, H), True)}  # one shared output, as before, rather than Python drawing
    plain = Recorder(tmp_path / "plain.mkv", W, H, FPS, 800, "libx264", 0.0, "mkv")
    monkeypatch.setattr(plain, "uses_writer", lambda: False)
    assert RenderEngine._output_keys(bridge, [(plain.push_frame, (W, H))]) == [((W, H), False)]


class _Tone:
    """A sound Python makes, as the mixer reads them."""
    id, kind, name = "tone", "audio_input", "Tone"
    volume, muted, monitor, mono, balance, sync_offset_ms = 1.0, False, False, False, 0.0, 0
    tracks = ("stream", "recording")
    audio_chain = None

    def read_audio(self, frames):
        return np.full((frames, 2), 0.1, np.float32)


def _mixed(rec, mixer, frames=45, pause_at=None):
    """A recording fed by a hand-ticked mixer, a block of sound per frame."""
    start = rec.start_time
    for i in range(frames):
        rec.push_frame(Frame("h", W, H, _nv12(i), start + i / FPS, fmt="nv12"))
        if pause_at is not None and i == pause_at[0]:
            rec.pause()
        if pause_at is not None and i == pause_at[1]:
            rec.resume()
        mixer.tick()
        time.sleep(0.005)  # the writer's thread takes what is queued
    rec.stop()


@pytest.mark.parametrize("off", [False, True])
def test_sound_reaches_the_writer_from_the_engines_mixer_without_python(native, tmp_path, monkeypatch, off):
    from sprigcast.audio.native_mixer import NativeMixer

    if off:
        monkeypatch.setenv("SPRIGCAST_NO_NATIVE_SOUND", "1")
    path = tmp_path / "mixed.mkv"
    rec = Recorder(path, W, H, FPS, 800, "libx264", 1000.0, "mkv", tracks=("stream", "recording"))
    handed = []
    queued = rec._audio.put_nowait
    monkeypatch.setattr(rec._audio, "put_nowait", lambda block: (handed.append(1), queued(block))[1])
    mixer = NativeMixer(lambda levels: None, clocked=False)
    try:
        mixer.set_sources([_Tone()])
        mixer.add_tap(rec.push_audio, rec.tracks)
        rec.start()
        assert rec.wait_open()
        _mixed(rec, mixer, pause_at=None if off else (20, 30))
        assert rec.error is None
    finally:
        mixer.remove_tap(rec.push_audio)
        mixer._engine.close()
    blocks = 45 if off else 35  # 10 of them fell into the pause
    if off:
        assert rec._sound_tap is None and len(handed) == 45
    else:
        assert rec._sound_tap is not None and len(handed) <= 3  # only until the engine took over
    seconds = []
    for i in (0, 1):
        with av.open(str(path)) as f:
            assert len(f.streams.audio) == 2
            seconds.append(sum(fr.samples for fr in f.decode(audio=i)) / 48000)
    assert abs(seconds[0] - blocks * 1024 / 48000) < 0.07 and seconds[0] == seconds[1], seconds


def test_nvenc_with_b_frames_opens_on_the_engines_device_and_takes_memory_frames(tmp_path, monkeypatch):
    """PC 2026-11-21: SprigCast's own memory grew about 490 MB in the 10 s after Twitch Enhanced Broadcasting's two
    encoders started. Twitch asks for B-frames and look-ahead, so they were FFmpeg's NVENC opened with no device,
    which makes a CUDA context each. On the engine's device there is none; frames still come from memory."""
    from ctypes import c_void_p

    from sprigcast.core.scene import EncoderSettings
    from sprigcast.render import gpu_share

    monkeypatch.delenv("SPRIGCAST_NO_CARD_UPLOAD", raising=False)
    monkeypatch.delenv("SPRIGCAST_CPU_CONVERT", raising=False)
    monkeypatch.setattr(gpu_share, "native_device", lambda: c_void_p(1234))
    twitch = EncoderSettings(keyint_sec=2, b_frames=2)
    rec = Recorder(tmp_path / "t.mkv", W, H, FPS, 800, "h264_nvenc", 0.0, "mkv", encoder=twitch)
    assert not rec.uses_direct()
    assert rec._card_device().value == 1234 and rec.card_upload
    rec.writer, rec._opening = _CardWriter(), False
    assert not rec.wants_gpu_frames()  # the engine's textures never go to it: look-ahead holds more than its pool
    # Not for another encoder, with the off switch, or after it couldn't start there.
    x264 = Recorder(tmp_path / "x.mkv", W, H, FPS, 800, "libx264", 0.0, "mkv", encoder=twitch)
    assert x264._card_device() is None and not x264.card_upload
    monkeypatch.setenv("SPRIGCAST_NO_CARD_UPLOAD", "1")
    off = Recorder(tmp_path / "o.mkv", W, H, FPS, 800, "h264_nvenc", 0.0, "mkv", encoder=twitch)
    assert off._card_device() is None and not off.card_upload
    monkeypatch.delenv("SPRIGCAST_NO_CARD_UPLOAD")
    refused = Recorder(tmp_path / "r.mkv", W, H, FPS, 800, "h264_nvenc", 0.0, "mkv", encoder=twitch)
    refused._no_card_upload = True
    assert refused._card_device() is None
    monkeypatch.setattr(gpu_share, "native_device", lambda: None)  # the engine can't share its device
    none = Recorder(tmp_path / "n.mkv", W, H, FPS, 800, "h264_nvenc", 0.0, "mkv", encoder=twitch)
    assert none._card_device() is None and not none.card_upload and not none._card_lost


def test_an_encoder_that_wont_start_on_the_engines_device_starts_on_its_own(native, tmp_path, monkeypatch, caplog):
    from ctypes import c_void_p

    from sprigcast.core.scene import EncoderSettings
    from sprigcast.render import gpu_share

    monkeypatch.delenv("SPRIGCAST_NO_CARD_UPLOAD", raising=False)
    monkeypatch.setattr(gpu_share, "native_device", lambda: c_void_p(1234))
    monkeypatch.setattr(sprigcore, "com_release", lambda pointer: None)
    opened = []

    class _Stats:
        def __init__(self, failed):
            self.state_name = "failed" if failed else "running"
            self.error_text = "the video encoder h264_nvenc (frames on the card): Invalid argument" if failed else ""

    class _Writer:
        on_card = False

        def __init__(self, *args, device=None, **kwargs):
            self.device = device
            opened.append(device)

        def stats(self):
            return _Stats(self.device is not None)

        def close(self):
            pass

    class _Stop(Exception):
        pass

    monkeypatch.setattr(sprigcore, "Writer", _Writer)
    rec = Recorder(tmp_path / "t.mkv", W, H, FPS, 800, "h264_nvenc", 0.0, "mkv",
                   encoder=EncoderSettings(keyint_sec=2, b_frames=2))
    monkeypatch.setattr(Recorder, "_opened", property(lambda self: (_ for _ in ()).throw(_Stop())), raising=False)
    with caplog.at_level(logging.WARNING), pytest.raises(_Stop):
        rec._writer_session()
    assert opened == [1234, None]  # once on the engine's device, then on a device of its own
    assert not rec.card_upload and not rec._card_lost and rec._no_card_upload
    assert "couldn't start on the engine's graphics device" in caplog.text
