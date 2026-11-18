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
    assert not rec.uses_writer()
    from sprigcast.output.replay import ReplayBuffer
    from sprigcast.output.stream import Streamer

    assert not ReplayBuffer.allow_writer and not Streamer.allow_writer and recorder_module.Recorder.allow_writer
