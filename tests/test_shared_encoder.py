import threading
import time

import av
import numpy as np
import pytest

from sprigcast.output.shared import EncoderPool, SharedRecording, SharedReplay
from sprigcast.render.engine import Frame

pytestmark = pytest.mark.skipif("libx264" not in av.codecs_available, reason="needs libx264 in PyAV")


class FakeEngine:
    def __init__(self):
        self.taps, self.audio = {}, []
        self.mixer = self

    def add_frame_tap(self, cid, cb, nv12_size=None):
        self.taps.setdefault(cid, []).append(cb)

    def remove_frame_tap(self, cid, cb):
        self.taps[cid].remove(cb)

    def add_tap(self, cb, tracks=("stream",)):
        self.audio.append(cb)

    def remove_tap(self, cb):
        self.audio.remove(cb)


class C:
    def __init__(self, cid, w, h):
        self.id, self.width, self.height, self.fps = cid, w, h, 30


CANVASES = [C("horizontal", 64, 36), C("vertical", 36, 64)]


def _feed(engine, start, frames, first=0, pool=None):
    for i in range(first, first + frames):
        # Faster than real time, so let the encoders keep up: these tests are about what lands in files.
        while pool is not None and any(enc._video.qsize() > 5 for enc in list(pool.encoders.values())):
            time.sleep(0.002)
        for c in CANVASES:
            for cb in engine.taps.get(c.id, []):
                cb(Frame(c.id, c.width, c.height, bytes(c.width * c.height * 4), start + 0.01 + i / 30))
        for cb in engine.audio:
            cb(np.zeros((1600, 2), dtype=np.float32))
        time.sleep(0.005)
    time.sleep(0.3)


def _frames(path):
    with av.open(str(path)) as f:
        return sum(1 for _ in f.decode(video=0))


def test_recording_and_replay_share_one_encoder_per_canvas(tmp_path):
    engine = FakeEngine()
    pool = EncoderPool(engine)
    replay = SharedReplay.start(pool, CANVASES, tmp_path, 10, 800, codec="libx264")
    t = time.perf_counter()  # after the start: frames stamped before an encoder starts are skipped
    _feed(engine, t, 30, pool=pool)
    rec = SharedRecording.start(pool, CANVASES, tmp_path, 800, codec="libx264")
    # Still one encoder (and one tap) per canvas
    assert len(pool.encoders) == 2 and all(len(engine.taps[c.id]) == 1 for c in CANVASES) and len(engine.audio) == 2
    _feed(engine, t, 45, first=30, pool=pool)
    # The replay buffer keeps the encoder running, so frames keep coming while the file finishes
    feeder = threading.Thread(target=_feed, args=(engine, t, 60, 75, pool))
    feeder.start()
    rec.stop()
    feeder.join()
    assert rec.errors == []
    for path in rec.paths:
        n = _frames(path)
        assert 44 <= n <= 65, n  # keyframe asked for at the start to the one at the end (+ frames already queued)
    clips = replay.save()
    assert all(_frames(p) >= 120 for p in clips)  # the replay spans before, during and after the recording
    replay.stop()
    assert pool.encoders == {} and all(not taps for taps in engine.taps.values()) and engine.audio == []


def test_recording_alone_then_again(tmp_path):
    engine = FakeEngine()
    pool = EncoderPool(engine)
    rec = SharedRecording.start(pool, CANVASES, tmp_path / "a", 800, codec="libx264")
    t = time.perf_counter()  # after the start: frames stamped before an encoder starts are skipped
    _feed(engine, t, 20, pool=pool)
    rec.stop()
    assert pool.encoders == {}
    assert [_frames(p) for p in rec.paths] == [20, 20]
    rec2 = SharedRecording.start(pool, CANVASES[:1], tmp_path / "b", 800, codec="libx264")
    _feed(engine, time.perf_counter(), 10, pool=pool)
    rec2.stop()
    assert rec2.errors == [] and _frames(rec2.paths[0]) == 10, _frames(rec2.paths[0])


def test_recording_can_be_saved_smaller_than_the_canvas(tmp_path):
    engine = FakeEngine()
    pool = EncoderPool(engine)
    rec = SharedRecording.start(pool, CANVASES, tmp_path, 800, codec="libx264", short_side=18)
    assert {cid: (e.out_width, e.out_height) for cid, e in pool.encoders.items()} == {
        "horizontal": (32, 18), "vertical": (18, 32)}
    _feed(engine, time.perf_counter(), 10, pool=pool)
    rec.stop()
    assert rec.errors == []
    for path in rec.paths:
        with av.open(str(path)) as f:
            stream = f.streams.video[0]
            assert sorted((stream.width, stream.height)) == [18, 32]


class TrackEngine(FakeEngine):
    """Feeds each tap the tracks it asked for: recording 0.5, voice 0.25, clips 0.125 (constant tones)."""

    LEVEL = {"stream": 0.0625, "recording": 0.5, "voice": 0.25, "clips": 0.125}

    def add_tap(self, cb, tracks=("stream",)):
        def feed(_block, cb=cb, tracks=tracks):
            blocks = tuple(np.full((1600, 2), self.LEVEL[t], dtype=np.float32) for t in tracks)
            cb(blocks[0] if len(blocks) == 1 else blocks)
        feed.cb = cb
        self.audio.append(feed)

    def remove_tap(self, cb):
        self.audio = [f for f in self.audio if f.cb != cb]


def _audio_tracks(path):
    with av.open(str(path)) as f:
        out = []
        for stream in f.streams.audio:
            f.seek(0)
            frames = [fr.to_ndarray() for fr in f.decode(stream)]
            level = float(np.median(np.abs(np.concatenate(frames, axis=1)))) if frames else 0.0
            out.append((stream.metadata.get("title", ""), round(level, 2)))
        return out


def test_recordings_carry_a_voice_track_and_clips_their_own_sound(tmp_path):
    engine = TrackEngine()
    pool = EncoderPool(engine)
    pool.voice_track = True
    replay = SharedReplay.start(pool, CANVASES[:1], tmp_path, 10, 800, codec="libx264")
    rec = SharedRecording.start(pool, CANVASES[:1], tmp_path, 800, codec="libx264")
    assert pool.encoders["horizontal"].tracks == ("recording", "voice", "clips")
    _feed(engine, time.perf_counter(), 40, pool=pool)
    clip = replay.save()[0]
    feeder = threading.Thread(target=_feed, args=(engine, time.perf_counter(), 20, 40, pool))
    feeder.start()
    rec.stop()
    feeder.join()
    replay.stop()
    assert rec.errors == []
    assert _audio_tracks(rec.paths[0]) == [("Recording", 0.5), ("Voice", 0.25)]
    assert _audio_tracks(clip) == [("", 0.12)]


def test_without_the_voice_track_a_recording_has_one_sound(tmp_path):
    engine = TrackEngine()
    pool = EncoderPool(engine)
    rec = SharedRecording.start(pool, CANVASES[:1], tmp_path, 800, codec="libx264")
    _feed(engine, time.perf_counter(), 20, pool=pool)
    rec.stop()
    assert [level for _, level in _audio_tracks(rec.paths[0])] == [0.5]
