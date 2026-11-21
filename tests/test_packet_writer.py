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


def test_the_engine_sends_a_stream_itself_and_python_only_reads_its_numbers(native, tmp_path, monkeypatch):
    """With the writer encoding, a link's connection is the engine's too (sc_link): no packet is queued or sent
    in Python. A connection that can't be made is tried again as before, with the reason and never the key."""
    enc = SharedStreamEncoder(stream_tests._Canvas(), 30, 800, "libx264", stream_tests.time.perf_counter())
    link = stream_tests.StreamLink("a", str(tmp_path), "sent.flv", enc)
    lost = stream_tests.StreamLink("lost", str(tmp_path / "no-such-folder"), "live_SECRET", enc)
    monkeypatch.setattr(stream_mod, "RETRY_FIRST", 0.05)
    enc.links += [link, lost]
    enc.start()
    link.start()
    lost.start()
    try:
        stream_tests._feed(enc, 1.0)
        assert link.connected and link.status == "live" and link._sender is not None
        assert link._q.empty() and link.frames > 10 and link.bytes_out > 0 and link.dropped == 0
        assert link.stats()["frames"] == link.frames
        assert not lost.connected and lost.reconnects >= 1 and "disconnected: couldn't connect" in lost.status
        assert "SECRET" not in lost.status
    finally:
        lost.stop()
        link.stop()
        enc.stop()
    assert enc.error is None and link.status == "stopped" and link._sender is None
    with av.open(str(tmp_path / "sent.flv")) as f:
        packets = [p for p in f.demux(video=0) if p.size]
        assert packets[0].is_keyframe and packets[0].pts == 0 and 0 <= len(packets) - link.frames <= 1


def test_the_engines_sender_can_be_turned_off(native, tmp_path, monkeypatch):
    monkeypatch.setenv("SPRIGCAST_NO_NATIVE_LINK", "1")
    enc = SharedStreamEncoder(stream_tests._Canvas(), 30, 800, "libx264", stream_tests.time.perf_counter())
    link = stream_tests.StreamLink("a", str(tmp_path), "sent.flv", enc)
    enc.links.append(link)
    enc.start()
    link.start()
    try:
        stream_tests._feed(enc, 1.0)
        assert link.connected and link._sender is None and link.frames > 0
    finally:
        link.stop()
        enc.stop()


def test_a_stream_delay_is_kept_by_the_engine_and_sent_out_at_the_end(native, tmp_path):
    enc = SharedStreamEncoder(stream_tests._Canvas(), 30, 800, "libx264", stream_tests.time.perf_counter())
    link = stream_tests.StreamLink("a", str(tmp_path), "sent.flv", enc)
    link.delay_s = 1.0
    enc.links.append(link)
    enc.start()
    link.start()
    try:
        stream_tests._feed(enc, 0.7)
        assert link._ready and link.frames == 0  # held back
        link.stop()
        assert link.status.startswith("ending: sending the last 1 s")
        stream_tests._feed(enc, 0.2)  # nothing new is taken while it ends
        link._thread.join(8)
        assert not link._thread.is_alive() and link.status == "stopped" and link.frames > 3
    finally:
        link._stop.set()
        enc.stop()
    with av.open(str(tmp_path / "sent.flv")) as f:
        assert 0 <= sum(1 for _ in f.decode(video=0)) - link.frames <= 1


def _engine_wrote(caplog, monkeypatch):
    """Watches for "written by the engine" said about a recording's file (output/shared.py FileTap)."""
    caplog.set_level(logging.INFO, logger="sprigcast.output.shared")
    return lambda: [r.getMessage() for r in caplog.records if r.name == "sprigcast.output.shared"
                    and "written by the engine" in r.getMessage()]


def test_the_engine_writes_the_recording_file_itself(native, tmp_path, monkeypatch):
    """A recording from a shared encoder is written by the engine (sc_file): Python packs and writes nothing."""
    from sprigcast.output import replay as replay_mod

    said = _engine_wrote(native, monkeypatch)
    made = []
    real = replay_mod.PacketFile.__init__
    monkeypatch.setattr(replay_mod.PacketFile, "__init__", lambda self, *a, **k: (made.append(a), real(self, *a, **k))[1])
    shared_tests.test_recording_alone_then_again(tmp_path)
    assert said() and not [a for a in made if "replay" not in str(a[0]).lower() and "clip" not in str(a[0]).lower()]


def test_the_engines_recording_file_can_be_turned_off(native, tmp_path, monkeypatch):
    monkeypatch.setenv("SPRIGCAST_NO_NATIVE_FILE", "1")
    said = _engine_wrote(native, monkeypatch)
    shared_tests.test_recording_alone_then_again(tmp_path)
    assert not said()


def test_recordings_written_by_the_engine_split_pause_and_keep_chapters(native, tmp_path, monkeypatch):
    import test_obs_lessons_3 as lessons

    said = _engine_wrote(native, monkeypatch)
    for i, test in enumerate((lessons.test_a_long_recording_splits_at_keyframes_with_nothing_lost,
                              lessons.test_chapter_marks_land_in_the_file,
                              lessons.test_an_mp4_keeps_its_chapters_when_made_plain,
                              shared_tests.test_stopping_a_recording_can_leave_the_wait_to_another_thread,
                              shared_tests.test_recording_can_be_saved_smaller_than_the_canvas,
                              shared_tests.test_without_the_voice_track_a_recording_has_one_sound)):
        folder = tmp_path / str(i)
        folder.mkdir()
        test(folder)
    assert len(said()) >= 6


def test_the_engine_keeps_clips_and_python_takes_no_packets(native, tmp_path, monkeypatch):
    """Clips' seconds are kept by the writer (sc_writer_keep) and a clip is written by the engine; with the
    recording written there too, no packet comes back to Python at all."""
    from sprigcast.output.shared import CanvasEncoder

    taken = []
    real = CanvasEncoder._take
    monkeypatch.setattr(CanvasEncoder, "_take", lambda self, p: (taken.append(p.kind), real(self, p))[1])
    kept = []
    real_snapshot = CanvasEncoder.clip_snapshot

    def snapshot(self, *a, **k):
        kept.append((self._engine_keeps, len(self._packets), self._engine_hands_out, self.buffered_seconds))
        return real_snapshot(self, *a, **k)

    monkeypatch.setattr(CanvasEncoder, "clip_snapshot", snapshot)
    shared_tests.test_a_clip_is_saved_without_waiting_for_its_file(tmp_path)
    assert kept and all(k[0] and k[1] == 0 and not k[2] and k[3] > 0 for k in kept)
    assert len(taken) < 20  # only what came before the writer was told nobody takes them


def test_clips_kept_by_the_engine_can_be_turned_off(native, tmp_path, monkeypatch):
    from sprigcast.output.shared import CanvasEncoder

    monkeypatch.setenv("SPRIGCAST_NO_NATIVE_CLIPS", "1")
    kept = []
    real_snapshot = CanvasEncoder.clip_snapshot
    monkeypatch.setattr(CanvasEncoder, "clip_snapshot",
                        lambda self, *a, **k: (kept.append((self._engine_keeps, len(self._packets))),
                                               real_snapshot(self, *a, **k))[1])
    shared_tests.test_a_clip_is_saved_without_waiting_for_its_file(tmp_path)
    assert kept and all(not k[0] and k[1] > 0 for k in kept)


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
    from test_enhanced import ANSWER, _metrics_before_picture

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
                tagged.append(_metrics_before_picture(bytes(p)))
    assert keys[0] == keys[1] == keys[2] and len(keys[0]) >= 2, keys
    assert all(tagged)


def test_youtube_at_a_twitch_quality_sends_its_packets_without_an_encoder_of_its_own(native, tmp_path, monkeypatch):
    """With Enhanced Broadcasting on the engine, another destination wanting the same picture as one of Twitch's
    qualities (canvas, size, frame rate) sends that quality's packets: no extra encoder."""
    import time

    import numpy as np
    from test_enhanced import ANSWER

    from sprigcast.core.scene import Canvas
    from sprigcast.output import enhanced as e
    from sprigcast.render.engine import Frame

    cfg = e.parse_config(ANSWER, "live_user", "libx264")
    h = Canvas(id="horizontal", width=640, height=360, fps=30)
    v = Canvas(id="vertical", width=360, height=640, fps=30)
    twitch = stream_mod.StreamTarget("Twitch", h, "rtmp://x/app", "live_user", 6000, output_id="tw")
    twitch.enhanced, twitch.canvases = cfg, [h, v]
    youtube = stream_mod.StreamTarget("YouTube", h, "rtmp://y/app", "yt", 8000, output_id="yt")
    paths = {"tw": tmp_path / "tw.flv", "yt": tmp_path / "yt.flv"}
    monkeypatch.setattr("sprigcast.output.shared_stream.StreamLink._default_open",
                        lambda self, url: av.open(str(paths["yt"]), "w", format="flv"))
    engine = stream_tests._Engine()
    from sprigcast.output.enhanced_stream import EnhancedStreamer

    real_init = EnhancedStreamer.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        self._url = str(paths["tw"])
    monkeypatch.setattr(EnhancedStreamer, "__init__", init)
    session = stream_mod.StreamSession.start(engine, [twitch, youtube], "libx264")
    try:
        assert session.shared == [] and len(session.borrowed) == 1  # YouTube rides Twitch's 640x360 quality
        assert len(engine.frame_taps) == 3 and len(engine.mixer.taps) == 3  # Twitch's 3 qualities; 2 streams + 1
        start = time.perf_counter()
        for i in range(75):
            when = start + 0.01 + i / 30
            while time.perf_counter() < when:
                time.sleep(0.002)
            for c in (h, v):
                data = np.full((c.height, c.width, 4), i * 3 % 255, np.uint8).tobytes()
                for canvas_id, callback in list(engine.frame_taps):
                    if canvas_id == c.id:
                        callback(Frame(c.id, c.width, c.height, data, when))
            for tap in list(engine.mixer.taps):
                tap(np.zeros((1600, 2), np.float32))
        yt = next(st for _, st in session.streamers if getattr(st, "output_id", "") == "yt")
        assert yt.connected
    finally:
        session.stop()
    assert engine.frame_taps == [] and engine.mixer.taps == []
    with av.open(str(paths["yt"])) as f:
        assert sorted(s.type for s in f.streams) == ["audio", "video"]
        assert sum(1 for _ in f.decode(video=0)) > 20
    with av.open(str(paths["yt"])) as f:
        sent = [bytes(p) for p in f.demux(video=0) if p.size]
    assert sent and not any(e.BPM_ERM_UUID in b for b in sent)  # Twitch's metrics stay Twitch's



def test_twitch_switched_off_while_live_leaves_its_quality_to_the_places_sending_it(native, tmp_path, monkeypatch):
    """Switching Twitch (Enhanced Broadcasting) off while live: Twitch stops, and the quality YouTube sends keeps
    encoding for YouTube until the stream ends. Settings used to refuse it, leaving no way to drop a failing
    Twitch but ending the whole stream (PC, 2026-11-21)."""
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
    twitch = stream_mod.StreamTarget("Twitch", h, "rtmp://x/app", "live_user", 6000, output_id="tw")
    twitch.enhanced, twitch.canvases = cfg, [h, v]
    youtube = stream_mod.StreamTarget("YouTube", h, "rtmp://y/app", "yt", 8000, output_id="yt")
    paths = {"tw": tmp_path / "tw.flv", "yt": tmp_path / "yt.flv"}
    monkeypatch.setattr("sprigcast.output.shared_stream.StreamLink._default_open",
                        lambda self, url: av.open(str(paths["yt"]), "w", format="flv"))
    engine = stream_tests._Engine()
    real_init = EnhancedStreamer.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        self._url = str(paths["tw"])
    monkeypatch.setattr(EnhancedStreamer, "__init__", init)
    session = stream_mod.StreamSession.start(engine, [twitch, youtube], "libx264")
    start = time.perf_counter()

    def feed(frames, first):
        for i in range(first, first + frames):
            when = start + 0.01 + i / 30
            while time.perf_counter() < when:
                time.sleep(0.002)
            for c in (h, v):
                data = np.full((c.height, c.width, 4), i * 3 % 255, np.uint8).tobytes()
                for canvas_id, callback in list(engine.frame_taps):
                    if canvas_id == c.id:
                        callback(Frame(c.id, c.width, c.height, data, when))
            for tap in list(engine.mixer.taps):
                tap(np.zeros((1600, 2), np.float32))

    try:
        feed(45, 0)
        tw = session.find("tw")
        lent = session.borrowed[0]
        assert session.drop("tw")
        assert session.find("tw") is None and session.find("yt") is not None
        assert len(engine.frame_taps) == 1  # only the quality YouTube sends is still fed
        yt = session.find("yt")
        sent = yt.frames
        feed(45, 45)
        deadline = time.monotonic() + 10
        while tw.running and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not tw.running and lent.running and lent.error is None
        assert yt.frames > sent + 20  # YouTube carried on after Twitch stopped
    finally:
        session.stop()
    assert engine.frame_taps == [] and engine.mixer.taps == []
    assert not lent.running


def _flv_tags(path):
    """(kind, time in ms, body) of each tag of an FLV file."""
    with open(path, "rb") as f:
        data = f.read()
    at, tags = 13, []
    while at + 11 <= len(data):
        size = int.from_bytes(data[at + 1:at + 4], "big")
        stamp = int.from_bytes(data[at + 4:at + 7], "big") | data[at + 7] << 24
        tags.append((data[at] & 0x1F, stamp, data[at + 11:at + 11 + size]))
        assert int.from_bytes(data[at + 11 + size:at + 15 + size], "big") == 11 + size
        at += 15 + size
    assert at == len(data)
    return tags


def _enhanced(tmp_path, name):
    from test_enhanced import ANSWER

    from sprigcast.core.scene import Canvas
    from sprigcast.output import enhanced as e
    from sprigcast.output.enhanced_stream import EnhancedStreamer

    cfg = e.parse_config(ANSWER, "live_user", "libx264")
    h = Canvas(id="horizontal", width=640, height=360, fps=30)
    v = Canvas(id="vertical", width=360, height=640, fps=30)
    start = stream_tests.time.perf_counter()
    st = EnhancedStreamer("Twitch", cfg, [h, v], start, user_key="live_user")
    st._url = str(tmp_path / name)
    return st, (h, v), start


def _feed_enhanced(st, canvases, start, frames=60):
    import time

    import numpy as np

    from sprigcast.render.engine import Frame

    taps = st.frame_taps()
    for i in range(frames):
        when = start + 0.01 + i / 30
        while time.perf_counter() < when:
            time.sleep(0.002)
        for c in canvases:
            data = np.full((c.height, c.width, 4), i * 3 % 255, np.uint8).tobytes()
            for canvas_id, callback, _size in taps:
                if canvas_id == c.id:
                    callback(Frame(c.id, c.width, c.height, data, when))
        st.push_audio(np.zeros((1600, 2), np.float32))


def test_the_engine_packs_and_sends_enhanced_broadcasting_as_python_did(native, tmp_path):
    """Twitch Enhanced Broadcasting's connection is the engine's (sc_esend): no sender thread of Python's packs or
    sends a packet. What goes out is what output/eflv.py made of the same packets, tag for tag: the main quality
    in Enhanced RTMP's own form, the others as multitrack packets, then the sound."""
    import threading

    from sprigcast.output.eflv import EnhancedIngest

    st, canvases, start = _enhanced(tmp_path, "engine.flv")
    handed, senders = [], []
    real = st._to_engine

    def to_engine(packet):
        handed.append(packet)
        senders.append(st._esend)
        real(packet)
    st._to_engine = to_engine
    st.start()
    for t in st.tracks:
        assert t.encoder.wait_open()
    python = EnhancedIngest(str(tmp_path / "python.flv"))
    streams = [python.add_stream_from_template(t.encoder._templates[0]) for t in st.tracks]
    sound = python.add_stream("aac", rate=48000)
    sound.layout, sound.bit_rate = "stereo", st.config.audio_kbps * 1000
    _feed_enhanced(st, canvases, start)
    assert st.connected and st.status == "live" and st.bytes_out > 0
    assert not any(t.name.startswith("sprigcast-send-") for t in threading.enumerate())
    st.stop()
    assert st.error is None, st.error
    assert st._esend is None and all(s is not None for s in senders)
    assert st.frames > 30 and len(handed) > 100 and {p.stream for p in handed} == {0, 1, 2, 3}

    for p in handed:  # the same packets through Python's packing
        packet = av.Packet(p.data)
        packet.pts, packet.dts, packet.time_base, packet.is_keyframe = p.pts, p.dts, p.time_base, p.key
        packet.stream = sound if p.stream == 3 else streams[p.stream]
        python.mux(packet)
    python.close()
    engine, ours = _flv_tags(tmp_path / "engine.flv"), _flv_tags(tmp_path / "python.flv")
    assert [t for t in engine if t[0] != 18] == [t for t in ours if t[0] != 18]
    main = [body for kind, _stamp, body in engine if kind == 9 and body[1:5] == b"avc1" and body[0] & 0x0F < 4]
    assert len(main) > 30 and main[0][0] == 0x90  # keyframe, Enhanced RTMP, SequenceStart
    assert not any(kind == 9 and not body[0] & 0x80 for kind, _stamp, body in engine)  # no classic H.264 tag left


def test_enhanced_broadcasting_can_stay_on_pythons_sender(native, tmp_path, monkeypatch):
    monkeypatch.setenv("SPRIGCAST_NO_NATIVE_ESEND", "1")
    st, canvases, start = _enhanced(tmp_path, "python.flv")
    st.start()
    for t in st.tracks:
        assert t.encoder.wait_open()
    _feed_enhanced(st, canvases, start, 45)
    assert st.connected and st._esend is None and st._sender is not None
    st.stop()
    assert st.error is None, st.error
    with av.open(str(tmp_path / "python.flv")) as f:
        assert len(f.streams.video) == 3 and sum(1 for _ in f.decode(video=0)) > 20


def test_an_enhanced_connection_the_engine_cant_make_is_tried_again_and_keeps_the_key_out(native, tmp_path,
                                                                                         monkeypatch):
    monkeypatch.setattr(stream_mod, "RETRY_FIRST", 0.05)
    st, canvases, start = _enhanced(tmp_path, "sent.flv")
    st._url = str(tmp_path / "no-such-folder" / "live_SECRET")
    st._secrets.append("live_SECRET")
    st.start()
    try:
        for t in st.tracks:
            assert t.encoder.wait_open()
        _feed_enhanced(st, canvases, start, 20)
        assert not st.connected and st.reconnects >= 1 and "disconnected: couldn't connect" in st.status
        assert "SECRET" not in st.status
        assert all(t.encoder.running and t.encoder.error is None for t in st.tracks)  # the qualities go on
    finally:
        st.stop()


def test_a_place_switched_back_on_while_live_sends_twitchs_quality_again(native, tmp_path, monkeypatch):
    """YouTube switched off and on mid-stream got a Python encoder and sender of its own beside the Twitch quality
    it had been sending; soon the PC was at 100% and YouTube dropped 110 frames (live, 2026-11-21). It is placed as
    going live would have: on Twitch's quality again, and a place with a picture nobody encodes gets a shared
    encoder the engine sends from."""
    import time

    import numpy as np
    from test_enhanced import ANSWER

    from sprigcast.core.scene import Canvas
    from sprigcast.output import enhanced as e
    from sprigcast.output.enhanced_stream import EnhancedStreamer
    from sprigcast.output.shared_stream import StreamLink
    from sprigcast.render.engine import Frame

    cfg = e.parse_config(ANSWER, "live_user", "libx264")
    h = Canvas(id="horizontal", width=640, height=360, fps=30)
    v = Canvas(id="vertical", width=360, height=640, fps=30)
    twitch = stream_mod.StreamTarget("Twitch", h, "rtmp://x/app", "live_user", 6000, output_id="tw")
    twitch.enhanced, twitch.canvases = cfg, [h, v]

    def youtube():
        return stream_mod.StreamTarget("YouTube", h, str(tmp_path), "yt.flv", 8000, output_id="yt")

    real_init = EnhancedStreamer.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        self._url = str(tmp_path / "tw.flv")
    monkeypatch.setattr(EnhancedStreamer, "__init__", init)
    engine = stream_tests._Engine()
    session = stream_mod.StreamSession.start(engine, [twitch, youtube()], "libx264")
    start = time.perf_counter()

    def feed(frames, first):
        for i in range(first, first + frames):
            when = start + 0.01 + i / 30
            while time.perf_counter() < when:
                time.sleep(0.002)
            for c in (h, v):
                data = np.full((c.height, c.width, 4), i * 3 % 255, np.uint8).tobytes()
                for canvas_id, callback in list(engine.frame_taps):
                    if canvas_id == c.id:
                        callback(Frame(c.id, c.width, c.height, data, when))
            for tap in list(engine.mixer.taps):
                tap(np.zeros((1600, 2), np.float32))

    try:
        feed(30, 0)
        quality = session.borrowed[0]
        first = session.find("yt")
        assert first.shared is quality and len(engine.frame_taps) == 3
        taps = len(engine.mixer.taps)
        assert session.drop("yt") and quality.links == [] and quality.running
        feed(10, 30)
        again = session.add(youtube(), "libx264")
        assert isinstance(again, StreamLink) and again is not first and again.shared is quality
        assert quality.links == [again] and session.borrowed == [quality] and session.shared == []
        assert len(engine.frame_taps) == 3 and len(engine.mixer.taps) == taps  # no encoder, no second sound tap
        feed(75, 40)  # it starts at the quality's next keyframe
        assert again.connected and again.frames > 0 and again._sender is not None  # sent by the engine

        small = stream_mod.StreamTarget("Kick", h, str(tmp_path), "kick.flv", 800, (480, 270), output_id="kick")
        kick = session.add(small, "libx264")
        assert isinstance(kick, StreamLink) and len(session.shared) == 1 and kick.shared is session.shared[0]
        assert len(engine.frame_taps) == 4
        feed(45, 115)
        assert kick.connected and kick._sender is not None
    finally:
        session.stop()
    assert engine.frame_taps == [] and engine.mixer.taps == []
    with av.open(str(tmp_path / "kick.flv")) as f:
        assert (f.streams.video[0].width, f.streams.video[0].height) == (480, 270)
