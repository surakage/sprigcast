"""Media: plays a video or sound file (MP4, MOV, MKV, WebM, MP3, WAV, OGG, FLAC...).

A video file is placed in a bloom like a picture: it plays while its bloom is on screen, and its sound
goes to the mixer. A sound file has no picture, so it lives in Sound like a mic and plays in every bloom.

A small thread decodes a little ahead (a few frames, about a second of sound) and the render and mixer
threads take what's due on the media's own clock, so picture and sound stay together. Paused, the
thread waits; asleep (its bloom off screen) the file is closed and every frame let go.

Controls (play, pause, restart, stop, seek) are safe to call from any thread: the control API, hotkeys
and the right-click menu call them directly.

Hidden with its eye (still in a bloom on screen), a video pauses; shown again it starts over or carries on
(restart_on_show). The renderer tells it with set_hidden() (Engine._show_hidden).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.core.frames import owned
from sprigcast.core.source import Source, register
from sprigcast.render.stinger import LIBVPX, fit_size
from sprigcast.sources.audio import SAMPLE_RATE

log = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".opus", ".wma", ".aif", ".aiff"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".ts", ".gif", ".wmv", ".mpg", ".mpeg"}
AHEAD_FRAMES = 3  # pictures decoded ahead of the one on screen (a 1080p picture is 8 MB)
AHEAD_AUDIO_S = 1.0  # sound decoded ahead of what's playing
SYNC_S = 0.08  # sound further off the clock than this is skipped or waited for
MAX_SIZE = (1920, 1920)  # bigger videos are shrunk while decoding: no canvas needs more
STATES = ("playing", "paused", "stopped", "ended")
SPEEDS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
HW_DEVICES = ("d3d11va", "cuda", "dxva2", "videotoolbox", "vaapi")  # tried in this order, where FFmpeg has them



def decode_threads(width: int, height: int) -> int:
    """Decoder threads for a video this size. FFmpeg's own choice is one per CPU thread (17 on an 8-core
    Ryzen), started again each time a looping file comes round: the thread count swung by a hundred
    minute to minute in the 2026-11-15 soak. Two decode up to 1080p far faster than real time."""
    return 2 if width * height <= 1920 * 1088 else 4

def clamp_speed(value: Any) -> float:
    try:
        return min(4.0, max(0.1, float(value)))
    except (TypeError, ValueError):
        return 1.0


def _hw_accel():
    """Hardware decoding for this PC (Windows: Direct3D 11), or None when FFmpeg has none."""
    try:
        from av.codec.hwaccel import HWAccel, hwdevices_available
    except ImportError:  # PyAV before 14
        return None
    have = set(hwdevices_available())
    kind = next((k for k in HW_DEVICES if k in have), None)
    return HWAccel(kind, allow_software_fallback=True) if kind else None


COLOR_MATRICES = {1: "709", 5: "601", 6: "601", 9: "2020", 10: "2020"}  # FFmpeg AVColorSpace -> ours


def pack_i420(frame, size: tuple[int, int]) -> tuple[np.ndarray, str] | None:
    """A decoded 4:2:0 picture as one 1-channel array for the GPU to convert (render/compositor.py src()):
    Y rows, then rows of U | V side by side, then the alpha rows. ("I420A/709/tv", ...) says how to read it.

    About 40% of the RGBA picture it replaces (1080p: 5 MB with alpha instead of 8), and the CPU skips
    the colour conversion. None when it doesn't fit (another pixel format, an odd size, a shrink needed).
    """
    kind = {"yuv420p": "I420", "yuvj420p": "I420", "yuva420p": "I420A", "nv12": "I420"}.get(frame.format.name)
    w, h = frame.width, frame.height
    if kind is None or (w, h) != tuple(size) or w % 2 or h % 2:
        return None
    cw, ch = w // 2, h // 2
    out = np.empty((h + ch + (h if kind == "I420A" else 0), w), dtype=np.uint8)

    def plane(i: int, width: int, rows: int) -> np.ndarray:
        p = frame.planes[i]
        return np.frombuffer(p, dtype=np.uint8).reshape(-1, p.line_size)[:rows, :width]

    out[:h] = plane(0, w, h)
    if frame.format.name == "nv12":  # hardware decoding: U and V share one plane, UVUV...
        uv = plane(1, w, ch)
        out[h:h + ch, :cw] = uv[:, 0::2]
        out[h:h + ch, cw:] = uv[:, 1::2]
    else:
        out[h:h + ch, :cw] = plane(1, cw, ch)
        out[h:h + ch, cw:] = plane(2, cw, ch)
    if kind == "I420A":
        out[h + ch:] = plane(3, w, h)
    # Untagged video is read as BT.601 TV range, as FFmpeg's own conversion did before.
    matrix = COLOR_MATRICES.get(int(frame.colorspace or 2), "601")
    full = int(frame.color_range or 0) == 2 or frame.format.name.startswith("yuvj")  # JPEG-style full range
    return out, f"{kind}/{matrix}/{'pc' if full else 'tv'}"


def _rewind(container) -> bool:
    """Back to the start of an open file for the next pass of a loop, with its decoders reset. False if the
    file can't seek (then it's opened again). Opening the file again on every pass started FFmpeg's
    threads again each time: a 3-second GIF looping all show added threads every few seconds."""
    try:
        container.seek(0)
        for stream in container.streams:
            if stream.type in ("video", "audio") and stream.codec_context.is_open:
                stream.codec_context.flush_buffers()
        return True
    except Exception as exc:  # noqa: BLE001 (a format without seeking: open it again)
        log.info("media: can't seek back in %s (%s); opening it again", getattr(container, "name", "?"), exc)
        return False


def _picture(frame, size: tuple[int, int]) -> tuple[np.ndarray, str]:
    packed = pack_i420(frame, size)
    if packed is not None:
        return packed
    from sprigcast.core.frames import picture_array

    return picture_array(frame, width=size[0], height=size[1], format="rgba"), "RGBA"


def is_sound_file(path: str) -> bool:
    return Path(str(path)).suffix.lower() in AUDIO_EXTS


def file_filter() -> str:
    video = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
    audio = " ".join(f"*{e}" for e in sorted(AUDIO_EXTS))
    return f"Video and sound ({video} {audio});;Video ({video});;Sound ({audio});;All files (*)"


@register("media")
class MediaSource(Source):
    """Settings: path, loop, restart_on_show (start over each time it comes on screen or is shown again),
    clear_on_end (show nothing once it ends instead of the last picture), play_on_start (sound files: start
    playing when SprigCast opens; otherwise they wait for play), speed (0.25 to 2; sound changes pitch with it),
    hw_decode (decode on the graphics card), volume_db, muted, monitor."""

    label = "Video or sound file"
    defaults = {"path": "", "loop": False, "restart_on_show": True, "clear_on_end": False, "play_on_start": False,
                "speed": 1.0, "hw_decode": False}

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._frames: deque[tuple[float, np.ndarray, str]] = deque()  # (time, picture, its channel_order)
        self.channel_order = "RGBA"  # of the picture video_frame() last returned (see pack_i420)
        self._audio: deque[tuple[float, np.ndarray]] = deque()  # (time of first sample, samples)
        self._audio_s = 0.0
        self._state = "playing"  # a sound file waits for play unless play_on_start (set below)
        self._base = 0.0  # position when the clock last stopped or jumped
        self._anchor: float | None = None  # monotonic time the clock started running from _base
        self._generation = 0  # bumped by a seek or restart: the decoder starts over
        self._start_at = 0.0
        self._finished_at: float | None = None  # timeline end once the file (not looping) is fully decoded
        self._shown: float | None = None
        self._hidden = False  # its eye is off (set_hidden)
        self._paused_by_hide = False
        self._speed = clamp_speed(settings.get("speed", 1.0))  # the speed the decoder and clock run at now
        self._blank: np.ndarray | None = None
        self._size = (0, 0)
        self.duration: float | None = None
        self.has_video = not is_sound_file(settings.get("path", ""))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = "starting" if settings.get("path") else "no file"
        if not self.has_video and not settings.get("play_on_start"):
            self._state = "stopped"

    # A video sleeps while its bloom is off screen; a sound file belongs to Sound and keeps playing.
    @property
    def sleeps(self) -> bool:  # type: ignore[override]
        return self.has_video

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    @property
    def gain(self) -> float:
        if self.settings.get("muted"):
            return 0.0
        return float(10 ** (float(self.settings.get("volume_db", 0.0)) / 20))

    # Lifecycle (render thread) ---------------------------------------------------------------

    def start(self) -> None:
        """Start a decoder thread. Never waits for an old one: a thread that is no longer self._thread
        stops on its own at its next check (waiting here froze the picture, and a join that timed out
        used to leave two threads decoding the same file, each with its own decoder threads)."""
        if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
            return
        self._stop.clear()
        thread = threading.Thread(target=self._run, name=f"media-{self.id}", daemon=True)
        self._thread = thread
        with self._wake:
            self._wake.notify_all()  # an old thread still waiting sees it isn't the one any more
        thread.start()

    def _mine(self) -> bool:
        """True on the decoder thread that is current (an old one finishing up gets False)."""
        return self._thread is threading.current_thread()

    def stop(self) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
        with self._lock:
            self._frames.clear()
            self._audio.clear()
            self._audio_s = 0.0

    def sleep(self) -> None:
        with self._lock:
            position = self._position()
            self._pause_clock()
            self._base = position
        self._stop.set()
        self._thread = None  # it closes its file and decoder and ends by itself; nothing waits for it
        with self._wake:
            self._wake.notify_all()
        with self._lock:
            self._frames.clear()
            self._audio.clear()
            self._audio_s = 0.0
            self._shown = None

    def wake(self) -> None:
        self._hidden = self._paused_by_hide = False  # the renderer says again if its eye is off
        if self.settings.get("restart_on_show", True):
            self._jump(0.0, "playing")
        else:
            with self._lock:
                self._jump_locked(self._base % self.duration if self.duration else self._base, self._state)
        self.start()

    def update(self, settings: dict[str, Any]) -> None:
        new_file = settings.get("path") != self.settings.get("path")
        reopen = bool(settings.get("hw_decode")) != bool(self.settings.get("hw_decode"))
        super().update(settings)
        if new_file:
            self.has_video = not is_sound_file(settings.get("path", ""))
            self.duration = None
            self._size = (0, 0)
            self.status = "starting" if settings.get("path") else "no file"
            self._jump(0.0, "playing" if self.has_video or settings.get("play_on_start") else "stopped")
        elif reopen or clamp_speed(settings.get("speed", 1.0)) != self._speed:
            with self._lock:  # decode again from here: new speed (its sound is resampled for it) or decoder
                if self._state in ("playing", "paused"):
                    paused_by_hide = self._paused_by_hide
                    self._jump_locked(self._position(), self._state)
                    self._paused_by_hide = paused_by_hide
                else:
                    self._speed = clamp_speed(settings.get("speed", 1.0))

    def set_hidden(self, hidden: bool) -> None:
        """Its eye went off (pause) or on again (start over, or carry on). Any thread."""
        if not self.has_video:
            return
        with self._lock:
            if hidden == self._hidden:
                return
            self._hidden = hidden
            if hidden:
                if self._state == "playing":
                    self._base = self._position()
                    self._pause_clock()
                    self._state = "paused"
                    self._paused_by_hide = True
            elif self.settings.get("restart_on_show", True):
                self._jump_locked(0.0, "playing")
            elif self._paused_by_hide:
                self._paused_by_hide = False
                self._state = "playing"
                self._wake.notify_all()

    # Controls (any thread) -----------------------------------------------------------------------

    def play(self) -> None:
        with self._lock:
            self._paused_by_hide = False
            if self._state in ("ended", "stopped"):
                self._jump_locked(0.0, "playing")
            elif self._state == "paused":
                self._state = "playing"
                self._wake.notify_all()

    def pause(self) -> None:
        with self._lock:
            self._paused_by_hide = False
            if self._state == "playing":
                self._base = self._position()
                self._pause_clock()
                self._state = "paused"

    def toggle(self) -> None:
        if self.state == "playing":
            self.pause()
        else:
            self.play()

    def restart(self) -> None:
        self._jump(0.0, "playing")

    def stop_playing(self) -> None:
        """Stop and show nothing; play starts from the beginning."""
        self._jump(0.0, "stopped")

    def seek(self, seconds: float) -> None:
        with self._lock:
            state = "paused" if self._state == "paused" else "playing"
            limit = self.duration if self.duration else seconds
            paused_by_hide = self._paused_by_hide
            self._jump_locked(min(max(0.0, seconds), max(0.0, limit - 0.05)), state)
            self._paused_by_hide = paused_by_hide

    @property
    def state(self) -> str:
        with self._lock:
            self._check_end()
            return self._state

    @property
    def position(self) -> float:
        with self._lock:
            p = self._position()
        return p % self.duration if self.duration and self.settings.get("loop") else p

    def info(self) -> dict[str, Any]:
        return {"state": self.state, "position": round(self.position, 3),
                "duration": round(self.duration, 3) if self.duration else None,
                "loop": bool(self.settings.get("loop")), "video": self.has_video, "status": self.status,
                "speed": self._speed, "hidden": self._hidden}

    # Clock (call with the lock held) ---------------------------------------------------------------

    def _position(self) -> float:
        if self._anchor is None:
            return self._base
        return self._base + (time.monotonic() - self._anchor) * self._speed

    def _pause_clock(self) -> None:
        self._anchor = None

    def _run_clock(self) -> None:
        """Start the clock once the first picture or sound is decoded, so nothing at the start is lost."""
        if self._state == "playing" and self._anchor is None and (self._frames or self._audio):
            self._anchor = time.monotonic()

    def _jump(self, seconds: float, state: str) -> None:
        with self._lock:
            self._jump_locked(seconds, state)

    def _jump_locked(self, seconds: float, state: str) -> None:
        self._generation += 1
        self._start_at = seconds
        self._base = seconds
        self._anchor = None
        self._state = state
        self._paused_by_hide = False
        self._speed = clamp_speed(self.settings.get("speed", 1.0))
        self._finished_at = None
        self._frames.clear()
        self._audio.clear()
        self._audio_s = 0.0
        self._shown = None
        if state == "stopped" and self.has_video:
            self._blank = np.zeros((2, 2, 4), dtype=np.uint8)
        self._wake.notify_all()

    def _check_end(self) -> None:
        if self._state == "playing" and self._finished_at is not None and self._position() >= self._finished_at \
                and not self._frames and not self._audio:
            self._base = self._finished_at
            self._anchor = None
            self._state = "ended"
            if self.settings.get("clear_on_end") and self.has_video:
                self._blank = np.zeros((2, 2, 4), dtype=np.uint8)

    # Render thread -----------------------------------------------------------------------------------

    def video_frame(self) -> np.ndarray | None:
        with self._lock:
            if self._blank is not None:
                blank, self._blank = self._blank, None
                self.channel_order = "RGBA"
                return blank
            if self._state != "playing" and self._shown is not None:
                return None
            self._run_clock()
            now = self._position()
            while len(self._frames) >= 2 and self._frames[1][0] <= now:
                self._frames.popleft()
                self._wake.notify_all()
            if self._frames and self._frames[0][0] <= now + 0.005 or (self._shown is None and self._frames):
                t, picture, order = self._frames[0]
                if len(self._frames) == 1:
                    self._frames.popleft()  # the last one decoded: hand it over and make room
                    self._wake.notify_all()
                if t != self._shown:
                    self._shown = t
                    self.channel_order = order
                    return picture
            self._check_end()
            return None

    # Mixer thread -------------------------------------------------------------------------------------

    def read_block(self, count: int) -> np.ndarray:
        out = np.zeros((count, 2), dtype=np.float32)
        with self._lock:
            if self._state != "playing" or self.asleep:
                return out
            self._run_clock()
            now = self._position()
            step = self._speed / SAMPLE_RATE  # timeline seconds per sample (sound is resampled for the speed)
            filled = 0
            while self._audio and filled < count:
                start, chunk = self._audio[0]
                cursor = now + filled * step
                if start + len(chunk) * step < cursor - SYNC_S:  # fell behind the clock: skip it
                    self._drop_audio()
                    continue
                if start > cursor + SYNC_S:  # ahead of the clock: silence until it's due
                    break
                take = min(count - filled, len(chunk))
                out[filled:filled + take] = chunk[:take]
                filled += take
                if take == len(chunk):
                    self._drop_audio()
                else:
                    self._audio[0] = (start + take * step, chunk[take:])
                    self._audio_s -= take / SAMPLE_RATE
            if not self.has_video:
                self._check_end()
        return out

    def _drop_audio(self) -> None:
        _, chunk = self._audio.popleft()
        self._audio_s -= len(chunk) / SAMPLE_RATE
        self._wake.notify_all()

    # Decoder thread -------------------------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set() and self._mine():
            path = str(self.settings.get("path", ""))
            with self._lock:
                generation, start_at, speed = self._generation, self._start_at, self._speed
            if not path:
                self.status = "no file"
                self._wait_for_change(generation)
                continue
            try:
                self._decode(path, generation, start_at, speed)
            except Exception as exc:
                log.warning("media %s could not be played: %s", path, exc)
                started = self.status == "ok"
                self.status = f"can't play: {exc}"[:80]
                if started:  # it broke partway: end there, never start over by itself (only play or loop do)
                    with self._lock:
                        if self._generation == generation:
                            self._finished_at = self._position()
                    self._wait_for_change(generation)
                else:  # couldn't open it: try again in a bit (a file still copying, a drive waking up)
                    self._wait_for_change(generation, timeout=5.0)

    def _wait_for_change(self, generation: int, timeout: float | None = None) -> None:
        with self._wake:
            self._wake.wait_for(lambda: self._stop.is_set() or self._generation != generation or not self._mine(),
                                timeout)

    def _current(self, generation: int) -> bool:
        return not self._stop.is_set() and self._generation == generation and self._mine()

    def _open(self, path: str):
        import av

        if self.settings.get("hw_decode") and not is_sound_file(path):
            try:
                hw = _hw_accel()
                if hw is not None:
                    return av.open(path, hwaccel=hw)
            except Exception as exc:  # no such device on this PC, an old driver...: decode on the CPU
                log.info("media %s: hardware decoding unavailable (%s)", path, exc)
        return av.open(path)

    def _decode(self, path: str, generation: int, start_at: float, speed: float = 1.0) -> None:
        import av

        if not Path(path).exists():
            raise FileNotFoundError("file not found")
        offset = 0.0  # added to each pass's times when looping, so the timeline keeps running
        container = None  # kept open from one pass to the next: looping seeks back to the start
        decoder = None  # libvpx (see-through webm): made once, flushed for each new pass
        try:
            while self._current(generation):
                if self.state == "stopped":  # waiting for play: just learn what the file is
                    if self.duration is None:
                        with av.open(path) as probe:
                            self.has_video = bool(probe.streams.video)
                            if probe.duration is not None:
                                self.duration = max(0.04, probe.duration / 1_000_000)
                    self.status = "ok"
                    self._wait_for_change(generation)
                    return
                if container is None:
                    container = self._open(path)
                elif not _rewind(container):  # the next pass: from the top of the open file
                    container.close()
                    container = self._open(path)
                video = container.streams.video[0] if container.streams.video else None
                audio = container.streams.audio[0] if container.streams.audio else None
                if video is None and audio is None:
                    raise ValueError("no picture or sound in the file")
                self.has_video = video is not None
                if container.duration is not None:
                    self.duration = max(0.04, container.duration / 1_000_000)
                elif video is not None and video.duration is not None and video.time_base is not None:
                    self.duration = max(0.04, float(video.duration * video.time_base))
                size = (0, 0)
                if video is not None:
                    name = video.codec_context.name
                    if decoder is not None:
                        decoder.flush_buffers()  # the next pass: same decoder and threads, nothing left over
                    elif name in LIBVPX and LIBVPX[name] in av.codecs_available:
                        decoder = av.CodecContext.create(LIBVPX[name], "r")  # keeps see-through parts
                        # FFmpeg's default is a thread per CPU thread (17 on the user's Ryzen), every loop
                        decoder.thread_count = decode_threads(video.codec_context.width,
                                                              video.codec_context.height)
                    elif not video.codec_context.is_open:  # set once: later passes reuse the open decoder
                        video.thread_type = "AUTO"
                        video.codec_context.thread_count = decode_threads(video.codec_context.width,
                                                                          video.codec_context.height)
                    size = fit_size((video.codec_context.width, video.codec_context.height), MAX_SIZE)
                    self._size = size
                # Faster or slower: the sound is resampled to fit, so it plays in step (higher or lower).
                rate = max(8000, round(SAMPLE_RATE / speed))
                resampler = av.AudioResampler(format="flt", layout="stereo", rate=rate) if audio else None
                skip_to = start_at if offset == 0.0 else 0.0
                if skip_to > 0.5:
                    target = video or audio
                    container.seek(int(skip_to / target.time_base), stream=target)
                self.status = "ok"
                # Times in the file can start above 0 (MPEG-TS, some MP4s): count from the file's start.
                origin = container.start_time / 1_000_000 if container.start_time is not None else 0.0
                streams = [s for s in (video, audio) if s is not None]
                for packet in container.demux(*streams):
                    if not self._current(generation):
                        return
                    if packet.stream is audio:
                        for frame in packet.decode():
                            t = float(frame.pts * frame.time_base) if frame.pts is not None else None
                            for out in resampler.resample(frame):
                                if t is None:
                                    continue
                                samples = owned(out.to_ndarray().reshape(-1, 2).astype(np.float32, copy=False))
                                at = t - origin
                                if at + len(samples) * speed / SAMPLE_RATE < skip_to:
                                    continue
                                if not self._push_audio(offset + at, samples, generation):
                                    return
                        continue
                    if decoder is not None and packet.size == 0:
                        continue  # demux's empty end-of-file packet: our own decoder is flushed once below
                    frames = decoder.decode(packet) if decoder is not None else packet.decode()
                    for frame in frames:
                        pts = frame.pts if frame.pts is not None else packet.pts
                        t = float(pts * video.time_base) if pts is not None else 0.0
                        at = t - origin
                        if at < skip_to - 0.001:
                            continue
                        if not self._push_frame(offset + at, _picture(frame, size), generation):
                            return
                if decoder is not None:
                    try:
                        tail = decoder.decode(None)  # flush: the last pictures libvpx was holding back
                    except EOFError:  # already flushed
                        tail = []
                    for frame in tail:
                        at = float((frame.pts or 0) * video.time_base) - origin
                        self._push_frame(offset + at, _picture(frame, size), generation)
                length = self.duration or 0.0
                if not self.settings.get("loop") or length <= 0.05:
                    with self._lock:
                        if self._generation == generation:
                            self._finished_at = offset + length
                    self._wait_for_change(generation)
                    return
                offset += length
        finally:
            # Only this thread ever touches its decoder and file, and it lets them go here, decoder first, after
            # its last frame is decoded: nothing frees them while FFmpeg is still working on them. Pictures it
            # handed over are copies of their own (core/frames.owned), so none points back into the decoder.
            decoder = None
            if container is not None:
                container.close()

    def _push_frame(self, t: float, picture: tuple[np.ndarray, str], generation: int) -> bool:
        pixels, order = picture
        with self._wake:
            self._wake.wait_for(lambda: not self._current(generation) or len(self._frames) < AHEAD_FRAMES)
            if not self._current(generation):
                return False
            self._frames.append((t, np.ascontiguousarray(pixels), order))
        return True

    def _push_audio(self, t: float, samples: np.ndarray, generation: int) -> bool:
        with self._wake:
            self._wake.wait_for(lambda: not self._current(generation) or self._audio_s < AHEAD_AUDIO_S
                                or (self.has_video and len(self._frames) < 2))
            if not self._current(generation):
                return False
            self._audio.append((t, samples))
            self._audio_s += len(samples) / SAMPLE_RATE
        return True
