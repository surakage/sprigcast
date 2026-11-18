"""Shared encoders on the native engine (phase 4): the engine's writer encodes on its own thread and hands the
packets back (packets mode, output/replay.py ReplayBuffer._writer_after); Python only keeps them for clips, writes
the files and sends the streams. The same tests as with PyAV's encoders, run on the writer."""

from __future__ import annotations

import logging

import av
import pytest
import test_shared_encoder as shared_tests
import test_shared_stream as stream_tests
from test_native_media import (
    player_lib,  # noqa: F401 (builds or finds the engine's media library)
)

from sprigcast import sprigcore
from sprigcast.output import stream as stream_mod
from sprigcast.output.replay import ReplayBuffer
from sprigcast.output.shared_stream import SharedStreamEncoder

pytestmark = pytest.mark.skipif("libx264" not in av.codecs_available, reason="needs libx264 in PyAV")


@pytest.fixture
def native(monkeypatch, player_lib, caplog):  # noqa: F811
    monkeypatch.setenv("SPRIGCAST_ENGINE", "native")
    for name in ("SPRIGCAST_NO_WRITER", "SPRIGCAST_NO_PACKET_WRITER"):
        monkeypatch.delenv(name, raising=False)
    assert sprigcore.use_native_media()
    caplog.set_level(logging.INFO, logger="sprigcast.output.recorder")
    yield caplog
    said = [r.getMessage() for r in caplog.get_records("call")]
    assert any("written by the engine" in m for m in said)
    assert not any("couldn't start" in m for m in said)


def test_shared_encoders_go_through_the_writer_unless_turned_off(native, monkeypatch):
    enc = ReplayBuffer("horizontal", 64, 36, 30, 800, "libx264", 0.0, 10)
    assert enc.packets_mode and enc.uses_writer()
    assert stream_mod.engine_encodes_streams()
    monkeypatch.setenv("SPRIGCAST_NO_PACKET_WRITER", "1")
    assert not enc.uses_writer() and not stream_mod.engine_encodes_streams()
    monkeypatch.delenv("SPRIGCAST_NO_PACKET_WRITER")
    monkeypatch.setenv("SPRIGCAST_ENGINE", "python")
    assert not enc.uses_writer() and not stream_mod.engine_encodes_streams()
    logging.getLogger("sprigcast.output.recorder").info("written by the engine (nothing was started here)")


def test_recording_and_clips_share_one_encoder_on_the_writer(native, tmp_path):
    shared_tests.test_recording_and_replay_share_one_encoder_per_canvas(tmp_path)


def test_clips_are_saved_from_the_writers_packets(native, tmp_path):
    shared_tests.test_a_clip_is_saved_without_waiting_for_its_file(tmp_path)


def test_recordings_from_the_writer_carry_a_voice_track(native, tmp_path):
    shared_tests.test_recordings_carry_a_voice_track_and_clips_their_own_sound(tmp_path)


def test_one_writer_feeds_two_streams_each_from_a_keyframe(native, tmp_path):
    stream_tests.test_one_encoder_feeds_two_files_each_from_a_keyframe(tmp_path)


def test_clips_kept_in_the_streams_writer(native, tmp_path):
    stream_tests.test_clips_started_while_live_keep_their_seconds_in_the_streams_encoder(tmp_path)


def test_a_stream_steers_the_writers_bitrate(native, tmp_path):
    enc = SharedStreamEncoder(stream_tests._Canvas(), 30, 800, "libx264", stream_tests.time.perf_counter())
    path = tmp_path / "a.flv"
    link = stream_tests.StreamLink("a", "unused", "key", enc,
                                   open_container=lambda u: av.open(str(path), "w", format="flv"))
    enc.links.append(link)
    enc.start()
    link.start()
    try:
        stream_tests._feed(enc, 0.5)
        enc._want_kbps = 300  # what the auto-pilot does when a link falls behind
        stream_tests._feed(enc, 0.5)
        assert enc._applied_kbps == 300 and link.connected
    finally:
        link.stop()
        enc.stop()
    assert enc.error is None
    with av.open(str(path)) as f:
        assert sum(1 for _ in f.decode(video=0)) > 10


def test_a_lone_stream_gets_a_shared_encoder_on_the_writer(native):
    engine = stream_tests._Engine()
    target = stream_mod.StreamTarget("Alone", stream_tests._Canvas(), "rtmp://127.0.0.1:1/app", "secretkey", 800,
                                     output_id="alone")
    session = stream_mod.StreamSession.start(engine, [target], "libx264")
    try:
        assert len(session.shared) == 1 and len(session.streamers) == 1
        assert not isinstance(session.streamers[0][1], stream_mod.Streamer)
        enc = session.shared[0]
        assert enc.wait_open() and enc.writer is not None and enc._templates is not None
    finally:
        session.stop()
    assert engine.frame_taps == [] and engine.mixer.taps == []


def test_enhanced_broadcasting_tracks_encode_on_writers_with_aligned_keyframes(native, tmp_path):
    """Each quality of an Enhanced Broadcasting stream is a packets-mode writer the engine feeds at its size; the
    keyframes still land at the same instants on every track and carry the broadcast metrics."""
    import time

    import numpy as np
    from test_enhanced import ANSWER

    from sprigcast.core.scene import Canvas
    from sprigcast.output import enhanced as e
    from sprigcast.output.enhanced_stream import EnhancedStreamer
    from sprigcast.render.engine import Frame

    cfg = e.parse_config(ANSWER, "live_user", "libx264")
    h = Canvas(id="horizontal", width=640, height=360, fps=30)
    v = Canvas(id="vertical", width=360, height=640, fps=30)
    start = time.perf_counter()
    st = EnhancedStreamer("Twitch", cfg, [h, v], start, user_key="live_user")
    assert st.native
    st._url = str(tmp_path / "out.flv")
    taps = st.frame_taps()
    assert [(c, size) for c, _cb, size in taps] == [("horizontal", (640, 360)), ("horizontal", (320, 180)),
                                                     ("vertical", (360, 640))]
    st.start()
    for t in st.tracks:
        assert t.encoder.wait_open()
    for i in range(75):
        when = start + 0.01 + i / 30
        while time.perf_counter() < when:
            time.sleep(0.002)
        for c in (h, v):
            data = np.full((c.height, c.width, 4), i * 3 % 255, np.uint8).tobytes()
            for canvas_id, callback, _size in taps:
                if canvas_id == c.id:
                    callback(Frame(c.id, c.width, c.height, data, when))
        st.push_audio(np.zeros((1600, 2), np.float32))
    st.stop()
    assert st.error is None, st.error
    assert all(not t.encoder.running and t.encoder.error is None for t in st.tracks)
    assert st.frames > 30

    import av

    with av.open(str(tmp_path / "out.flv")) as c:
        sizes = [(s.codec_context.width, s.codec_context.height) for s in c.streams.video]
        assert sizes == [(640, 360), (320, 180), (360, 640)]
        keys, tagged = {}, []
        for p in c.demux(video=list(range(3))):
            if p.is_keyframe:
                keys.setdefault(p.stream.index, []).append(round(float(p.pts * p.time_base), 2))
                tagged.append(e.BPM_ERM_UUID in bytes(p))
    assert keys[0] == keys[1] == keys[2] and len(keys[0]) >= 2, keys
    assert all(tagged)
