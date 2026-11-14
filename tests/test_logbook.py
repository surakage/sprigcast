import logging
import os
import time
from datetime import datetime

import pytest

pytest.importorskip("PySide6.QtCore")

from sprigcast.app import logbook  # noqa: E402


def test_setup_logs_to_todays_file_and_prunes_old_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("SPRIGCAST_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logbook, "_crash_file", object())  # leave pytest's own faulthandler alone
    old = tmp_path / "sprigcast 2026-01-01.log"
    old.write_text("old")
    os.utime(old, (time.time() - 30 * 86400,) * 2)
    path = logbook.setup(datetime(2026, 11, 14, 9, 0))
    try:
        logging.getLogger("sprigcast.test").info("hello logbook")
        logbook._handler.flush()
        assert path.name == "sprigcast 2026-11-14.log"
        assert "hello logbook" in path.read_text(encoding="utf-8")
        assert not old.exists()
    finally:
        logging.getLogger().removeHandler(logbook._handler)
        logbook._handler.close()
        logbook._handler = None


class _Pilot:
    current_kbps = 5400


class _St:
    def __init__(self, name, key):
        self.name, self._key = name, key
        self.connected, self.status = True, "live"
        self.bytes_out = self.frames = self.dropped = 0
        self.out_width, self.out_height, self.fps, self.bitrate_kbps = 1920, 1080, 60, 6000
        self.codec, self.autopilot, self._direct = "h264_nvenc", _Pilot(), object()

    def redact(self, text):
        return text.replace(self._key, "****")

    def stats(self):
        return {"backlog_s": 0.1, "encode_ms": 2.5, "network_pct": 12.0}


class _Session:
    def __init__(self, streamers):
        self.streamers, self.shared = streamers, []


def test_stream_report_writes_setup_lines_every_5_s_and_the_summary(tmp_path):
    from sprigcast.core.events import Stats

    now = [100.0]
    st = _St("Twitch (horizontal) live_SECRET", "live_SECRET")
    report = logbook.StreamReport(_Session([("horizontal", st)]), folder=tmp_path, clock=lambda: now[0],
                                  now=datetime(2026, 11, 14, 21, 0, 0))
    for second in range(1, 12):
        now[0] = 100.0 + second
        st.bytes_out += 750_000  # 6000 kbps
        st.frames += 60
        report.tick(Stats(60.0, 3.2, 0))
    report.finish(["Twitch: 0 of 660 frames dropped (0.0%), averaged 6000 kbps (set 6000)."])
    text = report.path.read_text(encoding="utf-8")
    assert report.path.name == "stream 2026-11-14 21-00-00.txt"
    assert "1920x1080 @ 60 fps, 6000 kbps auto-bitrate" in text
    assert "6000 / " in text and "auto-pilot 5400" in text and "render 60 fps" in text
    assert text.count("Twitch (horizontal)") >= 3  # setup + a line every 5 s
    assert "averaged 6000 kbps" in text and "Ended after" in text
    assert "SECRET" not in text
