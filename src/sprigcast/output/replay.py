"""Replay buffer: keep the last N seconds of each canvas encoded in memory, and save it as a clip on demand.

It encodes exactly like a recording (same encoder settings, NVENC when available), but instead of
writing packets to a file it keeps their bytes in a rolling buffer. Saving copies the buffer, starting
at a keyframe, into a new file on the encoder's own thread, so the live encode never waits for the disk.
"""

from __future__ import annotations

import io
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from sprigcast.output.recorder import CONTAINERS, Recorder, pick_codec

log = logging.getLogger(__name__)


@dataclass
class _Packet:
    kind: str  # "video" or "audio"
    data: bytes
    pts: int | None
    dts: int | None
    time_base: Fraction
    keyframe: bool

    @property
    def seconds(self) -> float:
        return float((self.pts if self.pts is not None else self.dts or 0) * self.time_base)


def replay_path(folder: Path, canvas_id: str, when: datetime, container: str) -> Path:
    """`SprigCast Replay 2026-11-12 19-30-05 horizontal.mkv`, with a counter if that name is taken."""
    base = f"SprigCast Replay {when:%Y-%m-%d %H-%M-%S} {canvas_id}"
    path = folder / f"{base}.{container}"
    n = 2
    while path.exists():
        path = folder / f"{base} ({n}).{container}"
        n += 1
    return path


class ReplayBuffer(Recorder):
    """A Recorder whose packets go to a rolling in-memory buffer of `seconds` instead of a file."""

    def __init__(self, canvas_id: str, width: int, height: int, fps: int, bitrate_kbps: int, codec: str,
                 start_time: float, seconds: float, container: str = "mkv", encoder=None):
        super().__init__(Path(f"replay-{canvas_id}.{container}"), width, height, fps, bitrate_kbps, codec,
                         start_time, container, encoder=encoder)
        self.canvas_id = canvas_id
        self.seconds = seconds
        self._packets: deque[_Packet] = deque()
        self._templates = None  # (video stream, audio stream) of the in-memory container
        self._requests: queue.Queue = queue.Queue()

    # Called from other threads ------------------------------------------------------

    def save(self, path: Path, timeout: float = 20.0) -> Path:
        """Write the buffered seconds to `path`. Blocks until written; raises if it couldn't be."""
        self._ask(lambda: self._write_clip(path), timeout)
        return path

    def _ask(self, job, timeout: float = 20.0):
        """Run job() on the encoder thread, between frames, and wait for it. Raises what it raised."""
        done: dict = {}
        finished = threading.Event()
        if not self.running:
            raise RuntimeError(self.error or "the encoder has stopped")
        self._requests.put((job, done, finished))
        if not finished.wait(timeout):
            raise TimeoutError("the encoder did not answer in time")
        if "error" in done:
            raise RuntimeError(done["error"])
        return done.get("result")

    @property
    def buffered_seconds(self) -> float:
        packets = [p for p in list(self._packets) if p.kind == "video"]
        return packets[-1].seconds - packets[0].seconds if len(packets) > 1 else 0.0

    # Encoder thread -------------------------------------------------------------------

    def _open_container(self):
        import av

        # Encoders need a container for their settings (global headers for MKV/MP4); nothing is written to it.
        return av.open(io.BytesIO(), "w", format=CONTAINERS[self.container_kind])

    def _open(self):
        container, video, audio = super()._open()
        # Opening the in-memory file opens the encoders and fills in each stream's codec parameters
        # (size, SPS/PPS headers), which saved clips copy from these template streams.
        container.start_encoding()
        self._templates = (video, audio)
        return container, video, audio

    def _mux(self, container, packets) -> None:
        video, audio = self._templates
        for packet in packets:
            kind = "video" if packet.stream is video else "audio"
            p = _Packet(kind, bytes(packet), packet.pts, packet.dts,
                        packet.time_base or packet.stream.time_base, bool(packet.is_keyframe))
            self._packets.append(p)
            self.bytes_out += packet.size
            self._on_packet(p)
        self._trim()

    def _on_packet(self, p: _Packet) -> None:
        """Hook: each packet as it comes out of the encoder (output/shared.py also writes it to a file)."""

    def _trim(self) -> None:
        """Keep the last `seconds`, starting at a video keyframe so the clip plays from its first frame.

        The clip starts at the latest keyframe at or before the cutoff, so it can run up to one
        keyframe interval longer than asked, never shorter (unless less has been recorded).
        """
        keys = [i for i, p in enumerate(self._packets) if p.kind == "video" and p.keyframe]
        if not keys:
            return
        newest = max(p.seconds for p in self._packets if p.kind == "video")
        cutoff = newest - self.seconds
        start = keys[0]
        for i in keys:
            if self._packets[i].seconds <= cutoff:
                start = i
        for _ in range(start):
            self._packets.popleft()

    def _write_audio(self, container, stream) -> None:
        super()._write_audio(container, stream)
        while True:  # saving happens here, on the encoder thread, between frames
            try:
                job, done, finished = self._requests.get_nowait()
            except queue.Empty:
                return
            try:
                done["result"] = job()
            except Exception as exc:
                log.exception("%s: request failed", self.canvas_id)
                done["error"] = str(exc) or exc.__class__.__name__
            finished.set()

    def _write_clip(self, path: Path) -> None:
        packets = list(self._packets)
        start = next((p for p in packets if p.kind == "video" and p.keyframe), None)
        if start is None or self._templates is None:
            raise RuntimeError("nothing buffered yet")
        packets = packets[packets.index(start):]
        out = PacketFile(path, self.container_kind, self._templates, start.seconds)
        try:
            for p in packets:
                out.write(p)
        finally:
            out.close()


class PacketFile:
    """A video file written from already-encoded packets, with time 0 at `zero` seconds."""

    def __init__(self, path: Path, container: str, templates, zero: float):
        import av

        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.zero = path, zero
        self._out = av.open(str(path), "w", format=CONTAINERS[container])
        video_t, audio_t = templates
        self._streams = {"video": self._out.add_stream_from_template(video_t),
                         "audio": self._out.add_stream_from_template(audio_t)}

    def write(self, p: _Packet) -> None:
        import av

        offset = round(self.zero / p.time_base)
        if p.pts is not None and p.pts < offset:
            return  # audio from just before the first keyframe
        packet = av.Packet(p.data)
        packet.pts = None if p.pts is None else p.pts - offset
        packet.dts = None if p.dts is None else p.dts - offset
        packet.time_base = p.time_base
        packet.is_keyframe = p.keyframe
        packet.stream = self._streams[p.kind]
        self._out.mux(packet)

    def close(self) -> None:
        self._out.close()


class ReplaySession:
    """One replay buffer per chosen canvas, fed by the engine like a recording."""

    def __init__(self, engine, buffers: list[ReplayBuffer], folder: Path, container: str):
        self.engine, self.buffers, self.folder, self.container = engine, buffers, folder, container

    @classmethod
    def start(cls, engine, canvases: list, folder: Path, seconds: float, bitrate_kbps: int, container: str = "mkv",
              codec: str | None = None, encoder=None) -> ReplaySession:
        codec = codec or pick_codec()
        start_time = time.perf_counter()
        buffers = []
        for canvas in canvases:
            buf = ReplayBuffer(canvas.id, canvas.width, canvas.height, canvas.fps, bitrate_kbps, codec, start_time,
                               seconds, container, encoder=encoder)
            buf.start()
            buffers.append(buf)
        for buf in buffers:
            engine.add_frame_tap(buf.canvas_id, buf.push_frame, buf.gpu_size)
            engine.mixer.add_tap(buf.push_audio)
        return cls(engine, buffers, folder, container)

    def save(self) -> list[Path]:
        """Save every canvas's last seconds, each to its own file, named with the same time."""
        when = datetime.now()
        saved = []
        for buf in self.buffers:
            saved.append(buf.save(replay_path(self.folder, buf.canvas_id, when, self.container)))
        return saved

    @property
    def errors(self) -> list[str]:
        return [f"{b.canvas_id}: {b.error}" for b in self.buffers if b.error]

    def stop(self) -> None:
        for buf in self.buffers:
            self.engine.remove_frame_tap(buf.canvas_id, buf.push_frame)
            self.engine.mixer.remove_tap(buf.push_audio)
        for buf in self.buffers:
            buf.stop()
