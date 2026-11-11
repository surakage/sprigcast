import shutil
import socket
import subprocess
import time

import av
import numpy as np
import pytest

from sprigcast.output import stream as stream_mod
from sprigcast.output.stream import Streamer, stream_url
from sprigcast.render.engine import Frame

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or "libx264" not in av.codecs_available, reason="needs ffmpeg CLI and libx264"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rtmp_server(port: int, out):
    """ffmpeg as a one-shot RTMP server that saves what it receives."""
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-listen", "1", "-i",
         f"rtmp://127.0.0.1:{port}/app/secretkey", "-c", "copy", "-y", str(out)],
        stdin=subprocess.DEVNULL,
    )


def feed(st: Streamer, seconds: float, fps: int = 30, w: int = 160, h: int = 90):
    data = bytes(w * h * 4)
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        st.push_frame(Frame("horizontal", w, h, data, time.perf_counter()))
        st.push_audio(np.zeros((1024, 2), dtype=np.float32))
        time.sleep(1 / fps)


def wait_for(predicate, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_stream_url_and_key_redaction():
    assert stream_url("rtmp://live.twitch.tv/app/", "live_123") == "rtmp://live.twitch.tv/app/live_123"
    st = Streamer("Twitch", "rtmp://x/app", "live_123", 16, 16, 30, 1000, "libx264", 0.0)
    assert st.redact("Connection refused: rtmp://x/app/live_123") == "Connection refused: rtmp://x/app/****"


@needs_ffmpeg
def test_streams_video_and_audio_to_rtmp_server(tmp_path):
    port, out = free_port(), tmp_path / "got.flv"
    server = rtmp_server(port, out)
    time.sleep(0.5)
    st = Streamer("Local", f"rtmp://127.0.0.1:{port}/app", "secretkey", 160, 90, 30, 1000, "libx264", time.perf_counter())
    st.start()
    try:
        feed(st, 1.5)
        assert st.connected and st.status == "live"
        assert st.bytes_out > 0 and st.measure_kbps() >= 0
    finally:
        st.stop()
        server.wait(timeout=10)
    with av.open(str(out)) as f:
        assert sorted(s.type for s in f.streams) == ["audio", "video"]
        assert sum(1 for _ in f.decode(video=0)) > 20


@needs_ffmpeg
def test_reconnects_when_server_appears_later(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_mod, "RETRY_FIRST", 0.3)
    port, out = free_port(), tmp_path / "late.flv"
    st = Streamer("Local", f"rtmp://127.0.0.1:{port}/app", "secretkey", 160, 90, 30, 1000, "libx264", time.perf_counter())
    st.start()
    server = None
    try:
        feed(st, 0.5)
        assert wait_for(lambda: st.reconnects >= 1)
        assert "disconnected" in st.status and "secretkey" not in st.status
        server = rtmp_server(port, out)
        end = time.time() + 10
        while not st.connected and time.time() < end:
            feed(st, 0.2)
        assert st.connected
        feed(st, 0.5)
    finally:
        st.stop()
        if server is not None:
            server.wait(timeout=10)
    assert out.exists() and out.stat().st_size > 0


class _C:
    def __init__(self, w, h, fps=60):
        self.width, self.height, self.fps = w, h, fps


def test_presets_and_sizes():
    from sprigcast.output.stream import SERVICES, preset_size, sizes_for

    assert preset_size(SERVICES["twitch"], _C(1920, 1080)) == (1920, 1080)
    assert preset_size(SERVICES["tiktok"], _C(1080, 1920)) == (720, 1280)
    assert sizes_for(_C(1280, 720)) == [(1280, 720), (854, 480)]  # never upscales


def test_bandwidth_test_url():
    assert stream_url("rtmp://live.twitch.tv/app", "k", bandwidth_test=True) == "rtmp://live.twitch.tv/app/k?bandwidthtest=true"


def test_health_levels(monkeypatch):
    monkeypatch.setattr(stream_mod, "GRACE_SECONDS", 0.0)
    st = Streamer("T", "rtmp://x/app", "k", 16, 16, 30, 1000, "libx264", 0.0)
    assert st.health() == ("warning", "connecting")
    st.status = "disconnected: Broken pipe; retrying in 2 s"
    assert st.health()[0] == "bad"
    st.connected = True
    st.health()
    st.frames += 30
    time.sleep(0.01)
    assert st.health() == ("good", "live")
    st.frames += 20
    st.dropped += 10
    st.mux_seconds += 1.0  # mostly waiting on the network
    level, reason = st.health()
    assert level == "bad" and "upload" in reason


def test_health_grace_after_connect_and_warnings_are_held(monkeypatch):
    st = Streamer("T", "rtmp://x/app", "k", 16, 16, 30, 1000, "libx264", 0.0)
    st.connected, st._connected_at = True, time.monotonic()
    st.health()
    st.frames, st.dropped = 20, 10  # backlog from the handshake: inside the grace period
    time.sleep(0.01)
    assert st.health() == ("good", "live")
    st._connected_at -= 10  # well after connecting
    st.health()
    st.frames, st.dropped = 40, 11
    time.sleep(0.01)
    level, reason = st.health()
    assert level == "warning" and "falling behind" in reason
    st.frames += 30
    time.sleep(0.01)
    assert st.health() == (level, reason)  # recovered, but still shown for a while
    st._held = (level, reason, time.monotonic() - 1)
    st.frames += 30
    time.sleep(0.01)
    assert st.health() == ("good", "live")


def test_summary_names_the_bottleneck():
    st = Streamer("Twitch (horizontal)", "rtmp://x/app", "k", 16, 16, 60, 1000, "libx264", 0.0)
    assert "never connected" in st.summary()
    st.frames, st.dropped, st.encode_seconds = 900, 100, 900 * 0.014  # 14 ms of a 16.7 ms budget
    assert "100 of 1000 frames dropped (10.0%)" in st.summary()
    assert "prepare frames" in st.summary()
    st.started_at -= 10
    st.mux_seconds, st.encode_seconds = 8.0, 900 * 0.004
    assert "upload can't keep up" in st.summary()


class _SlowNetwork:
    """Wraps a real output container; one mux call stalls, like a congested upload."""

    def __init__(self, container, stall_after=10, stall=1.5):
        self._c, self._n, self._after, self._stall = container, 0, stall_after, stall

    def __getattr__(self, name):
        return getattr(self._c, name)

    def mux(self, packet):
        self._n += 1
        if self._n == self._after:
            time.sleep(self._stall)
        return self._c.mux(packet)


def test_a_network_stall_delays_the_stream_but_drops_nothing(tmp_path, monkeypatch):
    out = tmp_path / "stall.flv"

    class SlowStreamer(Streamer):
        def _open_container(self):
            return _SlowNetwork(av.open(str(out), "w", format="flv"))

    st = SlowStreamer("Local", "rtmp://unused/app", "k", 160, 90, 30, 1000, "libx264", time.perf_counter())
    st.start()
    try:
        feed(st, 3.0)
    finally:
        st.stop()
    assert st.longest_stall >= 1.4
    assert st.dropped == 0
    assert st.frames >= 80
    with av.open(str(out)) as f:
        assert sum(1 for _ in f.decode(video=0)) == st.frames


def test_a_long_outage_skips_frames_instead_of_growing_forever(monkeypatch):
    st = Streamer("T", "rtmp://x/app", "k", 16, 16, 30, 1000, "libx264", 0.0)
    st.connected = True
    st._backlog_bytes = int(5 * 1000 * 1000 / 8)  # 5 s of video waiting to upload
    st.push_frame(Frame("h", 16, 16, bytes(16 * 16 * 4), 0.0))
    assert st.dropped == 1 and st._video.qsize() == 0
    st._backlog_bytes = int(1 * 1000 * 1000 / 8)  # caught up below the resume mark
    st.push_frame(Frame("h", 16, 16, bytes(16 * 16 * 4), 0.1))
    assert st._video.qsize() == 1
