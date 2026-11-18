"""One encoder per canvas, shared by the recording and the replay buffer.

On an NVIDIA card every encoder costs RAM (about 160 MB each even with one shared GPU context), and
recording plus the replay buffer used to run two per canvas. Here each canvas has a single
CanvasEncoder while either is on. It keeps the replay buffer's last seconds in memory, and a recording
is a file that the same encoded packets are also written to, starting at a keyframe asked for on the
spot. Recording and replay settings are the same (Settings → Recording), so nothing is lost.

Turning one on while the other runs joins the running encoder; the encoder stops when neither needs it.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from sprigcast.output.recorder import recording_path, resolve_codec, shrink_to
from sprigcast.output.replay import PacketFile, ReplayBuffer, _Packet, chapter_list, replay_path

log = logging.getLogger(__name__)


def part_path(first: Path, number: int) -> Path:
    """The file a split recording goes on in: "SprigCast 2026-11-16 19-10-53 horizontal part 2.mkv"."""
    return first.with_name(f"{first.stem} part {number}{first.suffix}")


class FileTap:
    """Writes an encoder's packets to a file on the side, from a keyframe asked for on the spot.

    Mixed into an encoder (a ReplayBuffer): the canvas encoder that recordings and clips share, and the
    stream's encoder when a recording is "Same as stream". Call _init_file_tap() from __init__ and send each
    packet to _to_file() while file_path is set.
    """

    def _init_file_tap(self, tracks: tuple[str, ...]) -> None:
        self.file: PacketFile | None = None  # the recording being written, on the encoder thread
        self.file_path: Path | None = None  # asked for; the file opens at the next keyframe
        self.file_container: str | None = None  # None: the encoder's own container
        self.file_error: str | None = None
        self._closing = False  # end the file at the next keyframe
        self._file_done = threading.Event()
        self.file_tracks: tuple[str, ...] = tracks  # what a recording keeps
        # Long recordings can go to several files (Settings → Recording → Split): a new one starts at
        # the first keyframe past split_seconds of video or split_bytes written, so nothing is lost between.
        self.split_seconds = 0.0
        self.split_bytes = 0
        self.file_parts: list[Path] = []  # every file this recording has written so far, in order
        self._file_zero = 0.0
        self._file_bytes = 0
        # Pause: nothing is written until resume; the file then carries on from a keyframe as if no time passed.
        self._paused = False
        self._resuming = False
        self._last_rel = 0.0  # seconds into the file of the last picture written
        self._marks: list[tuple[float, str]] = []  # chapter marks in the file being written
        self.file_chapters: dict[Path, list[dict]] = {}  # each finished part's chapters (for MP4s made plain)

    # Called from other threads ----------------------------------------------------------

    def start_file(self, path: Path, container: str | None = None, split_seconds: float = 0.0,
                   split_bytes: int = 0) -> list[Path]:
        """Start writing everything from the next keyframe (asked for right away) to `path`. Returns the list
        that fills with the files written (`path`, then "<name> part 2" and on when it splits)."""
        parts: list[Path] = []

        def job():
            if self.file is not None or self.file_path is not None:
                self.file_error = "already writing a file"
                raise RuntimeError("already writing a file")
            self.file_path, self.file_container, self.file_error = path, container, None
            self.split_seconds, self.split_bytes, self.file_parts = split_seconds, split_bytes, parts
            self._paused = self._resuming = False
            self._marks, self.file_chapters, self._last_rel = [], {}, 0.0
            self._force_key = True
        # Handed over without waiting: a new encoder only takes it once it's open (270 ms on the PC,
        # 2026-11-16, with the window waiting). Later requests (pause, stop) queue behind it, in order.
        self._ask(job, wait=False)
        return parts

    def pause_file(self) -> bool:
        """Stop writing for now; the file stays open. False when no file is being written."""
        def job():
            if self.file_path is None or self._paused:
                return False
            self._paused, self._resuming = True, False
            return True
        return bool(self._ask(job))

    def resume_file(self) -> bool:
        """Carry on in the same file from the next keyframe (asked for at once)."""
        def job():
            if self.file_path is None or not self._paused:
                return False
            self._paused, self._resuming, self._force_key = False, True, True
            return True
        return bool(self._ask(job))

    def mark_chapter(self, name: str) -> bool:
        """A chapter mark at the newest picture written ("Boss fight"). False when no file is being written."""
        def job():
            if self.file is None:
                return False
            self._marks.append((self._last_rel, name))
            return True
        return bool(self._ask(job))

    def stop_file(self, timeout: float = 10.0) -> None:
        """Finish the file while the encoder keeps running (the replay buffer or a stream still needs it).

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

    def _to_file(self, p: _Packet) -> None:
        try:
            if self._closing and p.kind == "video" and p.keyframe:
                self._close_file()
                return
            if self._paused:
                return
            if self._resuming and self.file is not None:
                if not (p.kind == "video" and p.keyframe):
                    return  # the file carries on at a keyframe
                # Move this file's clock on by the pause, so this picture follows the last one written.
                shift = (p.seconds - self.file.zero) - (self._last_rel + 1 / max(1, self.fps))
                self.file.zero += shift
                self._file_zero += shift
                self._resuming = False
            if self.file is not None and p.kind == "video" and p.keyframe and self._split_due(p):
                self._finish_part()  # the next part starts with this keyframe
            if self.file is None:
                if not (p.kind == "video" and p.keyframe):
                    return  # the file starts at a keyframe
                path = self.file_path if not self.file_parts else part_path(self.file_path, len(self.file_parts) + 1)
                self.file = PacketFile(path, self.file_container or self.container_kind, self._templates,
                                       p.seconds, self.audio_kinds(*self.file_tracks) or ("audio",), crash_safe=True)
                self.file_parts.append(path)
                self._file_zero, self._file_bytes = p.seconds, 0
                if len(self.file_parts) > 1:
                    log.info("recording %s: part %d", self.file_path.name, len(self.file_parts))
            began = time.perf_counter()
            self._file_bytes += len(p.data or b"") or p.size
            self.file.write(p)
            if p.kind == "video":
                self._last_rel = p.seconds - self.file.zero
            waited = time.perf_counter() - began
            self.mux_seconds += waited
            self.longest_stall = max(self.longest_stall, waited)
        except Exception as exc:  # disk full, drive removed: stop the file, keep the encoder going
            log.exception("recording %s failed", self.file_path)
            self.file_error = str(exc) or exc.__class__.__name__
            self._close_file()

    def _split_due(self, p: _Packet) -> bool:
        if self.split_seconds and p.seconds - self._file_zero >= self.split_seconds:
            return True
        return bool(self.split_bytes) and self._file_bytes >= self.split_bytes

    def _finish_part(self) -> None:
        out, self.file = self.file, None
        chapters = chapter_list(self._marks, self._last_rel + 1 / max(1, self.fps))
        self._marks = []
        if chapters:
            self.file_chapters[out.path] = chapters
        out.close(chapters)

    def _close_file(self) -> None:
        out, self.file_path, self._closing = self.file, None, False
        self._paused = self._resuming = False
        if out is not None:
            try:
                self._finish_part()
            except Exception as exc:
                self.file_error = self.file_error or str(exc) or exc.__class__.__name__
        self._file_done.set()

    def _before_close(self, container) -> bool:
        self._close_file()  # after the encoder's last packets are flushed into it
        return super()._before_close(container)


class CanvasEncoder(FileTap, ReplayBuffer):
    """A canvas's encoder: keeps `seconds` of packets for replays (0 = replay off) and can write a file."""

    def __init__(self, canvas_id, width, height, fps, bitrate_kbps, codec, start_time, container="mkv", encoder=None,
                 out_size=None, tracks: tuple[str, ...] = ("recording", "clips")):
        super().__init__(canvas_id, width, height, fps, bitrate_kbps, codec, start_time, 0.0, container, encoder,
                         out_size, tracks)
        self._init_file_tap(tuple(t for t in tracks if t != "clips"))

    def _video_options(self) -> dict[str, str]:
        from sprigcast.output.recorder import family

        opts = super()._video_options()
        if family(self.codec) == "nvenc":
            opts["forced-idr"] = "1"  # a keyframe asked for (a recording starting) is a clean IDR frame
        elif family(self.codec) in ("amf", "qsv") and opts:  # same for AMD and Intel
            opts["forced_idr"] = "1"
        return opts

    def _on_packet(self, p: _Packet) -> None:
        if self.file_path is not None:
            self._to_file(p)


class EncoderPool:
    """The running CanvasEncoders, by canvas, with who is using each ("recording", "replay")."""

    def __init__(self, engine):
        self.engine = engine
        self.encoders: dict[str, CanvasEncoder] = {}
        self._users: dict[str, set[str]] = {}
        # The mixer tracks a recording keeps (Settings → Audio, tracks 1-5), and their names in the file.
        # Encoders started from now on carry these plus the clips track.
        self.recording_tracks: tuple[str, ...] = ("recording",)
        self.track_titles: dict[str, str] = {}

    @property
    def voice_track(self) -> bool:
        return "voice" in self.recording_tracks

    @voice_track.setter
    def voice_track(self, on: bool) -> None:
        self.recording_tracks = ("recording", "voice") if on else ("recording",)

    @property
    def tracks(self) -> tuple[str, ...]:
        return (*self.recording_tracks, "clips")

    def acquire(self, user: str, canvas, bitrate_kbps: int, container: str, codec: str, encoder,
                short_side: int | None = None) -> CanvasEncoder:
        enc = self.encoders.get(canvas.id)
        if enc is not None and not enc.running:  # it failed: start a fresh one
            self._drop(canvas.id)
            enc = None
        if enc is None:
            enc = CanvasEncoder(canvas.id, canvas.width, canvas.height, canvas.fps, bitrate_kbps, codec,
                                time.perf_counter(), container, encoder,
                                shrink_to(canvas.width, canvas.height, short_side or getattr(canvas, "output_short_side", None)),
                                self.tracks)
            enc.file_tracks = self.recording_tracks
            enc.track_titles.update(self.track_titles)
            enc.start()
            self.encoders[canvas.id] = enc
            self._users[canvas.id] = set()
            self.engine.add_frame_tap(canvas.id, enc.push_frame, enc.gpu_size)
            self.engine.mixer.add_tap(enc.push_audio, enc.tracks)
        self._users[canvas.id].add(user)
        return enc

    def users(self, canvas_id: str) -> set[str]:
        return set(self._users.get(canvas_id, ()))

    def release(self, user: str, canvas_id: str, later: list | None = None) -> None:
        """`later`, when given, takes an encoder nobody uses any more instead of stopping it here: the caller
        stops it (enc.stop()) on a thread of its own. Stopping waits for the frames still queued to be encoded
        and the file to close: half a second and more, which froze the window when a recording ended (PC test
        2026-11-17, 583 ms). It gets no more frames or sound from here on either way."""
        users = self._users.get(canvas_id)
        if users is None:
            return
        users.discard(user)
        if not users:
            self._drop(canvas_id, later)

    def _drop(self, canvas_id: str, later: list | None = None) -> None:
        enc = self.encoders.pop(canvas_id)
        self._users.pop(canvas_id, None)
        self.engine.remove_frame_tap(canvas_id, enc.push_frame)
        self.engine.mixer.remove_tap(enc.push_audio)
        if later is None:
            enc.stop()
        else:
            later.append(enc)


class SharedRecording:
    """Records the chosen canvases, each to its own file, from the shared encoders."""

    def __init__(self, pool: EncoderPool, encoders: list[tuple[str, CanvasEncoder]], paths: list[Path],
                 borrowed: dict | None = None):
        self.pool = pool
        self.recorders = encoders  # (canvas id, encoder): what the stats panel reads
        self.parts: list[list[Path]] = []  # per canvas, every file written (more than one once it splits)
        self.borrowed = dict(borrowed or {})  # canvas id -> the stream's encoder this records from ("Same as stream")
        self._paths = paths
        self.started_at = time.monotonic()
        self._dropped_before = {cid: enc.dropped for cid, enc in encoders}
        self._eased_before = {cid: enc.eased for cid, enc in encoders}

    @classmethod
    def start(cls, pool: EncoderPool, canvases: list, folder: Path, bitrate_kbps: int, container: str = "mkv",
              codec: str | None = None, encoder=None, short_side: int | None = None,
              stream_encoders: dict | None = None, split_seconds: float = 0.0,
              split_bytes: int = 0) -> SharedRecording:
        """stream_encoders (canvas id -> a live stream's encoder): "Same as stream", those canvases are written
        from the packets already encoded for the stream, with no encoder of their own."""
        codec = codec or resolve_codec(encoder)
        now = datetime.now()
        encoders, paths, borrowed, parts = [], [], {}, []
        split = {"split_seconds": split_seconds, "split_bytes": split_bytes}
        try:
            for canvas in canvases:
                path = recording_path(folder, canvas.id, now, container)
                shared = (stream_encoders or {}).get(canvas.id)
                if shared is not None and shared.running:
                    shared.hold("recording")
                    borrowed[canvas.id] = shared
                    encoders.append((canvas.id, shared))
                    parts.append(shared.start_file(path, container, **split))
                else:
                    enc = pool.acquire("recording", canvas, bitrate_kbps, container, codec, encoder, short_side)
                    encoders.append((canvas.id, enc))
                    parts.append(enc.start_file(path, **split))
                paths.append(path)
        except Exception:
            for canvas_id, enc in encoders:
                enc.stop_file()  # queued after its start_file (which doesn't wait): ends whatever that began
                if canvas_id in borrowed:
                    enc.let_go("recording")
                else:
                    pool.release("recording", canvas_id)
            raise
        if borrowed:
            log.info("recording %s from the stream's encoder (Same as stream)", ", ".join(borrowed))
        made = cls(pool, encoders, paths, borrowed)
        made.parts = parts
        return made

    paused_since: float | None = None
    paused_total = 0.0

    @property
    def paused(self) -> bool:
        return self.paused_since is not None

    @property
    def elapsed(self) -> float:
        """How long the files run: paused time doesn't count."""
        now = time.monotonic()
        return now - self.started_at - self.paused_total - ((now - self.paused_since) if self.paused else 0.0)

    def pause(self) -> bool:
        if self.paused:
            return False
        for _, enc in self.recorders:
            enc.pause_file()
        self.paused_since = time.monotonic()
        return True

    def resume(self) -> bool:
        if not self.paused:
            return False
        for _, enc in self.recorders:
            enc.resume_file()
        self.paused_total += time.monotonic() - self.paused_since
        self.paused_since = None
        return True

    def mark_chapter(self, name: str) -> bool:
        """A chapter mark in every file at this moment. True when at least one file took it."""
        return any([enc.mark_chapter(name) for _, enc in self.recorders])

    @property
    def chapters(self) -> dict[Path, list[dict]]:
        found: dict[Path, list[dict]] = {}
        for _, enc in self.recorders:
            found.update(getattr(enc, "file_chapters", {}))
        return found

    @property
    def dropped(self) -> int:
        return sum(enc.dropped - self._dropped_before[cid] for cid, enc in self.recorders)

    @property
    def eased(self) -> int:
        """Frames left out on purpose while the encoder caught up (half frame rate for a while)."""
        return sum(getattr(enc, "eased", 0) - self._eased_before.get(cid, 0) for cid, enc in self.recorders)

    @property
    def errors(self) -> list[str]:
        return [f"{cid}: {enc.file_error or enc.error}" for cid, enc in self.recorders if enc.file_error or enc.error]

    @property
    def paths(self) -> list[Path]:
        """Every file written, each canvas's parts in order (just the first ones until a frame is written)."""
        written = [p for files in self.parts for p in files]
        return written if written else list(self._paths)

    def stop(self, later: list | None = None) -> None:
        """`later`: see EncoderPool.release. The files are finished once every encoder in it has stopped."""
        for canvas_id, enc in self.recorders:
            if canvas_id in self.borrowed:  # the stream's encoder: finish the file, and it ends if the stream has
                try:
                    enc.stop_file()
                except Exception as exc:
                    log.warning("finishing %s: %s", canvas_id, exc)
                enc.let_go("recording")
                continue
            if self.pool.users(canvas_id) != {"recording"}:  # the replay buffer keeps the encoder running
                try:
                    enc.stop_file()
                except Exception as exc:
                    log.warning("finishing %s: %s", canvas_id, exc)
            self.pool.release("recording", canvas_id, later)  # if last: the encoder flushes into the file and stops


class SharedReplay:
    """The replay buffer: the shared encoders keep their last `seconds`, saved on demand.

    While live with "Same as stream" on (and clips hearing what the stream hears), a canvas's clips come from
    the stream's own encoder instead of a second one: on an NVIDIA card that is one NVENC session (about
    160 MB) less for the whole stream. Moving over (follow) keeps the old encoder until the new one holds as many
    seconds, so a clip saved meanwhile is never shorter; the stream ending moves clips back the same way.
    """

    def __init__(self, pool: EncoderPool, encoders: list, folder: Path, container: str, canvases=None,
                 seconds: float = 0.0, settings: dict | None = None, borrowed: set | None = None):
        self.pool, self.buffers, self.folder, self.container = pool, encoders, folder, container
        self.canvases = list(canvases) if canvases is not None else []
        self.canvas_ids = [getattr(c, "id", None) or e.canvas_id for c, e in zip(self.canvases, encoders)] \
            if self.canvases else [e.canvas_id for e in encoders]
        self.seconds = seconds
        self._settings = settings or {}  # how to start a canvas encoder for clips again (pool.acquire)
        self._borrowed = set(borrowed or ())  # canvas ids whose clips come from the stream's encoder
        self._next: dict[str, tuple] = {}  # canvas id -> (encoder filling up to take over, borrowed?)

    @classmethod
    def start(cls, pool: EncoderPool, canvases: list, folder: Path, seconds: float, bitrate_kbps: int,
              container: str = "mkv", codec: str | None = None, encoder=None,
              short_side: int | None = None, stream_encoders: dict | None = None) -> SharedReplay:
        """stream_encoders (canvas id -> a live stream's encoder): those canvases keep their clips in the stream's
        encoder (see follow)."""
        codec = codec or resolve_codec(encoder)
        settings = {"bitrate_kbps": bitrate_kbps, "container": container, "codec": codec, "encoder": encoder,
                    "short_side": short_side}
        encoders, borrowed = [], set()
        for canvas in canvases:
            shared = (stream_encoders or {}).get(canvas.id)
            if shared is not None and shared.running:
                shared.hold("replay")
                shared.seconds = seconds
                borrowed.add(canvas.id)
                encoders.append(shared)
                continue
            enc = pool.acquire("replay", canvas, bitrate_kbps, container, codec, encoder, short_side)
            enc.seconds = seconds
            encoders.append(enc)
        if borrowed:
            log.info("clips of %s from the stream's encoder (Same as stream)", ", ".join(sorted(borrowed)))
        return cls(pool, encoders, folder, container, canvases, seconds, settings, borrowed)

    def follow(self, stream_encoders: dict, ending: list | None = None) -> None:
        """Call about once a second (UI thread): stream_encoders (canvas id -> a live stream's encoder, empty when
        clips shouldn't share it) is where each canvas's clips should come from. An encoder taking over first
        fills up to the clip length; then it serves the clips and the old one is let go (an encoder nobody else
        uses goes into `ending`, for the caller to stop on a thread of its own, or stops on one here)."""
        mine = ending is None
        ending = [] if mine else ending
        for i, canvas_id in enumerate(self.canvas_ids):
            canvas = self.canvases[i] if i < len(self.canvases) else None
            want = stream_encoders.get(canvas_id)
            if want is not None and not want.running:
                want = None
            current, borrowed = self.buffers[i], canvas_id in self._borrowed
            nxt = self._next.get(canvas_id)
            if want is not None and borrowed and current is want:
                if nxt is not None:  # the stream came back before the move away finished
                    self._let_go(canvas_id, *self._next.pop(canvas_id), ending)
                continue
            if want is None and not borrowed:
                if nxt is not None:  # the stream ended before the move to it finished
                    self._let_go(canvas_id, *self._next.pop(canvas_id), ending)
                continue
            if canvas is None and want is None:
                continue  # no way to start a canvas encoder again: stay on the stream's
            if nxt is not None and (nxt[0] is not want if want is not None else nxt[1]):
                self._let_go(canvas_id, *self._next.pop(canvas_id), ending)  # another stream encoder now
                nxt = None
            if nxt is None:
                try:
                    if want is not None:
                        want.hold("replay")
                        want.seconds = self.seconds
                        nxt = (want, True)
                    else:
                        st = self._settings
                        enc = self.pool.acquire("replay", canvas, st.get("bitrate_kbps", 6000),
                                                st.get("container", self.container), st.get("codec", "libx264"),
                                                st.get("encoder"), st.get("short_side"))
                        enc.seconds = self.seconds
                        nxt = (enc, False)
                except Exception as exc:  # noqa: BLE001 - clips stay where they are; tried again next time
                    log.warning("clips of %s can't move to %s: %s", canvas_id,
                                "the stream's encoder" if want is not None else "their own encoder", exc)
                    continue
                self._next[canvas_id] = nxt
                log.info("clips of %s moving to %s", canvas_id,
                         "the stream's encoder (Same as stream)" if nxt[1] else "their own encoder")
            enc, enc_borrowed = nxt
            have = current.buffered_seconds if current.running else 0.0
            if enc.running and enc.buffered_seconds + 0.5 < min(self.seconds, have):
                continue  # still filling up: clips keep coming from the old one
            self._next.pop(canvas_id, None)
            self.buffers[i] = enc
            if enc_borrowed:
                self._borrowed.add(canvas_id)
            else:
                self._borrowed.discard(canvas_id)
            self._let_go(canvas_id, current, borrowed, ending)
            log.info("clips of %s now from %s", canvas_id,
                     "the stream's encoder (Same as stream)" if enc_borrowed else "their own encoder")
        if mine and ending:
            self._end_later(ending)

    def _let_go(self, canvas_id: str, enc, borrowed: bool, ending: list) -> None:
        enc.seconds = 0.0  # whoever still uses it needn't keep the seconds
        if borrowed:
            enc.let_go("replay")  # the stream's: it ends here if the stream has (on_last_let_go)
        else:
            self.pool.release("replay", canvas_id, ending)

    @staticmethod
    def _end_later(ending: list) -> threading.Thread:
        def end() -> None:
            for enc in ending:
                try:
                    enc.stop()
                except Exception as exc:  # noqa: BLE001 (nothing is lost: its clips moved on)
                    log.warning("ending the clip encoder of %s: %s", enc.canvas_id, exc)

        thread = threading.Thread(target=end, name="clips-end")
        thread.start()
        return thread

    def save(self) -> list[Path]:
        """Save every canvas's last seconds, each to its own file, named with the same time."""
        when = datetime.now()
        return [enc.save(replay_path(self.folder, canvas_id, when, self.container), container=self.container)
                for canvas_id, enc in zip(self.canvas_ids, self.buffers)]

    def save_later(self, done) -> list[Path]:
        """As save(), without waiting for the files: returns their names at once and writes them on a thread of
        its own, then calls done(files written, problems) from that thread. Each encoder is only asked what
        the clip holds (between two frames); it goes on encoding while the file is written."""
        when = datetime.now()
        jobs = [(canvas_id, enc, replay_path(self.folder, canvas_id, when, self.container), enc.clip_snapshot())
                for canvas_id, enc in zip(self.canvas_ids, self.buffers)]

        def write() -> None:
            written, problems = [], []
            for canvas_id, enc, path, snapshot in jobs:
                try:
                    written.append(enc.write_clip(path, snapshot, self.container))
                except Exception as exc:  # noqa: BLE001 (this canvas's clip only)
                    log.warning("saving the clip %s: %s", path.name, exc)
                    problems.append(f"{canvas_id}: {exc}")
            done(written, problems)

        threading.Thread(target=write, name="clip-save", daemon=True).start()
        return [path for _canvas_id, _enc, path, _snapshot in jobs]

    @property
    def errors(self) -> list[str]:
        return [f"{cid}: {e.error}" for cid, e in zip(self.canvas_ids, self.buffers) if e.error]

    def stop(self, wait: bool = False) -> threading.Thread | None:
        """Clips off. An encoder nobody else uses stops on a thread of its own (returned; None when there is
        none): stopping waits for its queued frames and deletes its file of kept seconds, which froze the window
        for 314 ms when it ran here (PC test 2026-11-17). wait: stop them here instead (the app is closing)."""
        ending: list = []
        # A recording or stream still running keeps its encoder; it needn't keep the seconds.
        for canvas_id, enc in zip(self.canvas_ids, self.buffers):
            self._let_go(canvas_id, enc, canvas_id in self._borrowed, ending)
        for canvas_id, (enc, borrowed) in list(self._next.items()):
            self._let_go(canvas_id, enc, borrowed, ending)
        self._next.clear()
        self._borrowed.clear()
        if not ending:
            return None
        if wait:
            for enc in ending:
                try:
                    enc.stop()
                except Exception as exc:  # noqa: BLE001 (nothing is lost: clips are already off)
                    log.warning("ending the clip encoder of %s: %s", enc.canvas_id, exc)
            return None
        return self._end_later(ending)
