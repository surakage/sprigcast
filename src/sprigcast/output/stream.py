"""Live streaming over RTMP: one Streamer per output, each with its own encoder.

A Streamer is a Recorder whose file is an RTMP connection. When the connection drops it
waits (2 s, then longer, up to 30 s) and connects again, until you stop it.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from sprigcast.output.autopilot import Autopilot
from sprigcast.output.recorder import Recorder, rate_limits, video_options

log = logging.getLogger(__name__)

RETRY_FIRST, RETRY_MAX = 2.0, 30.0
GRACE_SECONDS = 5.0  # right after connecting, frames queued during the handshake are still going out
HOLD_SECONDS = 10.0  # a warning stays on screen this long so it can be read
SEVERITY = {"good": 0, "warning": 1, "bad": 2}
# Encoded packets wait in a send buffer so a slow network never stalls the encoder. Up to
# MAX_BACKLOG seconds of video can queue up (a few MB); past that, new frames are skipped until
# the backlog is back under RESUME_BACKLOG.
MAX_BACKLOG, RESUME_BACKLOG = 4.0, 2.0


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
    enhanced: object | None = None  # output.enhanced.EnhancedConfig: several qualities over one connection
    canvases: list | None = None  # with enhanced: the canvases its tracks come from (index = canvas_index)
    output_id: str | None = None  # the scene collection Output this target came from
    auto_bitrate: bool = False  # lower the bitrate when the upload struggles (output/autopilot.py)


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
        encoder=None,
        auto_bitrate: bool = False,
    ):
        super().__init__(
            Path(f"stream {name}"), width, height, fps, bitrate_kbps, codec, start_time, "flv", out_size, encoder
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
        self._send_q: queue.Queue = queue.Queue()
        self._send_error: Exception | None = None
        self._backlog_bytes = 0
        self._congested = False
        self._sender: threading.Thread | None = None
        self._connected_at = 0.0
        # Auto-pilot needs a target bitrate to steer; constant-quality encoding has none.
        self.autopilot = Autopilot(bitrate_kbps) if auto_bitrate and self.encoder.rate_control != "cqp" else None
        self._want_kbps = self._applied_kbps = bitrate_kbps
        self._autopilot_at = 0.0

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
        behind = self.backlog_seconds()
        loss = lost / max(1, sent + lost)
        why = "upload too slow for this bitrate" if waiting_on_network else "encoder overloaded"
        if loss > 0.05:
            return "bad", f"dropping {loss:.0%} of frames: {why}"
        if lost or backlog > 0.5:
            return "warning", f"falling behind: {why}"
        if behind > 1.0:
            return "warning", f"network slow, {behind:.1f} s behind"
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
            f"waiting on the network {waiting:.0%} of the time, longest stall {self.longest_stall:.1f} s"
        )
        if self.reconnects:
            line += f"; {self.reconnects} reconnects"
        if self.autopilot and self.autopilot.steps_down:
            line += (f"; auto-pilot lowered the bitrate {self.autopilot.steps_down} times"
                     f" (lowest {self.autopilot.lowest_kbps} kbps)")
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

    @staticmethod
    def default_encoder():
        from sprigcast.core.scene import EncoderSettings

        return EncoderSettings()  # constant bitrate, what Twitch and others ask for

    # Sending: the encoder thread queues packets, a sender thread writes them to the network ----------

    @property
    def current_kbps(self) -> int:
        return self.autopilot.current_kbps if self.autopilot else self.bitrate_kbps

    def backlog_seconds(self) -> float:
        return self._backlog_bytes * 8 / (self.current_kbps * 1000)

    def _video_options(self) -> dict[str, str]:
        return video_options(self.codec, self.encoder, self.bitrate_kbps, hrd=self.autopilot is None)

    def _steer(self, backlog: float) -> None:
        """About once a second: let auto-pilot pick the bitrate; the encoder thread applies it."""
        now = time.monotonic()
        if self.autopilot is None or now - self._autopilot_at < 1.0 or now - self._connected_at < GRACE_SECONDS:
            return
        self._autopilot_at = now
        kbps = self.autopilot.update(now, backlog)
        if kbps is not None:
            log.info("stream %s: auto-pilot bitrate %d kbps", self.name, kbps)
            self._want_kbps = kbps

    def _write_video(self, container, stream, frame, pts: int | None = None) -> None:
        if self._want_kbps != self._applied_kbps:
            k = self._want_kbps
            if self._direct is not None:  # SprigCast's own NVENC: reconfigured in place
                try:
                    self._direct.set_bitrate(k)
                except Exception as exc:  # keep streaming at the old bitrate rather than stop
                    log.warning("stream %s: couldn't change the bitrate to %d kbps: %s", self.name, k, exc)
            else:
                ctx = stream.codec_context
                ctx.bit_rate = k * 1000
                peak, buffer = rate_limits(self.codec, self.encoder, k)
                if peak:
                    ctx.max_bit_rate, ctx.rc_buffer_size = peak * 1000, buffer * 1000
            self._applied_kbps = k
        super()._write_video(container, stream, frame, pts)

    def push_frame(self, frame) -> None:
        if self.connected:
            backlog = self.backlog_seconds()
            self._steer(backlog)
            if self._congested and backlog < RESUME_BACKLOG:
                self._congested = False
            elif backlog > MAX_BACKLOG:
                self._congested = True
            if self._congested:
                if self._assign_pts(frame) is not None:  # a frame we would have sent
                    self.dropped += 1
                return
        super().push_frame(frame)

    def _open(self):
        container, video, audio = super()._open()
        self._applied_kbps = self.bitrate_kbps  # a fresh encoder starts at the set bitrate; _write_video steers it
        self._send_q = queue.Queue()
        self._send_error = None
        self._backlog_bytes = 0
        self._sender = threading.Thread(
            target=self._send_loop, args=(container,), name=f"sprigcast-send-{self.name}", daemon=True
        )
        self._sender.start()
        return container, video, audio

    def _mux(self, container, packets) -> None:
        if self._send_error is not None:
            raise self._send_error  # the connection failed: end this session so _run reconnects
        for packet in packets:
            self._backlog_bytes += packet.size
            self._send_q.put(packet)

    def _send_loop(self, container) -> None:
        while True:
            packet = self._send_q.get()
            if packet is None:
                return
            if self._send_error is not None:
                self._backlog_bytes -= packet.size  # connection already failed: discard
                continue
            try:
                Recorder._mux(self, container, [packet])  # timing, bytes_out and longest_stall
            except Exception as exc:
                self._send_error = exc
            finally:
                self._backlog_bytes -= packet.size
            if not self.connected and self.bytes_out and self._send_error is None:
                self.connected = True
                self._connected_at = time.monotonic()
                self.status = "live"

    def _before_close(self, container) -> bool:
        """Let the sender finish what is queued (briefly), then stop it before the container closes."""
        self._send_q.put(None)
        self._sender.join(timeout=3.0 if self._send_error is None else 0.5)
        if self._sender.is_alive():
            log.warning("stream %s: sender still blocked on the network; abandoning the connection", self.name)
            return False  # closing now would free the connection under the sender's feet
        if self._send_error is not None:
            raise self._send_error
        return True

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
            self._congested = False
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
    def start(cls, engine, targets: list[StreamTarget], codec: str, encoder=None) -> "StreamSession":
        from sprigcast.output.enhanced_stream import EnhancedStreamer

        start_time = time.perf_counter()
        streamers, taps = [], []
        for t in targets:
            c = t.canvas
            if t.enhanced is not None:
                st = EnhancedStreamer(t.name, t.enhanced, t.canvases or [c], start_time, user_key=t.key)
                taps += [(canvas_id, st.push_for(canvas_id)) for canvas_id in st.canvas_ids()]
            else:
                fps = min(t.fps or c.fps, c.fps)
                st = Streamer(t.name, t.server, t.key, c.width, c.height, fps, t.bitrate_kbps, codec, start_time,
                              t.size, t.bandwidth_test, encoder, auto_bitrate=t.auto_bitrate)
                taps.append((c.id, st.push_frame, st.gpu_size))
            st.output_id = t.output_id or t.name
            st.start()
            streamers.append((c.id, st))
        session = cls(engine, streamers)
        session.taps = taps
        for canvas_id, callback, *size in taps:
            engine.add_frame_tap(canvas_id, callback, *size)
        for _, st in streamers:
            engine.mixer.add_tap(st.push_audio)
        return session

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def stop(self) -> None:
        taps = getattr(self, "taps", None) or [(canvas_id, st.push_frame) for canvas_id, st in self.streamers]
        for canvas_id, callback, *_ in taps:
            self.engine.remove_frame_tap(canvas_id, callback)
        for _, st in self.streamers:
            self.engine.mixer.remove_tap(st.push_audio)
        for _, st in self.streamers:
            st.stop(timeout=5)
