"""Replay buffer: keep the last N seconds of each canvas encoded, and save it as a clip on demand.

It encodes exactly like a recording (same encoder settings, NVENC when available), but instead of
writing packets to a file it keeps them in a rolling buffer. Saving copies the buffer, starting at a
keyframe, into a new file on the encoder's own thread, so the live encode never waits for the disk.

The packets' bytes go to a temporary ring file rather than RAM (30 s of two 12 Mbps canvases would
be ~100 MB of memory). Windows keeps recently written file data in its file cache, which isn't
counted against SprigCast and is given back whenever games or other apps need the memory.
"""

from __future__ import annotations

import io
import logging
import queue
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from functools import cached_property
from pathlib import Path

from sprigcast.output.recorder import CONTAINERS, Recorder, pick_codec

log = logging.getLogger(__name__)


@dataclass
class _Packet:
    kind: str  # "video", "audio" (the first track) or "audio1", "audio2"... (the encoder's other tracks)
    data: bytes | None  # None once the bytes are in `spool`
    pts: int | None
    dts: int | None
    time_base: Fraction
    keyframe: bool
    spool: Spool | None = None
    at: int = 0  # position in the spool (always growing; see Spool)
    size: int = 0

    @cached_property
    def seconds(self) -> float:  # cached: Fraction maths is slow and the buffer asks often
        return float((self.pts if self.pts is not None else self.dts or 0) * self.time_base)

    def payload(self) -> bytes:
        return self.data if self.data is not None else self.spool.read(self.at, self.size)


class Spool:
    """A fixed-size ring of packet bytes in a temporary file (deleted when closed).

    Positions only ever grow; the byte in the file is at position % capacity. A packet never wraps
    around the end, so it may skip ahead to the next lap. Packets more than one lap behind the write
    position have been overwritten (`oldest_kept`). Used from the encoder thread only.
    """

    def __init__(self, capacity: int, folder: str | None = None):
        self.capacity = capacity
        self.file = tempfile.TemporaryFile(prefix="sprigcast-replay-", dir=folder)
        self.end = 0

    def place(self, size: int) -> int:
        """Where a packet of `size` bytes will go (call write() next)."""
        if size > self.capacity:
            raise ValueError("packet larger than the replay spool")
        at = self.end
        if at % self.capacity + size > self.capacity:
            at += self.capacity - at % self.capacity  # start the next lap rather than wrap mid-packet
        return at

    def write(self, at: int, data: bytes) -> None:
        self.file.seek(at % self.capacity)
        self.file.write(data)
        self.end = at + len(data)

    def oldest_kept(self) -> int:
        return self.end - self.capacity

    def read(self, at: int, size: int) -> bytes:
        if at < self.oldest_kept():
            raise RuntimeError("replay data was overwritten")
        self.file.flush()
        self.file.seek(at % self.capacity)
        return self.file.read(size)

    def close(self) -> None:
        self.file.close()


def spool_capacity(bitrate_kbps: int, seconds: float) -> int:
    """Room for `seconds` plus a keyframe interval, twice over for VBR peaks and audio; at least 16 MB."""
    return max(16 << 20, int(bitrate_kbps * 1000 / 8 * (seconds + 10) * 2))


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
                 start_time: float, seconds: float, container: str = "mkv", encoder=None,
                 out_size: tuple[int, int] | None = None, tracks: tuple[str, ...] = ("clips",)):
        super().__init__(Path(f"replay-{canvas_id}.{container}"), width, height, fps, bitrate_kbps, codec,
                         start_time, container, out_size, encoder=encoder, tracks=tracks)
        self.canvas_id = canvas_id
        self.seconds = seconds
        self._packets: deque[_Packet] = deque()
        self._keys: deque[_Packet] = deque()  # the video keyframes among _packets, oldest first
        self._newest = float("-inf")  # time of the newest video packet
        self._spool: Spool | None = None
        self._spool_failed = False
        self._templates = None  # (video stream, audio stream per track) of the in-memory container
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
        self._templates = (video, audio, *self._extra_audio)
        return container, video, audio

    def audio_kinds(self, *tracks: str) -> tuple[str, ...]:
        """The packet kinds of these named tracks (those this encoder has), for PacketFile."""
        return tuple(audio_kind(self.tracks.index(t)) for t in tracks if t in self.tracks)

    def _mux(self, container, packets) -> None:
        kinds = {id(stream): "video" if i == 0 else audio_kind(i - 1) for i, stream in enumerate(self._templates)}
        for packet in packets:
            kind = kinds.get(id(packet.stream), "audio")
            p = _Packet(kind, bytes(packet), packet.pts, packet.dts,
                        packet.time_base or packet.stream.time_base, bool(packet.is_keyframe))
            self._keep(p)
            self.bytes_out += packet.size
            began = time.perf_counter()
            self._on_packet(p)  # a recording file gets the bytes straight away
            if self.seconds > 0 and not self._spool_failed:
                self._to_spool(p)
            waited = time.perf_counter() - began
            self.mux_seconds += waited
            self.longest_stall = max(self.longest_stall, waited)
        self._trim()
        if self.seconds <= 0 and self._spool is not None and not (self._packets and self._packets[0].spool):
            self._spool.close()  # replay turned off and spooled packets (always the oldest) are gone
            self._spool = None

    def _to_spool(self, p: _Packet) -> None:
        """Move a packet's bytes out of memory into the ring file."""
        wanted = spool_capacity(self.bitrate_kbps, self.seconds)
        try:
            if self._spool is None or self._spool.capacity < wanted:
                self._spool = Spool(wanted)  # longer replay asked for: new ring (older packets keep the old one)
            spool = self._spool
            at = spool.place(len(p.data))
            limit = at + len(p.data) - spool.capacity
            while self._packets and self._packets[0].spool is spool and self._packets[0].at < limit:
                self._pop_oldest()  # a burst bigger than the ring: give up the oldest moments
            spool.write(at, p.data)
        except (OSError, ValueError) as exc:  # temp folder full or unwritable: keep packets in memory
            log.warning("%s: replay spool unavailable (%s), keeping packets in memory", self.canvas_id, exc)
            self._spool_failed = True
            return
        p.spool, p.at, p.size, p.data = spool, at, len(p.data), None

    def _on_packet(self, p: _Packet) -> None:
        """Hook: each packet as it comes out of the encoder (output/shared.py also writes it to a file)."""

    def _trim(self) -> None:
        """Keep the last `seconds`, starting at a video keyframe so the clip plays from its first frame.

        The clip starts at the latest keyframe at or before the cutoff, so it can run up to one
        keyframe interval longer than asked, never shorter (unless less has been recorded).
        """
        # Runs after every encoded frame, so it only looks at the oldest keyframes rather than the
        # whole buffer (a 30 s buffer holds thousands of packets; scanning them all slowed the encoder
        # until frames piled up).
        keys = self._keys
        if not keys:
            return
        cutoff = self._newest - self.seconds
        while len(keys) > 1 and keys[1].seconds <= cutoff:
            keys.popleft()
        first = keys[0]
        while self._packets and self._packets[0] is not first:
            self._pop_oldest()

    def _keep(self, p: _Packet) -> None:
        self._packets.append(p)
        if p.kind == "video":
            self._newest = max(self._newest, p.seconds)
            if p.keyframe:
                self._keys.append(p)

    def _pop_oldest(self) -> _Packet:
        p = self._packets.popleft()
        if self._keys and self._keys[0] is p:
            self._keys.popleft()
        return p

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
        out = PacketFile(path, self.container_kind, self._templates, start.seconds,
                         self.audio_kinds("clips") or ("audio",))
        try:
            for p in packets:
                out.write(p)
        finally:
            out.close()


def audio_kind(index: int) -> str:
    return "audio" if index == 0 else f"audio{index}"


class PacketFile:
    """A video file written from already-encoded packets, with time 0 at `zero` seconds.

    templates is (video stream, audio stream per track); `audio` picks which audio tracks go in, by
    packet kind, in order. Packets of other kinds are left out."""

    def __init__(self, path: Path, container: str, templates, zero: float, audio: tuple[str, ...] = ("audio",)):
        import av

        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.zero = path, zero
        self._out = av.open(str(path), "w", format=CONTAINERS[container])
        video_t, *audio_t = templates
        self._streams = {"video": self._out.add_stream_from_template(video_t)}
        for kind in audio:
            template = audio_t[0 if kind == "audio" else int(kind[5:])]
            stream = self._out.add_stream_from_template(template)
            title = template.metadata.get("title")
            if title and len(audio) > 1:
                stream.metadata["title"] = title
            self._streams[kind] = stream

    def write(self, p: _Packet) -> None:
        import av

        offset = round(self.zero / p.time_base)
        if p.pts is not None and p.pts < offset:
            return  # audio from just before the first keyframe
        stream = self._streams.get(p.kind)
        if stream is None:
            return  # a track this file leaves out
        packet = av.Packet(p.payload())
        packet.pts = None if p.pts is None else p.pts - offset
        packet.dts = None if p.dts is None else p.dts - offset
        packet.time_base = p.time_base
        packet.is_keyframe = p.keyframe
        packet.stream = stream
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
            engine.mixer.add_tap(buf.push_audio, buf.tracks)
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
