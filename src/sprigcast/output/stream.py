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


@dataclass(frozen=True)
class Service:
    label: str
    server: str  # ingest URL without the key; empty means the user must paste one
    max_kbps: int  # what the platform accepts for video
    canvas: str  # which canvas suits it
    help: str = ""


SERVICES: dict[str, Service] = {
    "twitch": Service("Twitch", "rtmp://live.twitch.tv/app", 6000, "horizontal",
                      "Key: Twitch Creator Dashboard → Settings → Stream → Primary Stream key."),
    "youtube": Service("YouTube", "rtmp://a.rtmp.youtube.com/live2", 12000, "horizontal",
                       "Key: YouTube Studio → Go live → Stream → Stream key."),
    "kick": Service("Kick", "", 8000, "horizontal",
                    "Server and key: Kick dashboard → Settings → Stream URL & Key."),
    "tiktok": Service("TikTok", "", 6000, "vertical",
                      "Server and key: TikTok LIVE Producer (needs LIVE access), copy Server URL and Stream key."),
    "custom": Service("Custom RTMP", "", 50000, "horizontal", "Any RTMP or RTMPS server URL plus its stream key."),
}


def stream_url(server: str, key: str) -> str:
    return f"{server.rstrip('/')}/{key}"


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
    ):
        super().__init__(Path(f"stream {name}"), width, height, fps, bitrate_kbps, codec, start_time, "flv")
        self.name = name
        self._url = stream_url(server, key)
        self._key = key
        self.connected = False
        self.reconnects = 0
        self.status = "connecting"

    def redact(self, text: str) -> str:
        """Error text from FFmpeg can contain the URL; never let the key reach the screen or a log."""
        return text.replace(self._key, "****") if self._key else text

    # Recorder hooks ------------------------------------------------------------

    def push_frame(self, frame) -> None:
        if self._stop.is_set():
            return
        try:
            self._video.put_nowait(frame)
        except queue.Full:
            if self.connected:  # frames lost while (re)connecting are expected, not an encoder problem
                self.dropped += 1

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
            self._last_pts, self._samples = -1, 0
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
    def start(cls, engine, targets: list[tuple], codec: str) -> "StreamSession":
        """targets: (name, canvas, server, key, bitrate_kbps) tuples."""
        start_time = time.perf_counter()
        streamers = []
        for name, canvas, server, key, bitrate in targets:
            st = Streamer(name, server, key, canvas.width, canvas.height, canvas.fps, bitrate, codec, start_time)
            st.start()
            streamers.append((canvas.id, st))
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
