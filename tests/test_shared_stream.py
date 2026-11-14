import time

import av
import numpy as np
import pytest

from sprigcast.output import shared_stream, stream as stream_mod
from sprigcast.output.shared_stream import SharedStreamEncoder, StreamLink, share_key
from sprigcast.output.stream import StreamSession, StreamTarget
from sprigcast.render.engine import Frame

from test_stream import free_port, needs_ffmpeg, rtmp_server, wait_for  # noqa: E402

W, H = 160, 90


class _Canvas:
    def __init__(self, cid="horizontal", w=W, h=H, fps=30):
        self.id, self.width, self.height, self.fps = cid, w, h, fps


def _feed(enc, seconds, fps=30):
    data = bytes(W * H * 4)
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        enc.push_frame(Frame("horizontal", W, H, data, time.perf_counter()))
        enc.push_audio(np.zeros((1600, 2), dtype=np.float32))
        time.sleep(1 / fps)


def _target(name, canvas, kbps=1000, **kw):
    return StreamTarget(name, canvas, "rtmp://x/app", "key", kbps, output_id=name, **kw)


def test_only_matching_streams_share():
    c, v = _Canvas(), _Canvas("vertical", H, W)
    a, b = share_key(_target("tw", c)), share_key(_target("yt", c))
    assert a == b
    assert share_key(_target("kick", c, kbps=2000)) != a  # another bitrate
    assert share_key(_target("tt", v)) != a  # another canvas
    assert share_key(_target("x", c, size=(80, 46))) != a
    assert share_key(_target("e", c, enhanced=object())) is None


def test_one_encoder_feeds_two_files_each_from_a_keyframe(tmp_path):
    enc = SharedStreamEncoder(_Canvas(), 30, 800, "libx264", time.perf_counter())
    paths = [tmp_path / "a.flv", tmp_path / "b.flv"]
    links = [StreamLink(p.stem, "unused", "key", enc, open_container=lambda u, p=p: av.open(str(p), "w", format="flv"))
             for p in paths]
    enc.links += links
    enc.start()
    links[0].start()
    try:
        _feed(enc, 1.0)
        links[1].start()  # joins late: starts at a fresh keyframe
        _feed(enc, 1.5)
        assert all(link.connected for link in links)
    finally:
        for link in links:
            link.stop()
        enc.stop()
    counts = []
    for p in paths:
        with av.open(str(p)) as f:
            assert sorted(s.type for s in f.streams) == ["audio", "video"]
            keys = [packet.is_keyframe for packet in f.demux(video=0) if packet.size]
            assert keys[0]  # each destination starts at a keyframe
            counts.append(len(keys))
    assert counts[0] > counts[1] > 10  # the late one has less, but plays from its first frame
    assert enc.frames > 50 and links[0].frames > 50


def test_a_slow_link_skips_without_holding_up_the_other(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_stream, "MAX_BACKLOG", 0.3)
    monkeypatch.setattr(shared_stream, "RESUME_BACKLOG", 0.1)

    class Slow:
        def __init__(self, path):
            self.inner = av.open(str(path), "w", format="flv")

        def add_stream_from_template(self, t):
            return self.inner.add_stream_from_template(t)

        def mux(self, packet):
            time.sleep(0.2)  # far slower than 30 packets a second
            self.inner.mux(packet)

        def close(self):
            self.inner.close()

    enc = SharedStreamEncoder(_Canvas(), 30, 800, "libx264", time.perf_counter())
    fast = StreamLink("fast", "u", "k", enc, open_container=lambda u: av.open(str(tmp_path / "f.flv"), "w", format="flv"))
    slow = StreamLink("slow", "u", "k", enc, open_container=lambda u: Slow(tmp_path / "s.flv"))
    enc.links += [fast, slow]
    enc.start()
    fast.start()
    slow.start()
    try:
        _feed(enc, 3.0)
    finally:
        slow.stop(timeout=10)
        fast.stop()
        enc.stop()
    assert slow.dropped > 0 and fast.dropped == 0
    assert fast.frames > 60 and enc.dropped == 0
    assert slow.health()[0] in ("warning", "bad") or not slow.connected


def test_a_link_that_cant_connect_retries_and_never_shows_the_key(monkeypatch):
    monkeypatch.setattr(shared_stream, "RETRY_FIRST", 0.2)

    def refuse(url):
        raise OSError(f"Connection refused: {url}")

    enc = SharedStreamEncoder(_Canvas(), 30, 800, "libx264", time.perf_counter())
    link = StreamLink("down", "rtmp://x/app", "live_SECRET", enc, open_container=refuse)
    enc.links.append(link)
    enc.start()
    link.start()
    try:
        _feed(enc, 0.6)
        assert wait_for(lambda: link.reconnects >= 1)
        assert "disconnected" in link.status and "SECRET" not in link.status
        assert link.summary().startswith("down: never connected")
    finally:
        link.stop()
        enc.stop()


class _Mixer:
    def __init__(self):
        self.taps = []

    def add_tap(self, f):
        self.taps.append(f)

    def remove_tap(self, f):
        self.taps.remove(f)


class _Engine:
    def __init__(self):
        self.mixer, self.frame_taps = _Mixer(), []

    def add_frame_tap(self, cid, cb, *size):
        self.frame_taps.append((cid, cb))

    def remove_frame_tap(self, cid, cb):
        self.frame_taps.remove((cid, cb))


@needs_ffmpeg
def test_session_shares_one_encoder_between_two_servers(tmp_path):
    ports = [free_port(), free_port()]
    outs = [tmp_path / "one.flv", tmp_path / "two.flv"]
    servers = [rtmp_server(p, o) for p, o in zip(ports, outs)]
    time.sleep(0.5)
    c, v = _Canvas(), _Canvas("vertical", H, W)
    targets = [StreamTarget(f"Local {i}", c, f"rtmp://127.0.0.1:{p}/app", "secretkey", 800, output_id=f"o{i}")
               for i, p in enumerate(ports)]
    targets.append(StreamTarget("Alone", v, "rtmp://127.0.0.1:1/app", "secretkey", 800, output_id="alone"))
    engine = _Engine()
    session = StreamSession.start(engine, targets, "libx264")
    try:
        assert len(session.shared) == 1 and len(session.streamers) == 3
        assert len(engine.frame_taps) == 2  # one shared encoder for horizontal, one Streamer for vertical
        enc = session.shared[0]
        end = time.time() + 6
        while time.time() < end and not all(st.connected for _, st in session.streamers[:2]):
            _feed(enc, 0.2)
        _feed(enc, 1.0)
        assert all(st.connected for _, st in session.streamers[:2])
        assert isinstance(session.streamers[2][1], stream_mod.Streamer)
    finally:
        session.stop()
        for s in servers:
            s.wait(timeout=10)
    assert engine.frame_taps == [] and engine.mixer.taps == []
    for out in outs:
        with av.open(str(out)) as f:
            assert sum(1 for _ in f.decode(video=0)) > 10
