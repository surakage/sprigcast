import time

import av
import numpy as np
import pytest

from sprigcast.output.replay import ReplayBuffer, ReplaySession
from sprigcast.render.engine import Frame

pytestmark = pytest.mark.skipif("libx264" not in av.codecs_available, reason="needs libx264 in PyAV")


def _feed(buf, start, seconds, fps=30, w=160, h=90):
    for i in range(int(seconds * fps)):
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        rgba[..., 1] = (i * 4) % 256
        rgba[..., 3] = 255
        buf.push_frame(Frame("horizontal", w, h, rgba.tobytes(), start + i / fps))
        buf.push_audio(np.full((1600, 2), 0.05, dtype=np.float32))  # 1/30 s of audio per frame: real time
        time.sleep(0.002)


def test_buffer_keeps_only_the_last_seconds_and_saves_a_playable_clip(tmp_path):
    start = 500.0
    buf = ReplayBuffer("horizontal", 160, 90, 30, 1000, "libx264", start, seconds=2.0)
    buf.start()
    _feed(buf, start, 6.0)
    time.sleep(0.3)
    assert 1.0 <= buf.buffered_seconds <= 2.1  # older seconds were dropped
    path = buf.save(tmp_path / "clip.mkv")
    first = path.stat().st_size
    buf.save(tmp_path / "again.mkv")  # saving twice works: the buffer isn't used up
    buf.stop()
    assert buf.error is None
    with av.open(str(path)) as f:
        assert sorted(s.type for s in f.streams) == ["audio", "video"]
        frames = sum(1 for _ in f.decode(video=0))
    assert 25 <= frames <= 63  # between the GOP-trimmed and full length at 30 fps
    assert (tmp_path / "again.mkv").stat().st_size > 0 and first > 0


def test_save_before_anything_is_buffered_says_so(tmp_path):
    buf = ReplayBuffer("horizontal", 64, 36, 30, 500, "libx264", 1.0, seconds=5)
    buf.start()
    with pytest.raises(RuntimeError, match="nothing buffered"):
        buf.save(tmp_path / "x.mkv", timeout=5)
    buf.stop()


class FakeEngine:
    def __init__(self):
        self.taps = {}
        self.mixer = self

    def add_frame_tap(self, cid, cb):
        self.taps.setdefault(cid, []).append(cb)

    def remove_frame_tap(self, cid, cb):
        self.taps[cid].remove(cb)

    def add_tap(self, cb):
        self.audio = getattr(self, "audio", []) + [cb]

    def remove_tap(self, cb):
        self.audio.remove(cb)


def test_session_saves_one_clip_per_canvas_and_detaches(tmp_path):
    class C:
        def __init__(self, cid, w, h):
            self.id, self.width, self.height, self.fps = cid, w, h, 30

    engine = FakeEngine()
    session = ReplaySession.start(engine, [C("horizontal", 64, 36), C("vertical", 36, 64)], tmp_path, 10, 800,
                                  codec="libx264")
    t = time.perf_counter()
    for i in range(40):
        for cid, (w, h) in (("horizontal", (64, 36)), ("vertical", (36, 64))):
            for cb in engine.taps[cid]:
                cb(Frame(cid, w, h, bytes(w * h * 4), t + 0.01 + i / 30))
        for cb in engine.audio:
            cb(np.zeros((1600, 2), dtype=np.float32))
        time.sleep(0.002)
    time.sleep(0.3)
    paths = session.save()
    session.stop()
    assert sorted(p.name.split()[-1] for p in paths) == ["horizontal.mkv", "vertical.mkv"]
    assert all(p.name.startswith("SprigCast Replay ") for p in paths)
    assert session.errors == [] and all(not taps for taps in engine.taps.values()) and engine.audio == []
