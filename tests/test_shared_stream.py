import time

import av
import numpy as np
import pytest

from sprigcast.output import shared_stream, stream as stream_mod
from sprigcast.output.shared_stream import SharedStreamEncoder, StreamLink, bitrate_groups, share_key
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
    assert share_key(_target("kick", c, kbps=2000)) == a  # the same picture: the bitrate decides the encoder
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
        assert wait_for(lambda: "disconnected" in link.status)  # written a moment after the count goes up
        assert "SECRET" not in link.status
        assert link.summary().startswith("down: never connected")
    finally:
        link.stop()
        enc.stop()


class _Mixer:
    def __init__(self):
        self.taps = []

    def add_tap(self, f, tracks=("stream",)):
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


class _Fake:
    def __init__(self, output_id, shared=None):
        self.output_id, self.shared, self.stopped = output_id, shared, False
        self.links = []

    def push_frame(self, frame):
        pass

    def push_audio(self, chunk):
        pass

    def stop(self, timeout=5):
        self.stopped = True


def test_session_drops_and_adds_one_place_while_live(monkeypatch):
    """place.stop / place.start: one place ends or joins, the rest keep going, a shared encoder goes with its last link."""
    engine = _Engine()
    enc = _Fake("enc")
    a, b, alone = _Fake("a", enc), _Fake("b", enc), _Fake("alone")
    enc.links = [a, b]
    taps = [("horizontal", enc.push_frame), ("vertical", alone.push_frame)]
    session = StreamSession(engine, [("horizontal", a), ("horizontal", b), ("vertical", alone)])
    session.taps, session.shared = list(taps), [enc]
    session.owned = {id(enc): [taps[0]], id(alone): [taps[1]]}
    for cid, cb in taps:
        engine.add_frame_tap(cid, cb)
    for st in (a, b, alone, enc):
        engine.mixer.add_tap(st.push_audio)
    assert session.drop("a") and enc.links == [b]
    assert ("horizontal", enc.push_frame) in engine.frame_taps  # b still needs the encoder
    assert session.drop("b")
    assert wait_for(lambda: a.stopped and b.stopped and enc.stopped)
    assert engine.frame_taps == [("vertical", alone.push_frame)] and session.shared == []
    assert engine.mixer.taps == [alone.push_audio]
    assert not session.drop("a")
    late = _Fake("late")
    monkeypatch.setattr(StreamSession, "_one", staticmethod(lambda t, *rest: (late, [("horizontal", late.push_frame)])))
    c = _Canvas()
    assert session.add(StreamTarget("Late", c, "rtmp://x/app", "k", 800, output_id="late"), "libx264") is late
    assert session.find("late") is late and ("horizontal", late.push_frame) in engine.frame_taps
    session.stop()
    assert engine.frame_taps == [] and engine.mixer.taps == []


def test_ending_a_stream_from_the_window_doesnt_wait_for_the_encoders():
    """Joining the encoders on the window's thread froze it 637 ms on the PC (2026-11-16)."""
    import threading
    import time

    from sprigcast.output.stream import StreamSession

    release = threading.Event()

    class Slow:
        def push_frame(self, frame):
            pass

        def push_audio(self, block):
            pass

        def stop(self, timeout=5):
            release.wait(5)

    class Mixer:
        def remove_tap(self, fn):
            pass

    class Engine:
        mixer = Mixer()

        def remove_frame_tap(self, canvas_id, fn):
            pass

    session = StreamSession(Engine(), [("h", Slow())])
    began = time.perf_counter()
    session.stop(wait=False)
    assert time.perf_counter() - began < 1.0 and not session.finished(0.05)
    release.set()
    assert session.finished(5)


def _feed_both(enc, engine, seconds, fps=30):
    """Frames and sound to the stream's encoder and to whatever the pool tapped (test_shared_encoder FakeEngine)."""
    data = bytes(W * H * 4)
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        frame = Frame("horizontal", W, H, data, time.perf_counter())
        block = np.zeros((1600, 2), dtype=np.float32)
        for target in [enc, *[cb.__self__ for cb in engine.taps.get("horizontal", [])]]:
            target.push_frame(frame)
        for cb in [enc.push_audio, *engine.audio]:
            cb(block)
        time.sleep(1 / fps)


def test_clips_started_while_live_keep_their_seconds_in_the_streams_encoder(tmp_path):
    from test_shared_encoder import FakeEngine

    from sprigcast.output.shared import EncoderPool, SharedReplay

    enc = SharedStreamEncoder(_Canvas(), 30, 800, "libx264", time.perf_counter())
    path = tmp_path / "live.flv"
    link = StreamLink("tw", "unused", "key", enc, open_container=lambda u: av.open(str(path), "w", format="flv"))
    enc.links.append(link)
    enc.start()
    link.start()
    pool = EncoderPool(FakeEngine())
    replay = SharedReplay.start(pool, [_Canvas()], tmp_path, 1.0, 800, container="mkv", codec="libx264",
                                stream_encoders={"horizontal": enc})
    try:
        assert replay.buffers == [enc] and not pool.encoders and "replay" in enc.holders and enc.seconds == 1.0
        _feed(enc, 2.5)
        clip = replay.save()[0]
        assert clip.suffix == ".mkv" and "horizontal" in clip.stem
        with av.open(str(clip)) as f:
            assert f.format.name.startswith("matroska") and sum(1 for _ in f.decode(video=0)) >= 25
        assert replay.stop() is None  # the stream still has it: nothing to stop
        assert "replay" not in enc.holders and enc.seconds == 0.0 and enc.running
        _feed(enc, 0.3)
    finally:
        link.stop()
        enc.stop()
    with av.open(str(path)) as f:  # the stream's own packets were never disturbed by the clip ring file
        assert sum(1 for _ in f.decode(video=0)) > 60


def test_clips_move_to_the_streams_encoder_once_it_holds_as_much_and_back_after(tmp_path):
    from test_shared_encoder import FakeEngine

    from sprigcast.output.shared import EncoderPool, SharedReplay

    engine = FakeEngine()
    pool = EncoderPool(engine)
    replay = SharedReplay.start(pool, [_Canvas()], tmp_path, 1.0, 800, codec="libx264")
    own = replay.buffers[0]
    enc = SharedStreamEncoder(_Canvas(), 30, 800, "libx264", time.perf_counter())
    enc.start()
    try:
        _feed_both(enc, engine, 1.5)
        replay.follow({"horizontal": enc})  # going live: the stream's encoder fills up first
        assert replay.buffers == [own] and "replay" in enc.holders and enc.seconds == 1.0
        _feed_both(enc, engine, 2.5)
        replay.follow({"horizontal": enc})
        assert replay.buffers == [enc] and not pool.encoders  # one encoder for the canvas now
        deadline = time.monotonic() + 10
        while own.running and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not own.running  # stopped on a thread of its own
        assert replay.save()[0].exists()
        # The stream ends: clips go back to an encoder of their own, the stream's lets go once that is full.
        enc.holders.discard("stream")
        replay.follow({})
        assert replay.buffers == [enc] and len(pool.encoders) == 1
        _feed_both(enc, engine, 2.5)
        replay.follow({})
        assert replay.buffers[0] is pool.encoders["horizontal"] and "replay" not in enc.holders
        assert enc.seconds == 0.0
        replay.stop(wait=True)
        assert not pool.encoders
    finally:
        enc.stop()


def test_close_bitrates_share_one_encoder_within_every_services_limit():
    """PC 2026-11-18: Twitch 6000, YouTube 9000 and Kick 8000 each had an encoder (the video engine at 90%);
    the 2026-11-19 soak still gave Twitch its own (80% with five encodes)."""
    c = _Canvas()
    tw = _target("tw", c, kbps=6000, max_kbps=6000)
    yt = _target("yt", c, kbps=9000, max_kbps=20000)
    kick = _target("kick", c, kbps=8000, max_kbps=8000)
    groups = bitrate_groups([tw, yt, kick])
    assert [(kbps, [t.name for t in members]) for kbps, _auto, members in groups] == \
        [(6000, ["yt", "kick", "tw"])]  # one encoder, at Twitch's limit
    same = [_target(n, c, kbps=6000, max_kbps=6000 if n == "tw" else 0) for n in ("tw", "yt", "kick")]
    assert [len(m) for _, _, m in bitrate_groups(same)] == [3]  # the same bitrate: one encoder for all
    far = [_target("a", c, kbps=2500), _target("b", c, kbps=9000)]
    assert len(bitrate_groups(far)) == 2  # too far apart: the lower one would look much worse at the other's
    auto = [_target("a", c, kbps=6000, auto_bitrate=True), _target("b", c, kbps=6000)]
    assert bitrate_groups(auto)[0][1] is False  # auto-pilot steers a shared encoder only if every place asks


class _Link:
    def __init__(self, name, behind):
        self.name, self.behind = name, behind

    def backlog_seconds(self):
        return self.behind


def test_one_slow_destination_costs_the_others_one_small_step_at_most():
    """2026-11-19 soak: Kick alone behind (0.41 s, YouTube 0) lowered the shared bitrate for everyone."""
    enc = SharedStreamEncoder(_Canvas(), 30, 6000, "libx264", time.perf_counter(), auto_bitrate=True)
    yt, kick, tw = _Link("yt", 0.0), _Link("kick", 3.0), _Link("tw", 0.05)
    pilot = enc.autopilot
    links = [(lk.behind, lk) for lk in (yt, kick, tw)]
    behind, who = enc.steering_backlog(links)
    assert who is kick and behind <= 1.5  # one small step for Kick alone, never a big one
    assert pilot.update(100.0, behind) == 4800
    behind, who = enc.steering_backlog(links)
    assert who is tw and pilot.update(110.0, behind) is None  # no further: Kick skips frames on its own
    yt.behind = 0.9  # YouTube behind too: the PC's own upload, so everyone comes down
    behind, who = enc.steering_backlog([(lk.behind, lk) for lk in (yt, kick, tw)])
    assert who is kick and pilot.update(120.0, behind) < 4800
    lone = SharedStreamEncoder(_Canvas(), 30, 6000, "libx264", time.perf_counter(), auto_bitrate=True)
    assert lone.steering_backlog([(3.0, kick)]) == (3.0, kick)  # on its own a link steers its encoder fully
    enc.links = [StreamLink("YouTube", "rtmp://x/app", "k", enc), StreamLink("Kick", "rtmp://x/app", "k", enc)]
    assert enc.label == "stream-horizontal (YouTube, Kick)"  # two encoders on one canvas are told apart in the log


def test_the_encoder_rests_while_no_destination_is_connected_and_wakes_when_one_is(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_stream, "RETRY_FIRST", 0.2)
    allowed = {"open": False}

    def door(url):
        if not allowed["open"]:
            raise OSError("[Errno 5] I/O error")
        return av.open(str(tmp_path / "k.flv"), "w", format="flv")

    enc = SharedStreamEncoder(_Canvas(), 30, 800, "libx264", time.perf_counter())
    link = StreamLink("kick", "rtmps://x/app", "sk_SECRET", enc, open_container=door)
    enc.links.append(link)
    enc.start()
    link.start()
    try:
        _feed(enc, 0.6)  # opens the encoder (its stream headers), then nobody takes its packets
        assert wait_for(lambda: link.reconnects >= 1)
        _feed(enc, 0.3)
        enc.links_changed()
        assert enc.paused_since is not None  # resting: no encoding for a destination that isn't there
        rested = enc.frames
        _feed(enc, 0.5)
        assert enc.frames <= rested + 2
        allowed["open"] = True
        assert wait_for(lambda: enc.paused_since is None, timeout=5)
        _feed(enc, 1.0)
        assert link.connected and link.frames > 5
    finally:
        link.stop()
        enc.stop()


def test_the_server_is_named_without_the_key():
    assert stream_mod.ingest_name("rtmps://ingest.example.net:443/app/sk_SECRET") == "rtmps://ingest.example.net:443/app"
    assert stream_mod.ingest_name("rtmp://a.example/live/k?bandwidthtest=true") == "rtmp://a.example/live"
    # Kick's ingest (Amazon IVS) needs its app name; pasted without it, the key would be taken as the app.
    kick = stream_mod.stream_url("rtmps://abc.global-contribute.live-video.net:443", "sk_X")
    assert kick == "rtmps://abc.global-contribute.live-video.net:443/app/sk_X"
    assert stream_mod.stream_url("rtmps://abc.global-contribute.live-video.net/app/", "sk_X").endswith(".net/app/sk_X")


def test_a_failed_connect_names_the_server_but_never_the_key(monkeypatch):
    monkeypatch.setattr(shared_stream, "RETRY_FIRST", 0.2)
    port = free_port()  # nothing listens there
    enc = SharedStreamEncoder(_Canvas(), 30, 800, "libx264", time.perf_counter())
    link = StreamLink("kick", f"rtmp://127.0.0.1:{port}/app", "live_SECRET", enc)
    enc.links.append(link)
    enc.start()
    link.start()
    try:
        _feed(enc, 0.6)
        # The count goes up a moment before the status is written: wait for the words.
        assert wait_for(lambda: f"couldn't connect to rtmp://127.0.0.1:{port}/app" in link.status)
        assert "SECRET" not in link.status
    finally:
        link.stop()
        enc.stop()
