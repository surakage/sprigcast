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

    def add_tap(self, cb):
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
