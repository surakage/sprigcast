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
