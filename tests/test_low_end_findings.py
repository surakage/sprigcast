"""Mikedog151's report from an AMD E2 with 4 GB (2026-11-24): the stream's numbers, its log lines and its rates."""

import logging

from sprigcast.core.scene import EncoderSettings


def test_stepping_down_gives_x264_the_bitrate_before_its_lower_limits():
    """FFmpeg hands x264 the max rate first: both at once had x264 see a max below the old average."""
    from sprigcast.output.recorder import next_rate

    enc = EncoderSettings(rate_control="cbr")
    assert next_rate("libx264", enc, (1500, 1500, 1500), 1500) is None
    first = next_rate("libx264", enc, (1500, 1500, 1500), 1200)
    assert first == (1200, 1500, 1500)  # the bitrate down, the limits where they were
    assert next_rate("libx264", enc, first, 1200) == (1200, 1200, 1200)  # then the limits follow
    assert next_rate("libx264", enc, (1200, 1200, 1200), 1500) == (1500, 1500, 1500)  # going up: all at once


def test_x264s_moved_keyframe_is_logged_once():
    from sprigcast.app.logbook import X264KeyQuiet

    quiet, lines = X264KeyQuiet(), []
    for n in (114, 174, 234):
        record = logging.LogRecord("libav.libx264", logging.WARNING, "", 0,
                                   f"forced frame type (1) at {n} was changed to frame type (2)", None, None)
        if quiet.filter(record):
            lines.append((record.levelname, record.getMessage()))
    other = logging.LogRecord("libav.libx264", logging.WARNING, "", 0, "something else", None, None)
    assert quiet.filter(other) and other.levelname == "WARNING"
    assert len(lines) == 1 and lines[0][0] == "INFO" and "later ones aren't logged" in lines[0][1]


def test_the_summary_averages_from_the_first_time_it_went_live(monkeypatch):
    """A reconnect 8 minutes before the end made 45 minutes at about 390 kbps read 2127."""
    import time

    from sprigcast.output import shared_stream

    link = shared_stream.StreamLink.__new__(shared_stream.StreamLink)
    now = 10_000.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    link.name, link.status = "Twitch", "live"
    link.shared = type("Shared", (), {"bitrate_kbps": 1500})()
    link.frames, link.dropped, link.reconnects = 79472 - 870, 870, 1
    link.started_at, link._first_live_at, link._connected_at = now - 2691, now - 2690, now - 493
    link.behind_seconds = link.mux_seconds = link.longest_stall = 0.0
    link.bytes_out = int(390 * 1000 / 8 * 2690)
    assert "averaged 390 kbps (set 1500)" in link.summary()


def test_the_report_names_the_x264_preset_for_an_x264_stream():
    from sprigcast.app.logbook import StreamReport

    class St:
        codec = "libx264"

    report = StreamReport.__new__(StreamReport)
    report.session = type("S", (), {"streamers": [("horizontal", St())]})()
    words = report._preset_words(EncoderSettings(x264_preset="superfast", preset="p2", tuning="hq"))
    assert words.startswith("x264 preset superfast") and "p2" not in words


def test_a_reused_engine_output_starts_its_drop_count_fresh(monkeypatch):
    from sprigcast.render import native_bridge

    bridge = native_bridge.NativeCanvases.__new__(native_bridge.NativeCanvases)
    bridge._dropped, bridge._late, bridge.output_drops = {}, {}, 0
    bridge._count_drops(0, 1, 6803, "horizontal", ((854, 480), False))  # what the slot's last output had
    assert bridge.output_drops == 0
    bridge._count_drops(0, 1, 6816, "horizontal", ((854, 480), False))
    assert bridge.output_drops == 13
