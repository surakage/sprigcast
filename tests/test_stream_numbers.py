"""The stream report's numbers on the engine's writer (PC soak 2026-11-20: "encode 0.0 ms" on every line)."""

from types import SimpleNamespace

from sprigcast.output.recorder import Recorder


def test_the_engines_encode_time_is_this_encoders_encode_time(tmp_path):
    rec = Recorder(tmp_path / "x.mkv", 64, 36, 60, 800, "h264_nvenc", 0.0, "mkv")
    stats = SimpleNamespace(frames_written=600, frames_in=600, frames_repeated=0, frames_dropped=0, bytes=10_000,
                            write_seconds=0.5, longest_write=0.01, queued=0, state_name="writing", on_card=1,
                            recoveries=0, encode_seconds=1.2)
    rec._writer_numbers(SimpleNamespace(stats=lambda: stats, on_card=True))
    assert rec.encode_seconds == 1.2 and rec.stats()["encode_ms"] == 2.0
    del stats.encode_seconds  # an engine from before it counted: 0, as before
    rec._writer_numbers(SimpleNamespace(stats=lambda: stats, on_card=True))
    assert rec.encode_seconds == 0.0


def test_waiting_on_the_network_counts_a_full_send_buffer_not_time_handing_packets_over(monkeypatch):
    """PC soak 2026-11-20: "network 41%" with 0 dropped and backlog 0 was the sender waiting for Python's lock."""
    import time as _time

    from sprigcast.output import shared_stream
    from sprigcast.output.replay import _Packet
    from sprigcast.output.shared_stream import SharedStreamEncoder, StreamLink

    from test_shared_stream import _Canvas

    enc = SharedStreamEncoder(_Canvas(), 30, 6000, "libx264", _time.perf_counter())
    link = StreamLink("Twitch", "rtmp://x/app", "k", enc)
    clock = [100.0]
    monkeypatch.setattr(shared_stream.time, "perf_counter", lambda: clock[0])

    class _Out:
        def mux(self, packet):
            clock[0] += 0.012  # slow to hand over (Python's lock), the upload itself has room

    import fractions

    p = _Packet("video", b"x" * 100, 0, 0, fractions.Fraction(1, 1000), True)
    streams = {"video": None}
    monkeypatch.setattr(shared_stream, "log_live", lambda st: None)
    import av

    monkeypatch.setattr(av, "Packet", lambda data: SimpleNamespace(size=len(data)))
    for _ in range(50):
        link._send(_Out(), streams, p)
        clock[0] += 0.02
    assert link.mux_seconds > 0.5 and link.behind_seconds == 0.0  # sending took time, nothing was waiting
    link._backlog_bytes = 6000 * 1000 // 8  # a second of video waiting: the upload is behind
    for _ in range(10):
        link._send(_Out(), streams, p)
        clock[0] += 0.02
    assert abs(link.behind_seconds - 10 * 0.032) < 0.05
    assert "sending_pct" in link.stats() and "(sending" in link.summary()


def test_a_live_flvs_header_note_is_information_not_a_warning(caplog):
    import logging

    from sprigcast.app.logbook import quiet_live_flv

    quiet_live_flv()
    quiet_live_flv()  # once only
    flv = logging.getLogger("libav.flv")
    with caplog.at_level(logging.INFO):
        flv.warning("Failed to update header with correct duration.")
        flv.warning("Failed to update header with correct filesize.")
        flv.warning("Something else")
    levels = [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "libav.flv"]
    assert levels[0] == ("INFO", "Failed to update header with correct duration. (normal when a live stream ends)")
    assert levels[1][0] == "INFO" and levels[2] == ("WARNING", "Something else")
    assert sum(1 for f in flv.filters if type(f).__name__ == "LiveFlvQuiet") == 1


def test_libav_lines_arent_held_back_to_be_written_later():
    """PyAV tallied a line FFmpeg repeated and wrote it when the next different line came: a camera's refused
    modes appeared as errors when a place stopped, minutes later (live 2026-11-21)."""
    import av.logging

    from sprigcast.app import main

    before = av.logging.get_skip_repeated()
    try:
        av.logging.set_skip_repeated(True)
        assert main.import_av() and av.logging.get_skip_repeated() is False
    finally:
        av.logging.set_skip_repeated(before)


def test_a_connection_the_server_stopped_reading_is_made_again():
    """PC live 2026-11-25: YouTube (horizontal) took nothing for 23 s and dropped 907 frames before Windows ended it."""
    from sprigcast.output.shared_stream import StallWatch

    w = StallWatch(5.0)
    assert not w.stalled(0.0, 100, 50_000, True)
    assert not w.stalled(4.9, 100, 50_000, True)  # a short wait on the network is normal
    assert w.stalled(5.0, 100, 50_000, True)
    assert not w.stalled(6.0, 101, 50_000, True)  # it moved again: counted from now
    assert not w.stalled(20.0, 101, 0, True)  # nothing waiting to go: the encoder, not the network
    assert not w.stalled(30.0, 101, 50_000, False)  # still connecting: connect_timeout covers that
    assert not w.stalled(34.0, 101, 50_000, True)
    assert w.stalled(35.0, 101, 50_000, True)


def test_youtube_goes_over_rtmps():
    from sprigcast.output import stream

    assert stream.SERVICES["youtube"].server.startswith("rtmps://a.rtmps.youtube.com:443/")
