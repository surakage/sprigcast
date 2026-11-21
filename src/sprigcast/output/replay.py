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
import os
import queue
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from fractions import Fraction
from pathlib import Path

from sprigcast.output.recorder import CONTAINERS, Recorder, pick_codec, resolve_codec

log = logging.getLogger(__name__)


_TIME_BASES: dict[tuple[int, int], Fraction] = {}


def _time_base(tb) -> Fraction:
    """One shared Fraction per time base: PyAV hands out a new one with every packet."""
    key = (tb.numerator, tb.denominator)
    shared = _TIME_BASES.get(key)
    if shared is None:
        shared = _TIME_BASES[key] = Fraction(*key)
    return shared


class _Packet:
    """One encoded packet in the buffer. Slotted, with its time worked out once: a clips buffer holds
    thousands (6,254 at the end of the 2026-11-15 stream): 241 bytes each against 425 as a dataclass."""

    __slots__ = ("kind", "data", "pts", "dts", "time_base", "keyframe", "spool", "at", "size", "seconds")

    def __init__(self, kind: str, data: bytes | None, pts: int | None, dts: int | None, time_base,
                 keyframe: bool, spool: Spool | None = None, at: int = 0, size: int = 0):
        self.kind = kind  # "video", "audio" (the first track) or "audio1", "audio2"... (other tracks)
        self.data = data  # None once the bytes are in `spool`
        self.pts, self.dts = pts, dts
        self.time_base = _time_base(time_base)
        self.keyframe = keyframe
        self.spool = spool
        self.at = at  # position in the spool (always growing; see Spool)
        self.size = size
        self.seconds = float((pts if pts is not None else dts or 0) * self.time_base)

    def payload(self) -> bytes:
        data = self.data  # read once: the encoder thread may move the bytes to the spool meanwhile
        return data if data is not None else self.spool.read(self.at, self.size)


class Spool:
    """A fixed-size ring of packet bytes in a temporary file (deleted when closed).

    Positions only ever grow; the byte in the file is at position % capacity. A packet never wraps
    around the end, so it may skip ahead to the next lap. Packets more than one lap behind the write
    position have been overwritten (`oldest_kept`). Written from the encoder thread; a clip being saved reads
    it from its own thread (ReplayBuffer.write_clip), so each seek-and-read or seek-and-write holds `lock`.
    """

    def __init__(self, capacity: int, folder: str | None = None):
        self.capacity = capacity
        self.file = tempfile.TemporaryFile(prefix="sprigcast-replay-", dir=folder)
        self.end = 0
        self.lock = threading.Lock()

    def place(self, size: int) -> int:
        """Where a packet of `size` bytes will go (call write() next)."""
        if size > self.capacity:
            raise ValueError("packet larger than the replay spool")
        at = self.end
        if at % self.capacity + size > self.capacity:
            at += self.capacity - at % self.capacity  # start the next lap rather than wrap mid-packet
        return at

    def write(self, at: int, data: bytes) -> None:
        with self.lock:
            self.file.seek(at % self.capacity)
            self.file.write(data)
            self.end = at + len(data)

    def oldest_kept(self) -> int:
        return self.end - self.capacity

    def read(self, at: int, size: int) -> bytes:
        with self.lock:
            if at < self.oldest_kept():
                raise RuntimeError("replay data was overwritten")
            self.file.flush()
            self.file.seek(at % self.capacity)
            return self.file.read(size)

    def close(self) -> None:
        self.file.close()


KEY_SLACK_S = 10.0  # sound kept past `seconds` while waiting for a keyframe (several keyframe intervals)


def spool_capacity(bitrate_kbps: int, seconds: float) -> int:
    """Room for `seconds` plus a keyframe interval, twice over for VBR peaks and audio; at least 16 MB."""
    return max(16 << 20, int(bitrate_kbps * 1000 / 8 * (seconds + 10) * 2))


def replay_path(folder: Path, canvas_id: str, when: datetime, container: str) -> Path:
    """`2026-11-12/SprigCast Replay 2026-11-12 19-30-05 horizontal.mkv` (a folder per day; prefix from
    Settings → Recording → File names), with a counter if that name is taken."""
    from sprigcast.output import naming, storage

    return naming.unique(storage.day_folder(folder, when), naming.replay_name(canvas_id, when), container)


class ReplayBuffer(Recorder):
    """A Recorder whose packets go to a rolling in-memory buffer of `seconds` instead of a file."""

    # With the native engine, the engine's writer encodes on its own thread and hands the packets back here
    # (packets mode, _writer_after): they go to the buffer, a file and the streams as PyAV's do.
    packets_mode = True

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
        self._streams_path: str | None = None  # packets mode: the writer's streams' file, read for the templates
        self._streams = None  # packets mode: the container the templates come from (kept open with them)
        self._requests: queue.Queue = queue.Queue()
        # The engine's writer keeps the clips' seconds itself (sc_writer_keep, interface 41): no packet is kept,
        # moved to a ring or written into a clip under Python's lock. Set as the writer opens.
        self._engine_keeps = False
        self._engine_kept: float | None = None  # the seconds the writer was last told to keep
        self._engine_hands_out = True
        self._ring_path: str | None = None

    # Called from other threads ------------------------------------------------------

    def save(self, path: Path, timeout: float = 20.0, container: str | None = None) -> Path:
        """Write the buffered seconds to `path` (as `container`, else this encoder's). Blocks until written; raises
        if it couldn't be."""
        self._ask(lambda: self.write_clip(path, self._clip_packets(), container), timeout)
        return path

    def clip_snapshot(self, timeout: float = 20.0):
        """What a clip saved now would hold, taken on the encoder thread between two frames (a list of packets:
        no bytes are read or copied). write_clip() makes the file from it, on any thread. The two steps let
        the window ask for a clip without waiting for the file: saving on the encoder thread held the window
        1.3 s on the PC (2026-11-17), and the encoder encoded nothing meanwhile."""
        return self._ask(self._clip_packets, timeout)

    def write_clip(self, path: Path, snapshot, container: str | None = None) -> Path:
        """Write a clip_snapshot() to `path`, as `container` (else this encoder's: a stream's is FLV, its clips
        aren't). The packets' bytes are still in the buffer (it is overwritten only after as many seconds again);
        if it has been by now, or the buffer was closed, this raises."""
        if snapshot[0] == "engine":  # the engine writes it from the packets it kept (this thread, no lock held)
            _, clip, tracks = snapshot
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                clip.write(path, CONTAINERS[container or self.container_kind], tracks=tracks)
            finally:
                clip.close()
            return path
        packets, templates, kinds, zero = snapshot
        out = PacketFile(path, container or self.container_kind, templates, zero, kinds)
        try:
            for p in packets:
                out.write(p)
        finally:
            out.close()
        return path

    def _ask(self, job, timeout: float = 20.0, wait: bool = True):
        """Run job() on the encoder thread, between frames, and wait for it. Raises what it raised.
        wait=False: just hand it over (it runs in order with the rest; a failure is logged there)."""
        done: dict = {}
        finished = threading.Event()
        if not self.running:
            raise RuntimeError(self.error or "the encoder has stopped")
        self._requests.put((job, done, finished))
        if not wait:
            return None
        if not finished.wait(timeout):
            raise TimeoutError("the encoder did not answer in time")
        if "error" in done:
            raise RuntimeError(done["error"])
        return done.get("result")

    @property
    def buffered_seconds(self) -> float:
        writer = self.writer if self._engine_keeps else None
        if writer is not None:
            try:
                return writer.kept()[0]
            except Exception:  # noqa: BLE001 - it closed meanwhile
                return 0.0
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
            self.bytes_out += packet.size
            self._take(_Packet(kind, bytes(packet), packet.pts, packet.dts,
                               packet.time_base or packet.stream.time_base, bool(packet.is_keyframe)))
        self._trimmed()

    def _take(self, p: _Packet) -> None:
        """One encoded packet: kept for clips, handed on (a file, the streams), its bytes moved to the ring."""
        if not self._engine_keeps:
            self._keep(p)
        began = time.perf_counter()
        self._on_packet(p)  # a recording file gets the bytes straight away
        if self.seconds > 0 and not self._spool_failed and not self._engine_keeps:
            self._to_spool(p)
        waited = time.perf_counter() - began
        self.mux_seconds += waited
        self.longest_stall = max(self.longest_stall, waited)

    def _trimmed(self) -> None:
        self._trim()
        if self.seconds <= 0 and self._spool is not None and not (self._packets and self._packets[0].spool):
            self._spool.close()  # replay turned off and spooled packets (always the oldest) are gone
            self._spool = None

    # Packets mode: the engine's writer encodes (Recorder._writer_session) ------------------------------------

    def _streams_file(self) -> str:
        self._streams_path = super()._streams_file()
        return self._streams_path

    def _writer_opened(self, writer):
        stats = super()._writer_opened(writer)
        if getattr(writer, "packets", False) and stats.state_name == "running":
            self._templates = self._read_streams()  # before any packet: a stream link connects at once
            self._engine_keeps = (self.engine_clips and hasattr(writer, "keep")
                                  and not os.environ.get("SPRIGCAST_NO_NATIVE_CLIPS"))
            self._engine_kept, self._engine_hands_out = None, True
        return stats

    engine_clips = False  # encoders whose clips the engine may keep (the shared ones: CanvasEncoder, streams)

    def _python_wants_packets(self) -> bool:
        """Whether anything in Python still takes this encoder's packets (a file or stream the engine doesn't
        write or send itself). False: the writer stops queueing them (sc_writer_hand_out)."""
        return True

    def _writer_before(self, writer) -> None:
        super()._writer_before(writer)
        if not self._engine_keeps:
            return
        if self.seconds != self._engine_kept:
            try:
                if self.seconds > 0 and self._ring_path is None and not self._spool_failed:
                    self._ring_path = self._ring_file()
                writer.keep(self.seconds, self._ring_path)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: the engine couldn't keep clips (%s)", self.canvas_id, exc)
            self._engine_kept = self.seconds
        wanted = self._python_wants_packets()
        if wanted != self._engine_hands_out:
            writer.hand_out(wanted)
            self._engine_hands_out = wanted

    def _ring_file(self) -> str | None:
        """A name for the writer's ring file of clip bytes (it makes and removes the file itself)."""
        import tempfile

        try:
            fd, path = tempfile.mkstemp(prefix="sprigcast-replay-", suffix=".ring")
            os.close(fd)
            return path
        except OSError as exc:
            log.warning("%s: replay spool unavailable (%s), keeping packets in memory", self.canvas_id, exc)
            self._spool_failed = True
            return None

    def _writer_after(self, writer) -> None:
        """Take the packets the writer has encoded so far; then run what other threads asked for (_ask)."""
        first = writer.take_packet()
        if first is not None:
            kinds = ("video", *(audio_kind(i) for i in range(len(self._templates) - 1)))
            p = first
            while p is not None:
                track, key, pts, dts, tb, data = p
                self._take(_Packet(kinds[track] if track < len(kinds) else "audio", data, pts, dts, Fraction(*tb),
                                   key))
                p = writer.take_packet()
            self._trimmed()
        self._serve_requests()

    def _read_streams(self):
        """The writer's streams' file, finished as it opened: the codec headers (SPS/PPS, AAC config) and sizes
        that saved clips and stream muxers copy, as PyAV's own encoders give them."""
        import av

        path = self._streams_path
        with open(path, "rb") as f:
            data = f.read()
        self._remove_streams_file()
        import av.logging as avlog

        # The file holds headers only: FFmpeg's look for stream details tries to decode packets that aren't there
        # and logs "no frame!", "Input buffer exhausted" and "Could not find codec parameters" at every stream start
        # (PC, 2026-11-18). Expected here: kept out of the log.
        with avlog.Capture(True) as said:
            self._streams = av.open(io.BytesIO(data), "r", format="nut")
        if said:
            log.debug("%s: reading the writer's stream headers: %s", self.path.stem,
                      " / ".join(str(line[2]).strip() for line in said[:6]))
        return (self._streams.streams.video[0], *self._streams.streams.audio)

    def _remove_streams_file(self) -> None:
        path, self._streams_path = self._streams_path, None
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    def _writer_closed(self) -> None:
        self._remove_streams_file()  # never opened: the writer failed before its first packets
        self._engine_keeps = False
        path, self._ring_path = self._ring_path, None
        if path:
            for name in (path, *(f"{path}.{n}" for n in range(1, 9))):  # the writer removed its own already
                try:
                    os.remove(name)
                except OSError:
                    pass
        self._before_close(None)  # a file of FileTap's is finished after the last packets went in

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
            # No picture keyframe yet (the video encoder is stalled or every frame was dropped): a clip
            # can't start anywhere, so don't let the sound pile up behind it. In a local soak where video
            # never got through, audio packets grew by ~1,500 a minute until stopped.
            packets = self._packets
            if packets:
                newest = packets[-1].seconds
                while packets and packets[0].seconds < newest - max(self.seconds, 1.0) - KEY_SLACK_S:
                    self._pop_oldest()
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
        self._serve_requests()

    def _serve_requests(self) -> None:
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

    def _clip_packets(self):
        writer = self.writer if self._engine_keeps else None
        if writer is not None:
            from sprigcast import sprigcore

            kinds = self.audio_kinds("clips") or ("audio",)
            try:
                clip = writer.clip_snapshot()
            except sprigcore.NativeError as exc:
                raise RuntimeError("nothing buffered yet") from exc
            return ("engine", clip, tuple(0 if kind == "audio" else int(kind[5:]) for kind in kinds))
        packets = list(self._packets)
        start = next((p for p in packets if p.kind == "video" and p.keyframe), None)
        if start is None or self._templates is None:
            raise RuntimeError("nothing buffered yet")
        return (packets[packets.index(start):], self._templates, self.audio_kinds("clips") or ("audio",),
                start.seconds)

    def _write_clip(self, path: Path) -> None:
        self.write_clip(path, self._clip_packets())


def audio_kind(index: int) -> str:
    return "audio" if index == 0 else f"audio{index}"


FRAGMENTED = "frag_keyframe+empty_moov+default_base_moof"


class PacketFile:
    """A video file written from already-encoded packets, with time 0 at `zero` seconds.

    templates is (video stream, audio stream per track); `audio` picks which audio tracks go in, by
    packet kind, in order. Packets of other kinds are left out."""

    def __init__(self, path: Path, container: str, templates, zero: float, audio: tuple[str, ...] = ("audio",),
                 crash_safe: bool = False):
        import av

        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.zero = path, zero
        # crash_safe (recordings): an MP4 is written in fragments, each playable as soon as it's written, so
        # a crash or power cut leaves a file that plays up to that moment. finish_mp4() makes it a plain MP4
        # once it's closed properly. MKV plays after a crash anyway.
        options = {"movflags": FRAGMENTED, "flush_packets": "1"} if crash_safe and container == "mp4" else {}
        self._out = av.open(str(path), "w", format=CONTAINERS[container], options=options)
        video_t, *audio_t = templates
        self._streams = {"video": self._out.add_stream_from_template(video_t)}
        for kind in audio:
            template = audio_t[0 if kind == "audio" else int(kind[5:])]
            stream = self._out.add_stream_from_template(template)
            title = template.metadata.get("title")
            if title and len(audio) > 1:
                stream.metadata["title"] = title
            self._streams[kind] = stream
        self._last_dts: dict[str, int] = {}

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
        stamp = packet.dts if packet.dts is not None else packet.pts
        if stamp is not None:
            if stamp <= self._last_dts.get(p.kind, -1):
                return  # sound from just before a resumed recording's keyframe: already covered
            self._last_dts[p.kind] = stamp
        packet.time_base = p.time_base
        packet.is_keyframe = p.keyframe
        packet.stream = stream
        self._out.mux(packet)

    def close(self, chapters: list[dict] | None = None) -> None:
        """chapters: chapter_list() entries, written into the file as it closes (MKV; an MP4 gets them when
        it's made plain, naming.finish_mp4)."""
        if chapters:
            try:
                self._out.set_chapters(chapters)
            except Exception as exc:  # a container without chapters: the file is still fine
                log.info("chapters left out of %s: %s", self.path.name, exc)
        self._out.close()


def chapter_list(marks: list[tuple[float, str]], end: float) -> list[dict]:
    """Chapters from (seconds into the file, name) marks: each runs to the next one or the end. A first
    chapter "Start" covers the time before the first mark, so players show the whole file."""
    from fractions import Fraction

    marks = sorted((max(0.0, t), name) for t, name in marks if t <= end)
    if not marks:
        return []
    if marks[0][0] > 0.5:
        marks.insert(0, (0.0, "Start"))
    ms = Fraction(1, 1000)
    out = []
    for i, (t, name) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else max(end, t + 0.001)
        out.append({"id": i + 1, "start": round(t * 1000), "end": max(round(stop * 1000), round(t * 1000) + 1),
                    "time_base": ms, "metadata": {"title": name}})
    return out


class ReplaySession:
    """One replay buffer per chosen canvas, fed by the engine like a recording."""

    def __init__(self, engine, buffers: list[ReplayBuffer], folder: Path, container: str):
        self.engine, self.buffers, self.folder, self.container = engine, buffers, folder, container

    @classmethod
    def start(cls, engine, canvases: list, folder: Path, seconds: float, bitrate_kbps: int, container: str = "mkv",
              codec: str | None = None, encoder=None) -> ReplaySession:
        codec = codec or resolve_codec(encoder)
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
