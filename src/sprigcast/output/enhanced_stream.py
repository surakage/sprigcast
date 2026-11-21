"""Sends several qualities of the stream over one connection (Twitch Enhanced Broadcasting).

Each quality ("track") has its own encoder thread fed from its canvas, so the GPU encodes them
side by side. Keyframes are forced at the same instants on every track (every keyint_sec
seconds of stream time), and each keyframe carries broadcast performance metrics. All packets go
through the one sender thread inherited from Streamer, which writes them to the FLV/RTMP
connection; FFmpeg's FLV muxer writes track 0 as classic FLV video and the others as Enhanced
RTMP multitrack packets.

With the native engine encoding the qualities, the engine packs and sends them too (sc_esend, _engine_session):
Python still chooses and tags each packet and encodes the sound, but nothing is packed or sent under its lock.
SPRIGCAST_NO_NATIVE_ESEND=1 keeps the sender thread here.
"""

from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
from fractions import Fraction

from sprigcast.core import privacy
from sprigcast.output.enhanced import EnhancedConfig, TrackMetrics, bpm_sei, wall_clock, with_sei
from sprigcast.output.recorder import SAMPLE_RATE, VIDEO_QUEUE, encoder_hwaccel
from sprigcast.output.shared_stream import SharedStreamEncoder
from sprigcast.output.stream import (
    MAX_BACKLOG,
    RESUME_BACKLOG,
    Streamer,
    engine_encodes_streams,
    log_live,
)

log = logging.getLogger(__name__)


class _Out:
    """A packet on its way to the engine's sender: which stream of the connection, and its bytes and times."""

    __slots__ = ("data", "dts", "key", "pts", "size", "stream", "time_base")

    def __init__(self, stream: int, data: bytes, pts: int, dts: int, time_base, key: bool):
        self.stream, self.data, self.pts, self.dts, self.time_base, self.key = stream, data, pts, dts, time_base, key
        self.size = len(data)


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


class _TrackEncoder(SharedStreamEncoder):
    """One quality of an Enhanced Broadcasting stream, encoded by the native engine's writer on its own thread
    (packets mode): the engine hands it frames at the track's size itself, and each packet goes to the stream
    (EnhancedStreamer._from_track). Keyframes come every keyint seconds of stream time on every track alike
    (the writer keys the first picture at or past each multiple).

    Other destinations wanting the same picture (YouTube, Kick... at this quality's canvas, size and frame rate)
    send these same packets as links (StreamSession.start): no extra encoder for them."""

    def _python_wants_packets(self) -> bool:
        return True  # each packet goes to the Enhanced Broadcasting connection from here (_on_packet)

    def __init__(self, owner: EnhancedStreamer, track: _Track, start_time: float):
        from sprigcast.core.scene import EncoderSettings

        cfg = track.cfg
        settings = EncoderSettings(keyint_sec=max(1, cfg.keyint_sec), b_frames=max(0, cfg.max_b_frames))
        super().__init__(track.canvas, track.fps, cfg.bitrate_kbps, cfg.encoder, start_time, (cfg.width, cfg.height),
                         settings)
        self.owner, self.track = owner, track

    def request_keyframe(self) -> None:
        pass  # a link joining waits for the next keyframe: Twitch needs every quality to key at the same instants

    def _video_options(self) -> dict[str, str]:
        return dict(self.track.cfg.options)

    def _session(self) -> None:
        if not self.uses_writer():
            raise RuntimeError("the engine's writer isn't available")
        if not self._writer_session():
            raise RuntimeError("the engine's writer couldn't start this quality")

    def _on_packet(self, p) -> None:
        try:
            self.owner._from_track(self.track, p)
        except Exception as exc:  # noqa: BLE001
            # Twitch's connection failing is the stream's to handle (it reconnects); raised here, it stopped this
            # encoder for good, and Kick, Facebook and YouTube sending this quality starved (PC, 2026-11-21).
            self.owner._send_failed(exc)
        if self.links:
            super()._on_packet(p)


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
        # Twitch's sign-in for the stream (config.key, "v2_..." plus our query) holds a "?" of its own: each form of
        # it is hidden, not only what comes before the first "?" (the key reached the log that way, 2026-11-20).
        self._secrets = [s for s in {config.key, config.url, user_key} if s]
        privacy.remember_secret(*self._secrets)
        self.tracks = [_Track(i, cfg, canvases[cfg.canvas_index]) for i, cfg in enumerate(config.tracks)]
        self._video = self.tracks[0].queue  # health checks look at the main quality's queue
        self._lock = threading.Lock()
        self._rendered = 0  # renders of the main canvas offered to this stream
        self._worker_error: Exception | None = None
        # With the native engine each quality is encoded by the engine's writer from frames the engine makes at
        # its size (no picture passes through Python); Python only tags keyframes and sends the packets.
        self.native = engine_encodes_streams()
        self._live = None  # (container, session start in perf_counter seconds) while connected, native only
        self._esend = None  # the engine's sender while it sends this connection (sprigcore.EnhancedSender)
        if self.native:
            for track in self.tracks:
                track.encoder = _TrackEncoder(self, track, start_time)
                track.skipped = 0
            self._video = self.tracks[0].encoder._video  # health checks look at the main quality's queue

    # Called from the render thread -----------------------------------------------------

    def canvas_ids(self) -> list[str]:
        return list(dict.fromkeys(t.canvas.id for t in self.tracks))

    def push_for(self, canvas_id: str):
        """A frame-tap callback for one canvas."""
        return lambda frame: self._push(canvas_id, frame)

    def frame_taps(self) -> list[tuple]:
        """(canvas id, callback, NV12 size or None) for each frame tap this stream needs (StreamSession)."""
        if self.native:
            return [(t.canvas.id, t.encoder.push_frame, t.encoder.gpu_size) for t in self.tracks]
        return [(canvas_id, self.push_for(canvas_id)) for canvas_id in self.canvas_ids()]

    def start(self) -> None:
        if self.native:
            for track in self.tracks:
                track.encoder.start()
        super().start()

    def _run(self) -> None:
        try:
            super()._run()
        finally:
            if self.native:
                self._stop_encoders()  # also when it ended between connections

    def hand_over(self, encoders) -> None:
        """This stream is stopping while other places send some of its qualities: those encoders carry on for
        them (StreamSession.drop), and this stream no longer stops them."""
        self._handed = [*getattr(self, "_handed", []), *encoders]

    def _stop_encoders(self, timeout: float = 15.0) -> None:
        handed = getattr(self, "_handed", [])
        for track in self.tracks:
            if track.encoder.running and not any(track.encoder is h for h in handed):
                track.encoder.stop(timeout)

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

    def _open_container(self):
        """Twitch wants the main quality in Enhanced RTMP's own form too (output/eflv.py)."""
        from sprigcast.output.eflv import EnhancedIngest
        from sprigcast.output.stream import CONNECT_TIMEOUT, SEND_TIMEOUT

        return EnhancedIngest(self._url, timeout=(CONNECT_TIMEOUT, SEND_TIMEOUT))

    def redact(self, text: str) -> str:
        return privacy.mask(text, self._secrets)

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
        self._delay_line = self._new_delay_line(self._send_q)
        self._sender = threading.Thread(
            target=self._send_loop, args=(container,), name=f"sprigcast-send-{self.name}", daemon=True
        )
        self._sender.start()
        return container, audio

    def _mux(self, container, packets) -> None:
        if self._send_error is not None:
            raise self._send_error
        line = self._delay_line
        if self._esend is not None:
            for packet in packets:
                if not isinstance(packet, _Out):  # the sound, from PyAV's encoder
                    stamp = packet.pts if packet.pts is not None else packet.dts
                    if stamp is None:
                        continue
                    packet = _Out(len(self.tracks), bytes(packet), stamp,
                                  packet.dts if packet.dts is not None else stamp, packet.time_base, True)
                if line is not None:
                    line.put(packet)
                else:
                    self._to_engine(packet)
            return
        for packet in packets:
            if line is not None:
                line.put(packet)
                continue
            with self._lock:
                self._backlog_bytes += packet.size
            self._send_q.put(packet)

    def _to_engine(self, packet: _Out) -> None:
        sender = self._esend
        if sender is not None:  # once it failed it takes nothing: the session sees that and reconnects
            sender.packet(packet.stream, packet.data, packet.pts, packet.dts, packet.time_base, packet.key)

    def _send_loop(self, container) -> None:
        from sprigcast.core.pacing import ABOVE_NORMAL, raise_thread_priority

        raise_thread_priority(ABOVE_NORMAL)  # a starved sender looks like a slow network
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
                log_live(self)

    def _session(self) -> None:
        if self.native:
            return self._native_session()
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

    # Native engine: the tracks' writers encode, packets come here ----------------------------------------------------

    def _native_session(self) -> None:
        container, finished = None, False
        self._sender = None
        try:
            deadline = time.monotonic() + 15
            for track in self.tracks:
                enc = track.encoder
                while enc._templates is None:  # its writer is still opening
                    if enc.error is not None:
                        raise RuntimeError(f"quality {track.index + 1}: {enc.error}")
                    if self._stop.is_set():
                        return
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"quality {track.index + 1}'s encoder did not start")
                    time.sleep(0.02)
            writers = self._engine_writers()
            if writers is not None:
                return self._engine_session(writers)
            container = self._open_container()
            for track in self.tracks:
                track.stream = container.add_stream_from_template(track.encoder._templates[0])
                track.started = False
            audio = container.add_stream("aac", rate=SAMPLE_RATE)
            audio.layout = "stereo"
            audio.bit_rate = self.config.audio_kbps * 1000
            self._send_q = queue.Queue()
            self._send_error = None
            self._backlog_bytes = 0
            self._delay_line = self._new_delay_line(self._send_q)
            self._sender = threading.Thread(
                target=self._send_loop, args=(container,), name=f"sprigcast-send-{self.name}", daemon=True
            )
            self._sender.start()
            self._live = (container, self.start_time)  # the tracks' packets go out from their next keyframe
            while not self._stop.is_set():
                self._write_audio(container, audio)
                for track in self.tracks:
                    if track.encoder.error is not None:
                        raise RuntimeError(f"quality {track.index + 1}: {track.encoder.error}")
                if self._send_error is not None:
                    raise self._send_error
                time.sleep(0.01)
            self._stop_encoders()  # their last packets go out before the stream's end
            self._live = None
            self._write_audio(container, audio)
            self._mux(container, audio.encode(None))
            finished = True
        finally:
            self._live = None
            if container is not None and self._sender is not None and self._before_close(container):
                try:
                    container.close()
                except Exception as exc:
                    if finished:
                        raise
                    log.warning("closing stream %s failed: %s", self.name, self.redact(str(exc)))

    def _engine_writers(self):
        """Each quality's writer when the engine can send this connection itself (interface 43), else None: the
        sender thread here does, as before (a test's own container, or SPRIGCAST_NO_NATIVE_ESEND=1)."""
        from sprigcast import sprigcore

        if (type(self)._open_container is not EnhancedStreamer._open_container or "_open_container" in vars(self)
                or os.environ.get("SPRIGCAST_NO_NATIVE_ESEND") or not hasattr(sprigcore, "EnhancedSender")):
            return None
        writers = [getattr(track.encoder, "writer", None) for track in self.tracks]
        if any(w is None or not getattr(w, "packets", False) for w in writers):
            return None
        lib = sprigcore._media_lib()
        return writers if lib is not None and hasattr(lib, "sc_esend_open") else None

    def _engine_session(self, writers) -> None:
        """One connection packed and sent by the engine (native/core/src/writer.cpp ESend): the qualities' packets
        are still chosen and tagged here (_from_track) and the sound is encoded here, but each is only handed
        over; FFmpeg's FLV muxer, the Enhanced RTMP form and the network are on a thread of the engine's own.
        Packing and sending them on a Python thread above normal priority held Python's lock some 230 times a
        second, beside a window that stopped answering for 371 ms (PC, 2026-11-21)."""
        import av
        import av.logging as avlog
        from av.codec.context import Flags

        from sprigcast import sprigcore
        from sprigcast.output.delay import DelayLine
        from sprigcast.output.stream import CONNECT_TIMEOUT, SEND_TIMEOUT, ingest_name

        aac = av.CodecContext.create("aac", "w")
        aac.sample_rate, aac.layout, aac.format = SAMPLE_RATE, "stereo", "fltp"
        aac.bit_rate = self.config.audio_kbps * 1000
        aac.time_base = Fraction(1, SAMPLE_RATE)
        aac.flags |= Flags.global_header  # its header goes in the stream's start, as FLV wants
        aac.open()
        base = (self.bytes_out, self.mux_seconds)
        sender, line = None, None
        old = avlog.get_level()
        louder = old is None or old > avlog.WARNING
        if louder:
            avlog.set_level(avlog.WARNING)
        try:
            # Why a connect fails is in what FFmpeg logs, said on the engine's thread (StreamLink._session_engine).
            with avlog.Capture(False) as logs:
                sender = sprigcore.EnhancedSender(self._url, writers, bytes(aac.extradata or b""),
                                                  self.config.audio_kbps, SAMPLE_RATE, 2, CONNECT_TIMEOUT,
                                                  SEND_TIMEOUT)
                while (stats := sender.stats()).state_name == "connecting":
                    if self._stop.wait(0.02):
                        return
                said = [str(entry[2]).strip() for entry in logs if str(entry[2]).strip()]
            if stats.state_name == "failed":
                detail = " | FFmpeg: " + " / ".join(said[-4:]) if said else ""
                why = (stats.error_text or "no answer").removeprefix("couldn't connect: ")
                raise ConnectionError(f"couldn't connect to {ingest_name(self._url)}: {why}{detail}")
            self._send_error = None
            self._backlog_bytes = 0
            for track in self.tracks:
                track.started = False
            line = DelayLine(self.delay_s, self._to_engine, f"delay-{self.name}") if self.delay_s > 0 else None
            self._delay_line = line
            self._esend = sender
            self._live = (None, self.start_time)  # the tracks' packets go out from their next keyframe

            def numbers():
                stats = sender.stats()
                self.bytes_out, self.mux_seconds = base[0] + stats.bytes, base[1] + stats.send_seconds
                self.longest_stall = max(self.longest_stall, stats.longest_send)
                self._backlog_bytes = stats.backlog_bytes
                if stats.state_name == "live" and not self.connected:
                    self.connected, self._connected_at, self.status = True, time.monotonic(), "live"
                    log_live(self)
                return stats

            while not self._stop.is_set():
                self._write_audio(None, aac)
                for track in self.tracks:
                    if track.encoder.error is not None:
                        raise RuntimeError(f"quality {track.index + 1}: {track.encoder.error}")
                if self._send_error is not None:
                    raise self._send_error
                if numbers().state_name == "failed":
                    raise RuntimeError(sender.stats().error_text or "the connection dropped")
                time.sleep(0.01)
            self._stop_encoders()  # their last packets go out before the stream's end
            self._live = None
            self._write_audio(None, aac)
            self._mux(None, aac.encode(None))
            self._delay_line = None
            if line is not None:
                line.finish(self.delay_s + 5)  # the delayed part goes out on time, then the stream ends
            sender.finish()
            deadline = time.monotonic() + 3.0
            while (stats := numbers()).state_name not in ("finished", "failed") and time.monotonic() < deadline:
                time.sleep(0.01)
            if stats.state_name == "failed":
                raise RuntimeError(stats.error_text or "the connection dropped")
            if stats.state_name != "finished":
                log.warning("stream %s: the engine's sender is still busy with the network; leaving the connection",
                            self.name)
        finally:
            if louder:
                avlog.set_level(old)
            self._live = None
            self._esend = None
            self._delay_line = None
            if line is not None:
                line.close()  # what's delayed belongs to this connection and goes with it
            self._backlog_bytes = 0
            if sender is not None:
                sender.close()

    def _send_failed(self, exc: Exception) -> None:
        """Sending a quality's packet failed: the stream's session sees it, drops the connection and reconnects."""
        if self._send_error is None:
            self._send_error = exc

    def _from_track(self, track: _Track, p) -> None:
        """A packet from a track's writer (its encoder thread): from the track's first keyframe since this
        connection began, on the connection's clock, keyframes tagged with the broadcast metrics."""
        import av

        live = self._live
        if live is None or p.kind != "video" or self._send_error is not None:
            return  # not connected, or the connection already failed: the session reconnects
        container, began = live
        enc = track.encoder
        offset = round((began - enc.start_time) / p.time_base)  # the connection's 0 in this track's frame numbers
        if p.pts is not None and p.pts < offset:
            return  # encoded before this connection
        if self._congested_for(track, p):
            return
        if not track.started:
            if not p.keyframe:
                return
            track.started = True
        data = p.payload()
        track.frames += 1
        if p.keyframe:
            now = time.time()
            data = with_sei(data, bpm_sei(
                track.metrics, cts=now, fer=now, ferc=now, pir=now,
                session=(self._rendered, 0, sum(t.dropped for t in self.tracks), sum(t.frames for t in self.tracks)),
                rendition=(enc.frames, track.dropped, track.frames),
            ))
        if self._esend is not None:
            stamp = p.pts if p.pts is not None else p.dts
            if stamp is None:
                return
            packet = _Out(track.index, data, stamp - offset, (p.dts if p.dts is not None else stamp) - offset,
                          p.time_base, bool(p.keyframe))
        else:
            packet = av.Packet(data)
            packet.pts = None if p.pts is None else p.pts - offset
            packet.dts = None if p.dts is None else p.dts - offset
            packet.time_base = p.time_base
            packet.is_keyframe = p.keyframe
            packet.stream = track.stream
        if track.index == 0:
            self.frames, self.dropped, self._rendered = track.frames, track.dropped, enc.frames + enc.dropped
            self.encode_seconds = enc.encode_seconds
        self._mux(container, [packet])

    def _congested_for(self, track: _Track, p) -> bool:
        """The network can't keep up: this track's packets are let go until it can, then it goes on from a
        keyframe (all tracks key at the same instants, so they come back together)."""
        track.dropped = track.encoder.dropped + track.skipped
        if not self.connected:
            return False
        backlog = self.backlog_seconds()
        if getattr(track, "skipping", False):
            if backlog < RESUME_BACKLOG and p.keyframe:
                track.skipping = False
                return False
            track.skipped += 1
            return True
        if backlog > MAX_BACKLOG:
            track.skipping = True
            track.skipped += 1
            return True
        return False

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
            threads=2,  # FFmpeg's default is a pool as big as the CPU, per track
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
                tagged = av.Packet(with_sei(bytes(packet), sei))
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
