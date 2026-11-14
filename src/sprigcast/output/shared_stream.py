"""One encoder for several destinations that want the same picture.

Going live to Twitch and YouTube from the same canvas, at the same size, frame rate and bitrate,
used to run two identical encoders. Here one ``SharedStreamEncoder`` encodes and each destination
is a ``StreamLink``: its own connection, sender thread, reconnects and health, fed the same packets.

A link that falls behind (slow upload) skips packets until it has caught up and a keyframe comes,
so it never holds the encoder or the other destinations back. A link that drops reconnects on its
own and starts again at a fresh keyframe; the others carry on. With auto bitrate on, the encoder
follows the link that is struggling most, since they all share one bitrate.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from sprigcast.output.autopilot import Autopilot
from sprigcast.output.recorder import measured_kbps, rate_limits
from sprigcast.output.replay import ReplayBuffer, _Packet
from sprigcast.output.stream import (
    GRACE_SECONDS,
    HOLD_SECONDS,
    MAX_BACKLOG,
    RESUME_BACKLOG,
    RETRY_FIRST,
    RETRY_MAX,
    SEVERITY,
    stream_url,
)

log = logging.getLogger(__name__)


def share_key(target) -> tuple | None:
    """What two stream targets must have in common to share an encoder; None when this one can't."""
    if target.enhanced is not None:
        return None
    c = target.canvas
    size = target.size or (c.width, c.height)
    return c.id, tuple(size), min(target.fps or c.fps, c.fps), target.bitrate_kbps, bool(target.auto_bitrate)


class SharedStreamEncoder(ReplayBuffer):
    """Encodes a canvas once for several StreamLinks. Keeps no replay (seconds=0)."""

    def __init__(self, canvas, fps: int, bitrate_kbps: int, codec: str, start_time: float, out_size=None,
                 encoder=None, auto_bitrate: bool = False):
        from sprigcast.core.scene import EncoderSettings

        super().__init__(f"stream-{canvas.id}", canvas.width, canvas.height, fps, bitrate_kbps, codec, start_time,
                         0.0, "flv", encoder or EncoderSettings(), out_size, tracks=("stream",))
        self.links: list[StreamLink] = []
        self.autopilot = Autopilot(bitrate_kbps) if auto_bitrate and self.encoder.rate_control != "cqp" else None
        self._want_kbps = self._applied_kbps = bitrate_kbps
        self._autopilot_at = 0.0

    @property
    def current_kbps(self) -> int:
        return self.autopilot.current_kbps if self.autopilot else self.bitrate_kbps

    def request_keyframe(self) -> None:
        self._force_key = True  # read on the encoder thread before the next frame

    def _video_options(self) -> dict[str, str]:
        from sprigcast.output.recorder import video_options

        opts = video_options(self.codec, self.encoder, self.bitrate_kbps, hrd=self.autopilot is None)
        # A link (re)joining asks for a keyframe: make it a clean IDR a new viewer can start from.
        if self.codec in ("h264_nvenc", "libx264"):
            opts["forced-idr"] = "1"
        elif self.codec in ("h264_amf", "h264_qsv") and "profile" in opts:
            opts["forced_idr"] = "1"
        return opts

    def _open(self):
        result = super()._open()
        self._applied_kbps = self.bitrate_kbps
        return result

    def _keep(self, p: _Packet) -> None:
        pass  # no replay to keep: packets only pass through to the links

    def _on_packet(self, p: _Packet) -> None:
        for link in self.links:
            link.offer(p)

    def _steer(self) -> None:
        now = time.monotonic()
        if self.autopilot is None or now - self._autopilot_at < 1.0:
            return
        self._autopilot_at = now
        live = [link for link in self.links if link.connected and now - link._connected_at >= GRACE_SECONDS]
        if not live:
            return
        kbps = self.autopilot.update(now, max(link.backlog_seconds() for link in live))
        if kbps is not None:
            log.info("shared stream %s: auto-pilot bitrate %d kbps", self.canvas_id, kbps)
            self._want_kbps = kbps

    def _write_video(self, container, stream, frame, pts: int | None = None) -> None:
        self._steer()
        if self._want_kbps != self._applied_kbps:
            k = self._want_kbps
            if self._direct is not None:
                try:
                    self._direct.set_bitrate(k)
                except Exception as exc:
                    log.warning("shared stream: couldn't change the bitrate to %d kbps: %s", k, exc)
            else:
                ctx = stream.codec_context
                ctx.bit_rate = k * 1000
                peak, buffer = rate_limits(self.codec, self.encoder, k)
                if peak:
                    ctx.max_bit_rate, ctx.rc_buffer_size = peak * 1000, buffer * 1000
            self._applied_kbps = k
        super()._write_video(container, stream, frame, pts)


class StreamLink:
    """One destination of a SharedStreamEncoder: a connection with its own sender, reconnects and health.

    Looks like a Streamer to the rest of the app (status, health(), summary(), stats()...).
    """

    def __init__(self, name: str, server: str, key: str, shared: SharedStreamEncoder, bandwidth_test: bool = False,
                 open_container=None):
        self.name = name
        self.shared = shared
        self.bandwidth_test = bandwidth_test
        self._url = stream_url(server, key, bandwidth_test)
        self._key = key
        self._open_container = open_container or self._default_open
        self.output_id: str | None = None
        self.connected = False
        self.status = "connecting"
        self.reconnects = 0
        self.frames = self.dropped = self.bytes_out = 0
        self.mux_seconds = self.longest_stall = 0.0
        self.started_at = time.monotonic()
        self.error: str | None = None
        self._q: queue.Queue = queue.Queue()
        self._backlog_bytes = 0
        self._ready = False  # connected and has stream headers: packets are wanted
        self._started = False  # the first keyframe went out (everything before it is skipped)
        self._congested = False
        self._zero = 0.0  # seconds of the first packet sent on this connection
        self._connected_at = 0.0
        self._health: tuple | None = None
        self._held: tuple[str, str, float] | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"sprigcast-link-{name}", daemon=True)

    # Streamer look-alike -----------------------------------------------------------------

    @property
    def fps(self) -> int:
        return self.shared.fps

    @property
    def out_width(self) -> int:
        return self.shared.out_width

    @property
    def out_height(self) -> int:
        return self.shared.out_height

    @property
    def bitrate_kbps(self) -> int:
        return self.shared.bitrate_kbps

    @property
    def current_kbps(self) -> int:
        return self.shared.current_kbps

    @property
    def autopilot(self):
        return self.shared.autopilot

    @property
    def codec(self) -> str:
        return self.shared.codec

    @property
    def encoder(self):
        return self.shared.encoder

    @property
    def _direct(self):
        return self.shared._direct

    @property
    def encode_seconds(self) -> float:
        return self.shared.encode_seconds * self.frames / max(1, self.shared.frames)

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def push_audio(self, block) -> None:  # the shared encoder takes the audio
        pass

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._q.put(None)
        self._thread.join(timeout)

    def backlog_seconds(self) -> float:
        return self._backlog_bytes * 8 / (max(1, self.current_kbps) * 1000)

    def redact(self, text: str) -> str:
        return text.replace(self._key, "****") if self._key else text

    def measure_kbps(self) -> float:
        """Output bitrate over about the last few seconds (call it about once a second)."""
        return measured_kbps(self, time.monotonic())

    def stats(self) -> dict:
        total = self.frames + self.dropped
        running = max(1e-6, time.monotonic() - self.started_at)
        return {
            "frames": self.frames,
            "dropped": self.dropped,
            "dropped_pct": 100 * self.dropped / total if total else 0.0,
            "encode_ms": 1000 * self.shared.encode_seconds / max(1, self.shared.frames),
            "budget_ms": 1000 / self.fps,
            "network_pct": 100 * self.mux_seconds / running,
            "longest_stall_s": self.longest_stall,
            "megabytes": self.bytes_out / 1e6,
            "backlog_s": self.backlog_seconds(),
            "size": f"{self.out_width}×{self.out_height}",
            "fps": self.fps,
        }

    def health(self) -> tuple[str, str]:
        now = time.monotonic()
        level, reason = self._measure_health(now)
        held = self._held
        if held and now < held[2] and SEVERITY[held[0]] >= SEVERITY[level] and self.connected:
            return held[0], held[1]
        if level != "good" and self.connected:
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
        loss = lost / max(1, sent + lost)
        if loss > 0.05:
            return "bad", f"dropping {loss:.0%} of frames: upload too slow for this bitrate"
        if lost:
            return "warning", "falling behind: upload too slow for this bitrate"
        behind = self.backlog_seconds()
        if behind > 1.0:
            return "warning", f"network slow, {behind:.1f} s behind"
        return "good", "live"

    def summary(self) -> str:
        total = self.frames + self.dropped
        if not total:
            return f"{self.name}: never connected ({self.status})"
        loss = self.dropped / total
        waiting = self.mux_seconds / max(1e-6, time.monotonic() - self.started_at)
        live_s = max(1e-6, time.monotonic() - (self._connected_at or self.started_at))
        average = f"averaged {self.bytes_out * 8 / 1000 / live_s:.0f} kbps (set {self.bitrate_kbps})"
        line = (f"{self.name}: {self.dropped} of {total} frames dropped ({loss:.1%}), {average}, sharing an encoder; "
                f"waiting on the network {waiting:.0%} of the time, longest stall {self.longest_stall:.1f} s")
        if self.reconnects:
            line += f"; {self.reconnects} reconnects"
        if loss > 0.01 and waiting > 0.5:
            line += ". Your upload can't keep up: lower this output's bitrate"
        return line + "."

    # Encoder thread: packets in ----------------------------------------------------------

    def offer(self, p: _Packet) -> None:
        if not self._ready:
            return
        if not self._started:
            if not (p.kind == "video" and p.keyframe):
                return  # a connection starts at a keyframe
            self._started, self._zero = True, p.seconds
        if self._congested:
            if self.backlog_seconds() < RESUME_BACKLOG and p.kind == "video" and p.keyframe:
                self._congested = False  # caught up: carry on from this keyframe
            else:
                if p.kind == "video":
                    self.dropped += 1
                return
        elif self.backlog_seconds() > MAX_BACKLOG:
            self._congested = True
            self.shared.request_keyframe()  # so it can rejoin soon after catching up
            if p.kind == "video":
                self.dropped += 1
            return
        size = len(p.data or b"") or p.size
        self._backlog_bytes += size
        self._q.put((p, size))

    # Link thread: connect, send, reconnect -----------------------------------------------

    def _default_open(self, url: str):
        import av

        return av.open(url, "w", format="flv")

    def _connect(self):
        out = self._open_container(self._url)
        deadline = time.monotonic() + 10
        while self.shared._templates is None:  # the encoder is still opening
            if self._stop.wait(0.05) or time.monotonic() > deadline:
                raise RuntimeError("the shared encoder did not start")
        video_t, audio_t = self.shared._templates[:2]
        streams = self.shared._ask(lambda: {"video": out.add_stream_from_template(video_t),
                                            "audio": out.add_stream_from_template(audio_t)})
        return out, streams

    def _send(self, out, streams, p: _Packet) -> None:
        import av

        offset = round(self._zero / p.time_base)
        if p.pts is not None and p.pts < offset:
            return  # audio from just before the first keyframe
        packet = av.Packet(p.payload())
        packet.pts = None if p.pts is None else p.pts - offset
        packet.dts = None if p.dts is None else p.dts - offset
        packet.time_base = p.time_base
        packet.is_keyframe = p.keyframe
        packet.stream = streams[p.kind]
        began = time.perf_counter()
        out.mux(packet)
        waited = time.perf_counter() - began
        self.mux_seconds += waited
        self.longest_stall = max(self.longest_stall, waited)
        self.bytes_out += packet.size
        if p.kind == "video":
            self.frames += 1
        if not self.connected:
            self.connected, self._connected_at, self.status = True, time.monotonic(), "live"

    def _session(self) -> None:
        out = None
        try:
            out, streams = self._connect()
            self._started, self._congested = False, False
            self._ready = True
            self.shared.request_keyframe()
            while not self._stop.is_set():
                item = self._q.get()
                if item is None:
                    return
                p, size = item
                try:
                    self._send(out, streams, p)
                finally:
                    self._backlog_bytes -= size
        finally:
            self._ready = False
            self.connected = False
            self._drain()
            if out is not None:
                try:
                    out.close()
                except Exception as exc:
                    log.info("link %s: closing: %s", self.name, self.redact(str(exc)))

    def _drain(self) -> None:
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                self._backlog_bytes -= item[1]

    def _run(self) -> None:
        delay = RETRY_FIRST
        while not self._stop.is_set():
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
                    delay = RETRY_FIRST
                self.reconnects += 1
                hint = "" if self.bytes_out else " (check the server and stream key)"
                self.status = f"disconnected: {message}{hint}; retrying in {delay:.0f} s"
                if self._stop.wait(delay):
                    self.status = "stopped"
                    return
                delay = min(delay * 2, RETRY_MAX)
