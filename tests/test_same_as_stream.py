"""Recording "Same as stream": a canvas being streamed is recorded from the stream's encoder, no second one."""

import time

import av

from sprigcast.output.shared import EncoderPool, SharedRecording
from sprigcast.output.shared_stream import SharedStreamEncoder, StreamLink
from sprigcast.output.stream import StreamSession, Streamer

from test_shared_stream import _Canvas, _Engine, _feed, _target  # noqa: E402
from test_stream import wait_for  # noqa: E402


def _no_network(monkeypatch):
    def refuse(self, url):
        raise OSError("no network in tests")

    monkeypatch.setattr(StreamLink, "_default_open", refuse)


def _frames(path) -> int:
    with av.open(str(path)) as f:
        assert sorted(s.type for s in f.streams) == ["audio", "video"]
        return sum(1 for _ in f.decode(video=0))


def test_one_place_gets_a_shared_encoder_only_when_asked(monkeypatch):
    _no_network(monkeypatch)
    c = _Canvas()
    for share_all, kind in ((False, Streamer), (True, StreamLink)):
        engine = _Engine()
        session = StreamSession.start(engine, [_target("tw", c)], "libx264", share_all=share_all)
        try:
            assert isinstance(session.streamers[0][1], kind)
            found = session.encoders_by_canvas()
            assert list(found) == (["horizontal"] if share_all else [])
            assert len(engine.frame_taps) == 1  # still one encoder either way
        finally:
            session.stop()
        assert engine.frame_taps == [] and engine.mixer.taps == []


def test_the_recording_keeps_the_encoder_after_the_stream_ends(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    c = _Canvas()
    engine = _Engine()
    session = StreamSession.start(engine, [_target("tw", c, kbps=800)], "libx264", share_all=True)
    enc = session.shared[0]
    pool = EncoderPool(engine)
    recording = None
    try:
        _feed(enc, 0.3)
        recording = SharedRecording.start(pool, [c], tmp_path, 4000, "mkv", codec="libx264",
                                          stream_encoders=session.encoders_by_canvas())
        assert recording.recorders == [("horizontal", enc)] and pool.encoders == {}  # no second encoder
        _feed(enc, 1.0)
        session.stop()  # the stream ends; the recording goes on
        assert enc.running and enc.holders == {"recording"}
        assert any(cb == enc.push_frame for _, cb in engine.frame_taps)
        assert enc.push_audio in engine.mixer.taps
        _feed(enc, 1.0)
    finally:
        if recording is not None:
            recording.stop()
    assert wait_for(lambda: not enc.running, 10)
    assert engine.frame_taps == [] and engine.mixer.taps == []  # the last one out took it off the picture
    (path,) = recording.paths
    assert path.suffix == ".mkv" and _frames(path) > 40  # before and after the stream ended
    assert not recording.errors


def test_stopping_the_recording_leaves_the_stream_running(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    c = _Canvas()
    engine = _Engine()
    session = StreamSession.start(engine, [_target("tw", c, kbps=800)], "libx264", share_all=True)
    enc = session.shared[0]
    try:
        _feed(enc, 0.2)
        recording = SharedRecording.start(EncoderPool(engine), [c], tmp_path, 4000, "mp4", codec="libx264",
                                          stream_encoders=session.encoders_by_canvas())
        _feed(enc, 1.0)
        recording.stop()
        assert enc.running and enc.holders == {"stream"} and enc.on_last_let_go is None
        assert len(engine.frame_taps) == 1
        _feed(enc, 0.2)
        assert _frames(recording.paths[0]) > 15
        # A second recording can start on the same stream.
        again = SharedRecording.start(EncoderPool(engine), [c], tmp_path / "2", 4000, "mkv", codec="libx264",
                                      stream_encoders=session.encoders_by_canvas())
        _feed(enc, 0.6)
        again.stop()
        assert _frames(again.paths[0]) > 5
    finally:
        session.stop()
    assert not enc.running and engine.frame_taps == [] and engine.mixer.taps == []


def test_dropping_the_last_place_keeps_the_encoder_for_the_recording(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    c = _Canvas()
    engine = _Engine()
    session = StreamSession.start(engine, [_target("tw", c, kbps=800)], "libx264", share_all=True)
    enc = session.shared[0]
    recording = None
    try:
        _feed(enc, 0.2)
        recording = SharedRecording.start(EncoderPool(engine), [c], tmp_path, 4000, "mkv", codec="libx264",
                                          stream_encoders=session.encoders_by_canvas())
        assert session.drop("tw")
        assert enc.running and session.shared == [] and session.taps == []
        assert any(cb == enc.push_frame for _, cb in engine.frame_taps)
        _feed(enc, 0.6)
        session.stop()  # nothing left of the stream: the recording's encoder stays
        assert enc.running and enc.push_audio in engine.mixer.taps
    finally:
        if recording is not None:
            recording.stop()
    assert wait_for(lambda: not enc.running, 10)
    assert engine.frame_taps == [] and engine.mixer.taps == []


def test_a_canvas_not_being_streamed_records_with_its_own_encoder(tmp_path):
    c, v = _Canvas(), _Canvas("vertical", 90, 160)
    engine = _Engine()
    stream_enc = SharedStreamEncoder(c, 30, 800, "libx264", time.perf_counter())
    stream_enc.start()
    pool = EncoderPool(engine)
    try:
        recording = SharedRecording.start(pool, [c, v], tmp_path, 4000, "mkv", codec="libx264",
                                          stream_encoders={"horizontal": stream_enc})
        assert list(pool.encoders) == ["vertical"] and recording.borrowed == {"horizontal": stream_enc}
        recording.stop()
        assert pool.encoders == {} and stream_enc.holders == {"stream"}
    finally:
        stream_enc.stop()
