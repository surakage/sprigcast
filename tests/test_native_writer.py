"""The engine's own writer (native/core/src/writer.cpp): NV12 pictures and stereo sound in, an encoded and muxed
file out, all on a thread of the engine's. Built with the media player on Linux (tests/test_native_media.py)."""

from __future__ import annotations

import time

import av
import numpy as np
import pytest

from sprigcast import sprigcore
from test_native_media import player_lib  # noqa: F401 (the fixture that builds or finds the library)

W, H = 64, 48


def nv12(level: int) -> np.ndarray:
    picture = np.full((H * 3 // 2, W), 128, np.uint8)
    picture[:H] = level
    return picture


def finished(writer, seconds=20.0):
    writer.finish()
    assert writer.wait(seconds)
    return writer.stats()


def test_a_recording_has_every_picture_and_its_sound(player_lib, tmp_path):  # noqa: F811
    path = tmp_path / "out.mkv"
    writer = sprigcore.Writer(str(path), "libx264", W, H, fps=30, video_kbps=500, keyframe_frames=30,
                              video_options={"preset": "ultrafast"}, audio_tracks=2, queue_frames=120)
    try:
        tone = (0.2 * np.sin(np.arange(1600) / 10.0)).astype(np.float32)
        for n in range(60):
            assert writer.video(nv12(16 + 3 * n), n) == sprigcore.SC_OK
            for track in (0, 1):
                assert writer.audio(np.stack([tone, tone], axis=1), track) == sprigcore.SC_OK
        stats = finished(writer)
        assert stats.state_name == "finished", stats.error_text
        assert stats.frames_written == 60 and stats.frames_dropped == 0 and stats.bytes > 0
    finally:
        writer.close()
    with av.open(str(path)) as f:
        video = f.streams.video[0]
        assert (video.width, video.height) == (W, H)
        assert len(f.streams.audio) == 2
        frames = list(f.decode(video=0))
    assert len(frames) == 60
    lumas = [int(frame.to_ndarray()[:H].mean()) for frame in (frames[0], frames[-1])]  # the Y plane as written
    assert abs(lumas[0] - 16) <= 4 and abs(lumas[1] - (16 + 3 * 59)) <= 4  # the pictures, in order
    with av.open(str(path)) as f:
        seconds = sum(frame.samples for frame in f.decode(audio=0)) / 48000
    assert abs(seconds - 60 * 1600 / 48000) < 0.1  # the sound, all of it


def test_a_full_queue_drops_and_counts_and_a_bad_encoder_fails_without_harm(player_lib, tmp_path):  # noqa: F811
    writer = sprigcore.Writer(str(tmp_path / "slow.mp4"), "libx264", W, H, video_options={"preset": "veryslow"},
                              audio_tracks=0, queue_frames=2)
    try:
        results = [writer.video(nv12(100), n) for n in range(40)]
        assert sprigcore.WRITER_DROPPED in results
        stats = finished(writer)
        assert stats.state_name == "finished", stats.error_text
        assert stats.frames_written + stats.frames_dropped == 40 and stats.frames_dropped > 0
    finally:
        writer.close()
    broken = sprigcore.Writer(str(tmp_path / "x.mp4"), "no_such_encoder", W, H)
    try:
        assert broken.wait(10)
        stats = broken.stats()
        assert stats.state_name == "failed" and "no_such_encoder" in stats.error_text
        assert broken.video(nv12(1), 0) == sprigcore.WRITER_REFUSED
    finally:
        broken.close()
    unwritable = sprigcore.Writer(str(tmp_path / "no" / "such" / "folder" / "x.mkv"), "libx264", W, H)
    try:
        assert unwritable.wait(10) and unwritable.stats().state_name == "failed"
    finally:
        unwritable.close()


def test_closing_without_finishing_gives_up_at_once(player_lib, tmp_path):  # noqa: F811
    writer = sprigcore.Writer(str(tmp_path / "gone.mkv"), "libx264", W, H, video_options={"preset": "ultrafast"})
    for n in range(5):
        writer.video(nv12(50), n)
    began = time.monotonic()
    writer.close()
    assert time.monotonic() - began < 6
    with pytest.raises(sprigcore.NativeError):
        writer.video(nv12(50), 6)
