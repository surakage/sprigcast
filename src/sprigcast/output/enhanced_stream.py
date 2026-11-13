"""Sends several qualities of the stream over one connection (Twitch Enhanced Broadcasting).

Each quality ("track") has its own encoder thread fed from its canvas, so the GPU encodes them
side by side. Keyframes are forced at the same instants on every track (every keyint_sec
seconds of stream time), and each keyframe carries broadcast performance metrics. All packets go
through the one sender thread inherited from Streamer, which writes them to the FLV/RTMP
connection; FFmpeg's FLV muxer writes track 0 as classic FLV video and the others as Enhanced
RTMP multitrack packets.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from fractions import Fraction

from sprigcast.output.enhanced import EnhancedConfig, TrackMetrics, bpm_sei, wall_clock
from sprigcast.output.recorder import SAMPLE_RATE, VIDEO_QUEUE, encoder_hwaccel
from sprigcast.output.stream import MAX_BACKLOG, RESUME_BACKLOG, Streamer

log = logging.getLogger(__name__)


class _Track:
    def __init__(self, index: int, cfg, canvas):
        self.index = index
        self.cfg = cfg
        self.canvas = canvas  # core.scene.Canvas this quality is cut from
        self.fps = max(1, min(cfg.fps or canvas.fps, canvas.fps))
        self.queue: queue.Queue = queue.Queue(VIDEO_QUEUE)
        self.reset()

    def reset(self) -> None:
        self.last_queued = -1
        self.last_pts = -1
        self.last_key_group = -1
        self.frames = 0  # encoded and sent
        self.frames_in = 0  # handed to this track's encoder
        self.dropped = 0
        self.encode_seconds = 0.0
        self.metrics = TrackMetrics()
        self.pending: dict[int, tuple[float, float, float]] = {}  # pts -> (cts, fer, ferc) for keyframes
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                break


class EnhancedStreamer(Streamer):
    def __init__(self, name: str, config: EnhancedConfig, canvases: list, start_time: float,
                 user_key: str = ""):
        first = config.tracks[0]
        canvas0 = canvases[first.canvas_index]
        super().__init__(
            name, config.server, config.key, canvas0.width, canvas0.height,
            max(1, min(first.fps or canvas0.fps, canvas0.fps)),
            sum(t.bitrate_kbps for t in config.tracks), first.encoder, start_time, (first.width, first.height),
        )
        self.config = config
        self._url = config.url
        self._secrets = [s for s in {config.key.split("?")[0], user_key} if s]
        self.tracks = [_Track(i, cfg, canvases[cfg.canvas_index]) for i, cfg in enumerate(config.tracks)]
        self._video = self.tracks[0].queue  # health checks look at the main quality's queue
        self._lock = threading.Lock()
        self._rendered = 0  # renders of the main canvas offered to this stream
        self._worker_error: Exception | None = None

    # Called from the render thread -----------------------------------------------------

    def canvas_ids(self) -> list[str]:
        return list(dict.fromkeys(t.canvas.id for t in self.tracks))

    def push_for(self, canvas_id: str):
        """A frame-tap callback for one canvas."""
        return lambda frame: self._push(canvas_id, frame)

    def push_frame(self, frame) -> None:  # single-canvas callers (and tests)
        self._push(self.tracks[0].canvas.id, frame)

    def _push(self, canvas_id: str, frame) -> None:
        if self.error is not None or self._stop.is_set():
            return
        if canvas_id == self.tracks[0].canvas.id:
            self._rendered += 1
        congested = False
        if self.connected:
            backlog = self.backlog_seconds()
            if self._congested and backlog < RESUME_BACKLOG:
                self._congested = False
            elif backlog > MAX_BACKLOG:
                self._congested = True
            congested = self._congested
        for track in self.tracks:
            if track.canvas.id != canvas_id:
                continue
            slot = math.floor((frame.time - self.start_time) * track.fps + 0.25)
            if slot <= track.last_queued:
                continue  # this render falls in a frame slot already taken (60 fps canvas, 30 fps track)
            track.last_queued = slot
            if congested:
                if self.connected:
                    track.dropped += 1
                continue
            try:
                track.queue.put_nowait(frame)
            except queue.Full:
                if self.connected:
                    track.dropped += 1
        self.dropped = self.tracks[0].dropped

    # Stats ---------------------------------------------------------------------------------

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "****")
        return text

    def stats(self) -> dict:
        out = super().stats()
        out["size"] = " + ".join(f"{t.cfg.width}×{t.cfg.height}" for t in self.tracks)
        return out

    @property
    def track_summary(self) -> str:
        return self.config.describe()

    # Stream thread -----------------------------------------------------------------------

    def _drain(self) -> None:
        for track in self.tracks:
            track.reset()
        while True:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                break

    def _open(self):
        import av

        container = self._open_container()
        for track in self.tracks:
            cfg = track.cfg
            stream = container.add_stream(cfg.encoder, rate=track.fps, hwaccel=encoder_hwaccel(cfg.encoder))
            stream.width, stream.height = cfg.width, cfg.height
            stream.pix_fmt = _sw_format(cfg.encoder)
            stream.bit_rate = cfg.bitrate_kbps * 1000
            ctx = stream.codec_context
            ctx.time_base = Fraction(1, track.fps)
            ctx.gop_size = cfg.keyint_sec * track.fps
            ctx.max_b_frames = cfg.max_b_frames
            ctx.colorspace, ctx.color_primaries, ctx.color_trc, ctx.color_range = 1, 1, 1, 1
            stream.options = dict(cfg.options)
            track.stream = stream
            track.reformatter = av.video.reformatter.VideoReformatter()
        audio = container.add_stream("aac", rate=SAMPLE_RATE)
        audio.layout = "stereo"
        audio.bit_rate = self.config.audio_kbps * 1000
        self._send_q = queue.Queue()
        self._send_error = None
        self._backlog_bytes = 0
        self._sender = threading.Thread(
            target=self._send_loop, args=(container,), name=f"sprigcast-send-{self.name}", daemon=True
        )
        self._sender.start()
        return container, audio

    def _mux(self, container, packets) -> None:
        if self._send_error is not None:
            raise self._send_error
        for packet in packets:
            with self._lock:
                self._backlog_bytes += packet.size
            self._send_q.put(packet)

    def _send_loop(self, container) -> None:
        while True:
            packet = self._send_q.get()
            if packet is None:
                return
            size = packet.size
            if self._send_error is None:
                try:
                    began = time.perf_counter()
                    container.mux(packet)
                    waited = time.perf_counter() - began
                    self.mux_seconds += waited
                    self.longest_stall = max(self.longest_stall, waited)
                    self.bytes_out += size
                except Exception as exc:
                    self._send_error = exc
            with self._lock:
                self._backlog_bytes -= size
            if not self.connected and self.bytes_out and self._send_error is None:
                self.connected = True
                self._connected_at = time.monotonic()
                self.status = "live"

    def _session(self) -> None:
        container, finished = None, False
        try:
            container, audio = self._open()
            self._worker_error = None
            workers = [
                threading.Thread(target=self._track_loop, args=(container, t), name=f"sprigcast-track{t.index}",
                                 daemon=True)
                for t in self.tracks
            ]
            for worker in workers:
                worker.start()
            while not self._stop.is_set():
                self._write_audio(container, audio)
                if self._worker_error is not None:
                    raise self._worker_error
                if self._send_error is not None:
                    raise self._send_error
                time.sleep(0.01)
            for worker in workers:
                worker.join(timeout=10)
            if self._worker_error is not None:
                raise self._worker_error
            self._write_audio(container, audio)
            self._mux(container, audio.encode(None))
            finished = True
        finally:
            self._stop_workers = True
            if container is not None and self._before_close(container):
                try:
                    container.close()
                except Exception as exc:
                    if finished:
                        raise
                    log.warning("closing stream %s failed: %s", self.name, self.redact(str(exc)))

    def _track_loop(self, container, track: _Track) -> None:
        try:
            while True:
                if self._worker_error is not None or self._send_error is not None:
                    return
                try:
                    frame = track.queue.get(timeout=0.05)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    continue
                self._encode(container, track, frame)
            self._send(container, track, track.stream.encode(None))
        except Exception as exc:
            log.exception("track %s of stream %s failed", track.index, self.name)
            self._worker_error = exc

    def _encode(self, container, track: _Track, frame) -> None:
        import av
        from av.video.frame import PictureType
        from av.video.reformatter import ColorRange, Colorspace

        pts = math.floor((frame.time - self.start_time) * track.fps + 0.25)
        if pts <= track.last_pts:
            return
        track.last_pts = pts
        began = time.perf_counter()
        canvas = track.canvas
        vf = av.VideoFrame(canvas.width, canvas.height, "rgba")
        if vf.planes[0].line_size == canvas.width * 4:
            vf.planes[0].update(frame.data)
        else:
            import numpy as np

            rgba = np.frombuffer(frame.data, dtype=np.uint8).reshape(canvas.height, canvas.width, 4)
            vf = av.VideoFrame.from_ndarray(rgba, format="rgba")
        vf = track.reformatter.reformat(
            vf, width=track.cfg.width, height=track.cfg.height, interpolation="BICUBIC",
            format=_sw_format(track.cfg.encoder), dst_colorspace=Colorspace.ITU709, dst_color_range=ColorRange.MPEG,
        )
        vf.pts = pts
        group = pts // (track.cfg.keyint_sec * track.fps)
        if group > track.last_key_group:  # first frame of each keyint window: a keyframe on every track
            track.last_key_group = group
            vf.pict_type = PictureType.I
            track.pending[pts] = (wall_clock(frame.time), wall_clock(began), 0.0)
        track.frames_in += 1
        fer = time.perf_counter()
        packets = track.stream.encode(vf)
        ferc = time.perf_counter()
        for key_pts, (cts, fer_wall, done) in list(track.pending.items()):
            if not done:
                track.pending[key_pts] = (cts, fer_wall, wall_clock(ferc))
        track.encode_seconds += ferc - began
        self._send(container, track, packets)
        if track.index == 0:
            self.encode_seconds = track.encode_seconds

    def _send(self, container, track: _Track, packets) -> None:
        import av

        out = []
        for packet in packets:
            track.frames += 1
            if packet.is_keyframe:
                cts, fer, ferc = track.pending.pop(packet.pts, (time.time(), 0.0, 0.0))
                for stale in [p for p in track.pending if p < (packet.pts or 0)]:
                    track.pending.pop(stale, None)
                sei = bpm_sei(
                    track.metrics, cts=cts, fer=fer, ferc=ferc, pir=time.time(),
                    session=(self._rendered, 0, sum(t.dropped for t in self.tracks), sum(t.frames for t in self.tracks)),
                    rendition=(track.frames_in, track.dropped, track.frames),
                )
                tagged = av.Packet(bytes(packet) + sei)
                tagged.pts, tagged.dts, tagged.time_base = packet.pts, packet.dts, packet.time_base
                tagged.stream = packet.stream
                tagged.is_keyframe = True
                packet = tagged
            out.append(packet)
        if track.index == 0:
            self.frames = track.frames
        self._mux(container, out)


def _sw_format(encoder: str) -> str:
    """The pixel format frames are converted to for this encoder. (Once open, an NVENC stream with a
    shared GPU context reports "cuda" as its format, so this can't be read back from the stream.)"""
    return "yuv420p" if encoder == "libx264" else "nv12"  # every graphics-card encoder takes NV12
