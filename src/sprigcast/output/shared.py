"""One encoder per canvas, shared by the recording and the replay buffer.

On an NVIDIA card every encoder costs RAM (about 160 MB each even with one shared GPU context), and
recording plus the replay buffer used to run two per canvas. Here each canvas has a single
CanvasEncoder while either is on. It keeps the replay buffer's last seconds in memory, and a recording
is a file that the same encoded packets are also written to, starting at a keyframe asked for on the
spot. Recording and replay settings are the same (Settings → Output → Recording), so nothing is lost.

Turning one on while the other runs joins the running encoder; the encoder stops when neither needs it.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from sprigcast.output.recorder import pick_codec, recording_path, shrink_to
from sprigcast.output.replay import PacketFile, ReplayBuffer, _Packet, replay_path

log = logging.getLogger(__name__)


class CanvasEncoder(ReplayBuffer):
    """A canvas's encoder: keeps `seconds` of packets for replays (0 = replay off) and can write a file."""

    def __init__(self, canvas_id, width, height, fps, bitrate_kbps, codec, start_time, container="mkv", encoder=None,
                 out_size=None, tracks: tuple[str, ...] = ("recording", "clips")):
        super().__init__(canvas_id, width, height, fps, bitrate_kbps, codec, start_time, 0.0, container, encoder,
                         out_size, tracks)
        self.file: PacketFile | None = None  # the recording being written, on the encoder thread
        self.file_path: Path | None = None  # asked for; the file opens at the next keyframe
        self.file_error: str | None = None
        self._closing = False  # end the file at the next keyframe
        self._file_done = threading.Event()

    def _video_options(self) -> dict[str, str]:
        opts = super()._video_options()
        if self.codec == "h264_nvenc":
            opts["forced-idr"] = "1"  # a keyframe asked for (a recording starting) is a clean IDR frame
        elif self.codec in ("h264_amf", "h264_qsv") and "profile" in opts:  # same for AMD and Intel
            opts["forced_idr"] = "1"
        return opts

    # Called from other threads ----------------------------------------------------------

    def start_file(self, path: Path) -> None:
        """Start writing everything from the next keyframe (asked for right away) to `path`."""
        def job():
            if self.file is not None or self.file_path is not None:
                raise RuntimeError("already writing a file")
            self.file_path, self.file_error = path, None
            self._force_key = True
        self._ask(job)

    def stop_file(self, timeout: float = 10.0) -> None:
        """Finish the file while the encoder keeps running (the replay buffer still needs it).

        A keyframe is asked for and the file ends just before it: everything shown before a keyframe
        has left the encoder by the time it comes out, so no frame is lost in the encoder's lookahead.
        """
        if not self.running:
            return  # the encoder thread closed it on its way out
        self._file_done.clear()

        def job():
            if self.file is None:
                self._close_file()  # nothing written yet (no keyframe came)
            else:
                self._closing = True
                self._force_key = True
        self._ask(job)
        if not self._file_done.wait(timeout):  # frames stopped coming: close what we have
            self._ask(self._close_file)

    # Encoder thread ---------------------------------------------------------------------

    def _on_packet(self, p: _Packet) -> None:
        if self.file_path is not None:
            self._to_file(p)

    def _to_file(self, p: _Packet) -> None:
        try:
            if self._closing and p.kind == "video" and p.keyframe:
                self._close_file()
                return
            if self.file is None:
                if not (p.kind == "video" and p.keyframe):
                    return  # the file starts at a keyframe
                self.file = PacketFile(self.file_path, self.container_kind, self._templates, p.seconds,
                                       self.audio_kinds("recording", "voice") or ("audio",))
            began = time.perf_counter()
            self.file.write(p)
            waited = time.perf_counter() - began
            self.mux_seconds += waited
            self.longest_stall = max(self.longest_stall, waited)
        except Exception as exc:  # disk full, drive removed: stop the file, keep the encoder for replays
            log.exception("recording %s failed", self.file_path)
            self.file_error = str(exc) or exc.__class__.__name__
            self._close_file()

    def _close_file(self) -> None:
        out, self.file, self.file_path, self._closing = self.file, None, None, False
        if out is not None:
            try:
                out.close()
            except Exception as exc:
                self.file_error = self.file_error or str(exc) or exc.__class__.__name__
        self._file_done.set()

    def _before_close(self, container) -> bool:
        self._close_file()  # after the encoder's last packets are flushed into it
        return super()._before_close(container)


class EncoderPool:
    """The running CanvasEncoders, by canvas, with who is using each ("recording", "replay")."""

    def __init__(self, engine):
        self.engine = engine
        self.encoders: dict[str, CanvasEncoder] = {}
        self._users: dict[str, set[str]] = {}
        self.voice_track = False  # recordings also get your voice on its own audio track (for new encoders)

    @property
    def tracks(self) -> tuple[str, ...]:
        return ("recording", "voice", "clips") if self.voice_track else ("recording", "clips")

    def acquire(self, user: str, canvas, bitrate_kbps: int, container: str, codec: str, encoder,
                short_side: int | None = None) -> CanvasEncoder:
        enc = self.encoders.get(canvas.id)
        if enc is not None and not enc.running:  # it failed: start a fresh one
            self._drop(canvas.id)
            enc = None
        if enc is None:
            enc = CanvasEncoder(canvas.id, canvas.width, canvas.height, canvas.fps, bitrate_kbps, codec,
                                time.perf_counter(), container, encoder,
                                shrink_to(canvas.width, canvas.height, short_side), self.tracks)
            enc.start()
            self.encoders[canvas.id] = enc
            self._users[canvas.id] = set()
            self.engine.add_frame_tap(canvas.id, enc.push_frame, enc.gpu_size)
            self.engine.mixer.add_tap(enc.push_audio, enc.tracks)
        self._users[canvas.id].add(user)
        return enc

    def users(self, canvas_id: str) -> set[str]:
        return set(self._users.get(canvas_id, ()))

    def release(self, user: str, canvas_id: str) -> None:
        users = self._users.get(canvas_id)
        if users is None:
            return
        users.discard(user)
        if not users:
            self._drop(canvas_id)

    def _drop(self, canvas_id: str) -> None:
        enc = self.encoders.pop(canvas_id)
        self._users.pop(canvas_id, None)
        self.engine.remove_frame_tap(canvas_id, enc.push_frame)
        self.engine.mixer.remove_tap(enc.push_audio)
        enc.stop()


class SharedRecording:
    """Records the chosen canvases, each to its own file, from the shared encoders."""

    def __init__(self, pool: EncoderPool, encoders: list[tuple[str, CanvasEncoder]], paths: list[Path]):
        self.pool = pool
        self.recorders = encoders  # (canvas id, encoder): what the stats panel reads
        self._paths = paths
        self.started_at = time.monotonic()
        self._dropped_before = {cid: enc.dropped for cid, enc in encoders}

    @classmethod
    def start(cls, pool: EncoderPool, canvases: list, folder: Path, bitrate_kbps: int, container: str = "mkv",
              codec: str | None = None, encoder=None, short_side: int | None = None) -> SharedRecording:
        codec = codec or pick_codec()
        now = datetime.now()
        encoders, paths = [], []
        try:
            for canvas in canvases:
                enc = pool.acquire("recording", canvas, bitrate_kbps, container, codec, encoder, short_side)
                encoders.append((canvas.id, enc))
                path = recording_path(folder, canvas.id, now, container)
                enc.start_file(path)
                paths.append(path)
        except Exception:
            for canvas_id, enc in encoders:
                if enc.file_path is not None:
                    enc.stop_file()
                pool.release("recording", canvas_id)
            raise
        return cls(pool, encoders, paths)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def dropped(self) -> int:
        return sum(enc.dropped - self._dropped_before[cid] for cid, enc in self.recorders)

    @property
    def errors(self) -> list[str]:
        return [f"{cid}: {enc.file_error or enc.error}" for cid, enc in self.recorders if enc.file_error or enc.error]

    @property
    def paths(self) -> list[Path]:
        return list(self._paths)

    def stop(self) -> None:
        for canvas_id, enc in self.recorders:
            if self.pool.users(canvas_id) != {"recording"}:  # the replay buffer keeps the encoder running
                try:
                    enc.stop_file()
                except Exception as exc:
                    log.warning("finishing %s: %s", canvas_id, exc)
            self.pool.release("recording", canvas_id)  # if last: the encoder flushes into the file and stops


class SharedReplay:
    """The replay buffer: the shared encoders keep their last `seconds`, saved on demand."""

    def __init__(self, pool: EncoderPool, encoders: list[CanvasEncoder], folder: Path, container: str):
        self.pool, self.buffers, self.folder, self.container = pool, encoders, folder, container

    @classmethod
    def start(cls, pool: EncoderPool, canvases: list, folder: Path, seconds: float, bitrate_kbps: int,
              container: str = "mkv", codec: str | None = None, encoder=None,
              short_side: int | None = None) -> SharedReplay:
        codec = codec or pick_codec()
        encoders = []
        for canvas in canvases:
            enc = pool.acquire("replay", canvas, bitrate_kbps, container, codec, encoder, short_side)
            enc.seconds = seconds
            encoders.append(enc)
        return cls(pool, encoders, folder, container)

    def save(self) -> list[Path]:
        """Save every canvas's last seconds, each to its own file, named with the same time."""
        when = datetime.now()
        return [enc.save(replay_path(self.folder, enc.canvas_id, when, self.container)) for enc in self.buffers]

    @property
    def errors(self) -> list[str]:
        return [f"{e.canvas_id}: {e.error}" for e in self.buffers if e.error]

    def stop(self) -> None:
        for enc in self.buffers:
            enc.seconds = 0.0  # a recording still running keeps the encoder; it needn't keep the seconds
            self.pool.release("replay", enc.canvas_id)
