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
