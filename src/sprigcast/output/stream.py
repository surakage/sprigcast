"""Live streaming over RTMP: one Streamer per output, each with its own encoder.

A Streamer is a Recorder whose file is an RTMP connection. When the connection drops it
waits (2 s, then longer, up to 30 s) and connects again, until you stop it.
"""

from __future__ import annotations

import logging
import queue
import time
from dataclasses import dataclass
from pathlib import Path

from sprigcast.output.recorder import Recorder

log = logging.getLogger(__name__)

RETRY_FIRST, RETRY_MAX = 2.0, 30.0
GRACE_SECONDS = 5.0  # right after connecting, frames queued during the handshake are still going out
HOLD_SECONDS = 10.0  # a warning stays on screen this long so it can be read
SEVERITY = {"good": 0, "warning": 1, "bad": 2}


@dataclass(frozen=True)
class Service:
    label: str
    server: str  # ingest URL without the key; empty means the user must paste one
    max_kbps: int  # what the platform accepts for video
    canvas: str  # which canvas suits it
    help: str = ""
    # Preset applied when you pick the service: longest side of the encoded picture, fps, bitrate.
    long_side: int = 1920
    fps: int = 60
    kbps: int = 6000
    bandwidth_test: bool = False  # supports a test mode that never shows the stream to viewers


SERVICES: dict[str, Service] = {
    "twitch": Service("Twitch", "rtmp://live.twitch.tv/app", 6000, "horizontal",
                      "Key: Twitch Creator Dashboard → Settings → Stream → Primary Stream key.",
                      1920, 60, 6000, bandwidth_test=True),
    "youtube": Service("YouTube", "rtmp://a.rtmp.youtube.com/live2", 20000, "horizontal",
                       "Key: YouTube Studio → Go live → Stream → Stream key.", 1920, 60, 9000),
    "kick": Service("Kick", "", 8000, "horizontal",
                    "Server and key: Kick dashboard → Settings → Stream URL & Key.", 1920, 60, 8000),
    "tiktok": Service("TikTok", "", 6000, "vertical",
                      "Server and key: TikTok LIVE Producer (needs LIVE access), copy Server URL and Stream key.",
                      1280, 30, 4000),
    "custom": Service("Custom RTMP", "", 50000, "horizontal", "Any RTMP or RTMPS server URL plus its stream key.",
                      1920, 60, 6000),
}

# Encoded sizes offered for each canvas shape.
HORIZONTAL_SIZES = [(1920, 1080), (1600, 900), (1280, 720), (854, 480)]
VERTICAL_SIZES = [(1080, 1920), (720, 1280), (540, 960)]


def sizes_for(canvas) -> list[tuple[int, int]]:
    """Output sizes that keep the canvas shape, never larger than the canvas."""
    options = HORIZONTAL_SIZES if canvas.width >= canvas.height else VERTICAL_SIZES
    fitting = [(w, h) for w, h in options if w <= canvas.width and h <= canvas.height]
    return fitting or [(canvas.width, canvas.height)]


def preset_size(service: Service, canvas) -> tuple[int, int]:
    """The largest offered size whose longest side fits the service's preset."""
    options = sizes_for(canvas)
    return next(((w, h) for w, h in options if max(w, h) <= service.long_side), options[-1])


def stream_url(server: str, key: str, bandwidth_test: bool = False) -> str:
    """Twitch's bandwidth test sends the stream to Twitch without showing it to anyone."""
    return f"{server.rstrip('/')}/{key}" + ("?bandwidthtest=true" if bandwidth_test else "")


@dataclass
class StreamTarget:
    name: str  # shown in the status, e.g. "Twitch (horizontal)"
    canvas: object  # core.scene.Canvas
    server: str
    key: str
    bitrate_kbps: int
    size: tuple[int, int] | None = None  # encoded size; None = canvas size
    fps: int | None = None  # None = canvas fps
    bandwidth_test: bool = False


class Streamer(Recorder):
    def __init__(
        self,
        name: str,
        server: str,
        key: str,
        width: int,
        height: int,
        fps: int,
        bitrate_kbps: int,
        codec: str,
        start_time: float,
        out_size: tuple[int, int] | None = None,
        bandwidth_test: bool = False,
    ):
        super().__init__(
            Path(f"stream {name}"), width, height, fps, bitrate_kbps, codec, start_time, "flv", out_size
        )
        self.name = name
        self.bandwidth_test = bandwidth_test
        self._url = stream_url(server, key, bandwidth_test)
        self._key = key
        self.connected = False
        self.reconnects = 0
        self.status = "connecting"
        self._health: tuple | None = None
        self._held: tuple[str, str, float] | None = None  # (level, reason, shown until)
        self._connected_at = 0.0

    def health(self) -> tuple[str, str]:
        """("good" | "warning" | "bad", short reason), comparing against the previous call (about 1 s ago).

        Connecting shows as a warning, a drop in connection as bad. A warning or worse stays shown
        for HOLD_SECONDS even after things recover, unless something more severe replaces it.
        """
        now = time.monotonic()
        level, reason = self._measure_health(now)
        held = self._held
        if held and now < held[2] and SEVERITY[held[0]] >= SEVERITY[level] and self.connected:
            return held[0], held[1]
        if level != "good" and self.connected:  # connection states speak for themselves, no need to hold them
            self._held = (level, reason, now + HOLD_SECONDS)
        return level, reason

    def _measure_health(self, now: float) -> tuple[str, str]:
        sample = (now, self.frames, self.dropped, self.mux_seconds)
        last, self._health = self._health, sample
        if not self.connected:
            return ("warning" if self.status.startswith(("connecting", "reconnecting")) else "bad"), self.status
        if last is None or now <= last[0] or now - self._connected_at < GRACE_SECONDS:
            return "good", "live"
        sent, lost = sample[1] - last[1], sample[2] - last[2]
        waiting_on_network = (sample[3] - last[3]) / (now - last[0]) > 0.5
        backlog = self._video.qsize() / self._video.maxsize
        loss = lost / max(1, sent + lost)
        why = "upload too slow for this bitrate" if waiting_on_network else "encoder overloaded"
        if loss > 0.05:
            return "bad", f"dropping {loss:.0%} of frames: {why}"
        if lost or backlog > 0.5:
            return "warning", f"falling behind: {why}"
        return "good", "live"

    def redact(self, text: str) -> str:
        """Error text from FFmpeg can contain the URL; never let the key reach the screen or a log."""
        return text.replace(self._key, "****") if self._key else text

    def summary(self) -> str:
        """One line for after the stream ends: how much was dropped and what was slow."""
        total = self.frames + self.dropped
        if not total:
            return f"{self.name}: never connected ({self.status})"
        loss = self.dropped / total
        encode_ms = 1000 * self.encode_seconds / max(1, self.frames)
        budget_ms = 1000 / self.fps
        waiting = self.mux_seconds / max(1e-6, time.monotonic() - self.started_at)
        line = (
            f"{self.name}: {self.dropped} of {total} frames dropped ({loss:.1%}). "
            f"Encoding took {encode_ms:.1f} ms per frame (budget {budget_ms:.1f} ms); "
            f"waiting on the network {waiting:.0%} of the time"
        )
        if self.reconnects:
            line += f"; {self.reconnects} reconnects"
        if loss > 0.01:
            if waiting > 0.5:
                line += ". Your upload can't keep up: lower this output's bitrate."
            elif encode_ms > 0.6 * budget_ms:
                line += ". Your PC can't prepare frames fast enough: lower this output's resolution or frame rate."
            else:
                line += ". Neither looks maxed out, so the drops were probably brief hiccups."
        return line + ("" if line.endswith(".") else ".")

    # Recorder hooks ------------------------------------------------------------

    def _counts_drops(self) -> bool:
        return self.connected  # frames lost while (re)connecting are expected, not an encoder problem

    def push_audio(self, block) -> None:
        if self._stop.is_set():
            return
        try:
            self._audio.put_nowait(block)
        except queue.Full:
            pass

    def _open_container(self):
        import av

        return av.open(self._url, "w", format="flv")

    def _video_options(self) -> dict[str, str]:
        k = self.bitrate_kbps
        rate = {"maxrate": f"{k}k", "bufsize": f"{k}k"}  # constant bitrate, what Twitch and others ask for
        if self.codec == "h264_nvenc":
            return {"preset": "p5", "tune": "hq", "rc": "cbr", "profile": "high", "spatial-aq": "1", **rate}
        return {"preset": "veryfast", "profile": "high", "nal-hrd": "cbr", **rate}

    def _mux(self, container, packets) -> None:
        super()._mux(container, packets)
        if not self.connected and self.bytes_out:
            self.connected = True
            self._connected_at = time.monotonic()
            self.status = "live"

    # Stream thread -------------------------------------------------------------

    def _drain(self) -> None:
        for q in (self._video, self._audio):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def _run(self) -> None:
        delay = RETRY_FIRST
        while not self._stop.is_set():
            self._drain()  # anything queued while disconnected would arrive late and out of sync
            self.start_time = time.perf_counter()
            self._last_pts, self._last_queued, self._samples = -1, -1, 0
            self.connected = False
            self.status = "connecting" if not self.reconnects else f"reconnecting (try {self.reconnects})"
            began = time.monotonic()
            try:
                self._session()
                self.status = "stopped"
                return
            except Exception as exc:
                message = self.redact(str(exc) or exc.__class__.__name__)
                log.warning("stream %s dropped: %s", self.name, message)
                if time.monotonic() - began > 60:
                    delay = RETRY_FIRST  # it had been running fine: start the backoff again
                self.reconnects += 1
                hint = "" if self.bytes_out else " (check the server and stream key)"
                self.status = f"disconnected: {message}{hint}; retrying in {delay:.0f} s"
                if self._stop.wait(delay):
                    self.status = "stopped"
                    return
                delay = min(delay * 2, RETRY_MAX)


class StreamSession:
    """Every live output at once. Each output encodes its own canvas; all share the mixed audio."""

    def __init__(self, engine, streamers: list[tuple[str, Streamer]]):
        self.engine = engine
        self.streamers = streamers  # (canvas id, streamer)
        self.started_at = time.monotonic()

    @classmethod
    def start(cls, engine, targets: list[StreamTarget], codec: str) -> "StreamSession":
        start_time = time.perf_counter()
        streamers = []
        for t in targets:
            c = t.canvas
            fps = min(t.fps or c.fps, c.fps)
            st = Streamer(t.name, t.server, t.key, c.width, c.height, fps, t.bitrate_kbps, codec, start_time,
                          t.size, t.bandwidth_test)
            st.start()
            streamers.append((c.id, st))
        session = cls(engine, streamers)
        for canvas_id, st in streamers:
            engine.add_frame_tap(canvas_id, st.push_frame)
            engine.mixer.add_tap(st.push_audio)
        return session

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def stop(self) -> None:
        for canvas_id, st in self.streamers:
            self.engine.remove_frame_tap(canvas_id, st.push_frame)
            self.engine.mixer.remove_tap(st.push_audio)
        for _, st in self.streamers:
            st.stop(timeout=5)
