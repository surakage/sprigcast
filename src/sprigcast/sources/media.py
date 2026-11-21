"""Media: plays a video or sound file (MP4, MOV, MKV, WebM, MP3, WAV, OGG, FLAC...).

A video file is placed in a bloom like a picture: it plays while its bloom is on screen, and its sound
goes to the mixer. A sound file has no picture, so it lives in Sound like a mic and plays in every bloom.

A small thread decodes a little ahead (a few frames, about a second of sound) and the render and mixer
threads take what's due on the media's own clock, so picture and sound stay together. Paused, the
thread waits. Nothing is kept running for nothing (2026-11-16, the user's rule):
- asleep (its bloom went off air), every frame is let go and the thread closes its file and decoder and ends;
- once it has played to the end (not looping), the thread closes them and ends too. The canvas keeps the last
  picture (it's a texture there), and play, restart or a seek start a new thread.
A bloom switch waits for its first picture (slow_wake, see Engine._wait_for_waking), so nothing pops in late.

Controls (play, pause, restart, stop, seek) are safe to call from any thread: the control API, hotkeys
and the right-click menu call them directly.

Hidden with its eye (still in a bloom on screen), a video pauses; shown again it starts over or carries on
(restart_on_show). The renderer tells it with set_hidden() (Engine._show_hidden).

With the native engine (SPRIGCAST_ENGINE=native, docs/native-engine.md phase 3) the engine plays the file itself:
native/core/src/media.cpp is this player moved to C++ method for method, on a thread that never needs Python's
lock. This class then only passes the controls on and hands the renderer and mixer what the engine has ready
(self._native), decoding on the graphics card too when hw_decode asks.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast import sprigcore
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
LOOP_KEEP_BYTES = 48 * 1024 * 1024  # a silent looping clip (a GIF) whose pictures fit in this is decoded once
MAX_SIZE = (1920, 1920)  # bigger videos are shrunk while decoding: no canvas needs more
STATES = ("playing", "paused", "stopped", "ended")
SPEEDS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
PARK_S = 0.0  # asleep, the decoder thread closes its file and decoder and ends right away (a wake that comes
# before it noticed reuses them). Was 60 s (#361); the user wants nothing off air kept open.
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


class _Held:
    """The open file and libvpx decoder of one decoder thread, kept from one play to the next: a wake, seek or
    restart reuses them instead of opening the file and starting FFmpeg's decoder threads again. Only that
    thread touches them, and it lets them go itself (release)."""

    __slots__ = ("key", "container", "decoder")

    def __init__(self):
        self.key = None
        self.container = None
        self.decoder = None

    def release(self) -> None:
        container, self.container, self.decoder, self.key = self.container, None, None, None
        if container is None:
            return
        container.close()
        # Here (the decoder thread) the file's last reference should be the local above. Anything else holding it
        # keeps FFmpeg's decoder threads alive until Python's cycle collector finds it: say what, once.
        extra = sys.getrefcount(container) - 2
        global _held_logged
        if extra > 0 and not _held_logged:
            _held_logged = True
            import gc

            kinds = sorted({type(r).__name__ for r in gc.get_referrers(container)})
            log.info("media: a closed file is still held %d more time(s), by %s", extra, ", ".join(kinds))


_held_logged = False


def sound_resampler(rate: int = SAMPLE_RATE):
    """PyAV's AudioResampler to stereo float at `rate`, on this thread alone. Left to choose, FFmpeg's filter
    graph inside it starts a pool of threads (15 on the user's PC) that only goes when Python's cycle collector
    frees the resampler: every loop, wake or seek of a video with sound added 15 threads (2026-11-16)."""
    import av

    return av.AudioResampler(format="flt", layout="stereo", rate=rate, threads=1)


def shrunk(size: tuple[int, int], shrink: float) -> tuple[int, int]:
    """The picture size for a video shown at `shrink` of its size (1, 1/2 or 1/4), even on both sides."""
    if shrink >= 1.0:
        return size
    return max(2, math.ceil(size[0] * shrink / 2) * 2), max(2, math.ceil(size[1] * shrink / 2) * 2)


def _picture(frame, size: tuple[int, int], out: tuple[int, int] | None = None) -> tuple[np.ndarray, str]:
    """The picture to show for a decoded frame, `size` its full size. `out`, when smaller: shown small on every
    canvas, it's made that size here, on the decoder thread (FFmpeg's area filter, without Python's lock), so
    the render thread sends the card a quarter or a sixteenth of the pixels. The 2026-11-17 heavy run showed
    two 1080p videos at 0.3 of their size: 5-6 ms a frame each, mostly sending pixels nobody saw."""
    if out is not None and tuple(out) != tuple(size):
        kind = {"yuv420p": "yuv420p", "yuvj420p": "yuvj420p", "yuva420p": "yuva420p", "nv12": "yuv420p"}.get(
            frame.format.name)
        if kind is not None:
            from sprigcast.core.frames import reformat

            small = reformat(frame, width=out[0], height=out[1], format=kind, interpolation="AREA")
            packed = pack_i420(small, out)
            if packed is not None:
                return packed
        from sprigcast.core.frames import picture_array

        return picture_array(frame, width=out[0], height=out[1], format="rgba", interpolation="AREA"), "RGBA"
    packed = pack_i420(frame, size)
    if packed is not None:
        return packed
    from sprigcast.core.frames import picture_array

    return picture_array(frame, width=size[0], height=size[1], format="rgba"), "RGBA"


def is_sound_file(path: str) -> bool:
    return Path(str(path)).suffix.lower() in AUDIO_EXTS


def file_filter(only: str | None = None) -> str:
    """The file picker's types: both kinds, or only "video" or only "sound" (+ Add → Video file / Sound file)."""
    video = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
    audio = " ".join(f"*{e}" for e in sorted(AUDIO_EXTS))
    if only == "video":
        return f"Video ({video})"
    if only == "sound":
        return f"Sound ({audio})"
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
        self.shrink = 1.0  # shown at most this part of its size anywhere (1, 1/2, 1/4): decoded that small
        self.duration: float | None = None
        self.has_video = not is_sound_file(settings.get("path", ""))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._parked = False  # asleep: the decoder thread waits with its file open (PARK_S), decoding nothing
        self.status = "starting" if settings.get("path") else "no file"
        if not self.has_video and not settings.get("play_on_start"):
            self._state = "stopped"
        self._native = self._open_native()  # the engine's own player (sprigcore.MediaPlayer), or None: this one
        self._fault_logged = False

    # The native engine's player ----------------------------------------------------------------------

    def _open_native(self):
        if not sprigcore.use_native_media():
            return None
        try:
            player = sprigcore.MediaPlayer(playing=self._state == "playing")
            self._send_options(player)
            player.set_decode(bool(self.settings.get("hw_decode")))
            player.set_file(str(self.settings.get("path", "")), self.has_video, self._state == "playing")
            return player
        except Exception as exc:  # noqa: BLE001 (any refusal: the Python player plays it)
            log.warning("media %s: the engine's player isn't available (%s); playing it in Python", self.id, exc)
            return None

    def _send_options(self, player) -> None:
        s = self.settings
        player.set_options(bool(s.get("loop")), bool(s.get("clear_on_end")), bool(s.get("restart_on_show", True)),
                           clamp_speed(s.get("speed", 1.0)))

    def _control(self, what: int, value: float = 0.0, ensure: bool = False) -> None:
        """A control passed to the engine's player; `ensure` then starts its decoder thread if it has ended."""
        try:
            self._native.control(what, value)
            if ensure and not self.asleep:
                self._native.control(sprigcore.MEDIA_START, 1)
            self._sync()
        except sprigcore.NativeError as exc:
            log.warning("media %s: %s", self.id, exc)

    @property
    def engine_player(self):
        """The engine's player of a file with a picture: the native engine's compositor takes its pictures from
        it directly (render/engine.py), and the render thread only calls :meth:`engine_tick`."""
        return self._native if self.has_video else None

    def engine_tick(self) -> None:
        """Render thread, on a tick the engine showed the picture itself: keep status and size up to date."""
        self._sync()

    def _sync(self):
        """What the engine's player knows, copied to where the rest of SprigCast reads it."""
        info = self._native.info()
        self.has_video = bool(info.has_video)
        self.duration = info.duration if info.duration > 0 else None
        self._size = (info.width, info.height)
        self.status = info.status_text
        self._speed = info.speed
        self._hidden = bool(info.hidden)
        self._state_seen = (time.monotonic(), info.state_name)
        if info.faulted and not self._fault_logged:
            self._fault_logged = True
            log.error("media %s: the engine's decoder faulted on %s; the file is given up, SprigCast goes on",
                      self.id, self.settings.get("path"))
        return info

    # A video sleeps while its bloom is off screen; a sound file belongs to Sound and keeps playing.
    @property
    def sleeps(self) -> bool:  # type: ignore[override]
        return self.has_video

    @property
    def slow_wake(self) -> bool:
        """A bloom switch to it waits (briefly) for its first picture, as the file opens again."""
        return self.has_video and not str(self.status).startswith("can't") and self.status != "no file"

    def _ensure_thread(self) -> None:
        """After a control: start a decoder thread if the last one ended (it does once the file played out)."""
        if self._native is not None:
            if not self.asleep:
                self._control(sprigcore.MEDIA_START, 1)
            return
        if self.asleep or self._parked:
            return  # wake starts one
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive() and not self._stop.is_set()
        if not alive:
            self.start()

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    shrinks = True  # the renderer says how small it's shown (set_shrink); `size` stays the layout box

    def set_shrink(self, shrink: float) -> None:
        """Render thread: shown at most `shrink` of its size; the next pictures decoded are made that small."""
        self.shrink = shrink
        if self._native is not None:
            self._native.set_shrink(shrink)

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
        if self._native is not None:
            return self._control(sprigcore.MEDIA_START, 0)
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
        if self._native is not None:
            return self._control(sprigcore.MEDIA_CLOSE)
        self._parked = False
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
        """Let every frame go. The decoder thread keeps its file and decoder open for PARK_S (a bloom that
        comes back soon reuses them: no new FFmpeg threads each time), then closes them and ends by itself.
        Nothing waits for it here."""
        if self._native is not None:
            return self._control(sprigcore.MEDIA_SLEEP)
        with self._lock:
            position = self._position()
            self._pause_clock()
            self._base = position
            self._parked = True
            self._generation += 1  # the decoder stops where it is
            self._frames.clear()
            self._audio.clear()
            self._audio_s = 0.0
            self._shown = None
            self._wake.notify_all()

    def wake(self) -> None:
        if self._native is not None:
            return self._control(sprigcore.MEDIA_WAKE)
        with self._lock:
            resume = self._paused_by_hide  # its eye was off when it went to sleep: it plays again when shown
            self._hidden = self._paused_by_hide = False  # the renderer says again if its eye is off
            self._parked = False
            if self.settings.get("restart_on_show", True):
                self._jump_locked(0.0, "playing")
            elif self._state == "ended":
                if self.settings.get("clear_on_end") or not self.duration:
                    self._blank = np.zeros((2, 2, 4), dtype=np.uint8)  # nothing, as when it ended
                    return
                self._jump_locked(max(0.0, self.duration - 0.1), "playing")  # its last picture, then ended again
            else:
                state = "playing" if resume else self._state
                self._jump_locked(self._base % self.duration if self.duration else self._base, state)
        self.start()

    def update(self, settings: dict[str, Any]) -> None:
        new_file = settings.get("path") != self.settings.get("path")
        reopen = bool(settings.get("hw_decode")) != bool(self.settings.get("hw_decode"))
        super().update(settings)
        if self._native is not None and reopen:
            try:  # on the graphics card or off it: the file opens again (from its start) with the other decoder
                self._native.set_decode(bool(settings.get("hw_decode")))
            except sprigcore.NativeError as exc:
                log.warning("media %s: %s", self.id, exc)
            new_file = True
        if self._native is not None:
            try:
                self._send_options(self._native)
                if new_file:
                    self.has_video = not is_sound_file(settings.get("path", ""))
                    self._native.set_file(str(settings.get("path", "")), self.has_video,
                                          bool(self.has_video or settings.get("play_on_start")))
                    self._control(sprigcore.MEDIA_START, 1)
                elif clamp_speed(settings.get("speed", 1.0)) != self._speed:
                    self._control(sprigcore.MEDIA_RESPEED, ensure=True)
            except sprigcore.NativeError as exc:
                log.warning("media %s: %s", self.id, exc)
            return
        if new_file:
            self.has_video = not is_sound_file(settings.get("path", ""))
            self.duration = None
            self._size = (0, 0)
            self.status = "starting" if settings.get("path") else "no file"
            self._jump(0.0, "playing" if self.has_video or settings.get("play_on_start") else "stopped")
            self._ensure_thread()
        elif reopen or clamp_speed(settings.get("speed", 1.0)) != self._speed:
            with self._lock:  # decode again from here: new speed (its sound is resampled for it) or decoder
                if self._state in ("playing", "paused"):
                    paused_by_hide = self._paused_by_hide
                    self._jump_locked(self._position(), self._state)
                    self._paused_by_hide = paused_by_hide
                else:
                    self._speed = clamp_speed(settings.get("speed", 1.0))
            self._ensure_thread()

    @property
    def follows_air(self) -> bool:
        """A sound file: told hidden or not by whether a bloom it's placed in is on air (render/engine.py)."""
        return not self.has_video

    def set_hidden(self, hidden: bool) -> None:
        """Its eye went off (pause) or on again (start over, or carry on). Any thread."""
        if not self.has_video:  # a placed sound file: its bloom went on air (play) or off (pause)
            if hidden:
                if self.state == "playing":
                    self.pause()
            elif self.settings.get("restart_on_show", True):
                self.restart()
            else:
                self.play()
            return
        if self._native is not None:
            return self._control(sprigcore.MEDIA_HIDE if hidden else sprigcore.MEDIA_SHOW, ensure=not hidden)
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
        if not hidden:
            self._ensure_thread()

    # Controls (any thread) -----------------------------------------------------------------------

    def play(self) -> None:
        if self._native is not None:
            return self._control(sprigcore.MEDIA_PLAY, ensure=True)
        with self._lock:
            self._paused_by_hide = False
            if self._state in ("ended", "stopped"):
                self._jump_locked(0.0, "playing")
            elif self._state == "paused":
                self._state = "playing"
                self._wake.notify_all()
        self._ensure_thread()

    def pause(self) -> None:
        if self._native is not None:
            return self._control(sprigcore.MEDIA_PAUSE)
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
        if self._native is not None:
            return self._control(sprigcore.MEDIA_RESTART, ensure=True)
        self._jump(0.0, "playing")
        self._ensure_thread()

    def stop_playing(self) -> None:
        """Stop and show nothing; play starts from the beginning."""
        if self._native is not None:
            return self._control(sprigcore.MEDIA_STOP)
        self._jump(0.0, "stopped")

    def seek(self, seconds: float) -> None:
        if self._native is not None:
            return self._control(sprigcore.MEDIA_SEEK, float(seconds), ensure=True)
        with self._lock:
            state = "paused" if self._state == "paused" else "playing"
            limit = self.duration if self.duration else seconds
            paused_by_hide = self._paused_by_hide
            self._jump_locked(min(max(0.0, seconds), max(0.0, limit - 0.05)), state)
            self._paused_by_hide = paused_by_hide
        self._ensure_thread()

    @property
    def state(self) -> str:
        if self._native is not None:
            return self._sync().state_name
        with self._lock:
            self._check_end()
            return self._state

    STATE_FRESH_S = 1.0

    def recent_state(self) -> str:
        """The state as last read from the engine's player, when that was under STATE_FRESH_S ago (the render thread
        reads it every tick a video is on screen): the Control API's half-second watch asked the player itself from
        the window's thread, and waited 1.2 s there while the PC was at 100% (2026-11-21)."""
        at, state = getattr(self, "_state_seen", (0.0, ""))
        if self._native is not None and state and time.monotonic() - at < self.STATE_FRESH_S:
            return state
        return self.state

    @property
    def position(self) -> float:
        if self._native is not None:
            return self._sync().position
        with self._lock:
            p = self._position()
        return p % self.duration if self.duration and self.settings.get("loop") else p

    def info(self) -> dict[str, Any]:
        if self._native is not None:  # one look at the engine's player, not one for the state and one for the place
            now = self._sync()
            state, position = now.state_name, now.position
        else:
            state, position = self.state, self.position
        return {"state": state, "position": round(position, 3),
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
        if self._native is not None:
            got = self._native.video_frame()
            self._sync()
            if got is None:
                return None
            if isinstance(got, str):  # "blank": show nothing from now on
                self.channel_order = "RGBA"
                return np.zeros((2, 2, 4), dtype=np.uint8)
            self.channel_order = got[1]
            return got[0]
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

    def native_sound(self):
        """The engine's player, for the engine's sound mixer to read straight from (audio/native_mixer.py)."""
        return self._native

    def sound_kept(self) -> None:
        """Once a block while the engine's mixer reads the player itself: what read_block would keep current."""
        self._blocks = (getattr(self, "_blocks", 0) + 1) % 25
        if not self._blocks:  # a sound file has no picture to ask for: keep its state and status current
            self._sync()

    def read_block(self, count: int) -> np.ndarray:
        if self._native is not None:
            self._blocks = (getattr(self, "_blocks", 0) + 1) % 25
            if not self._blocks:  # a sound file has no picture to ask for: keep its state and status current
                self._sync()
            return self._native.read_audio(count, self.asleep)
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
        held = _Held()
        try:
            while not self._stop.is_set() and self._mine():
                if self._parked and not self._park(held):
                    return
                generation = self._generation
                if self._play_once(held):
                    with self._wake:  # played to the end (or waiting for play): let everything go, unless a
                        if self._generation == generation and self._mine():  # control just asked for more
                            self._thread = None
                            return
        finally:
            held.release()  # on this thread, after its last frame: nothing else ever touches them

    def _park(self, held: _Held) -> bool:
        """Asleep: wait with the file open. False once PARK_S passed (this thread is done; wake starts another)."""
        with self._wake:
            woke = self._wake.wait_for(lambda: not self._parked or self._stop.is_set() or not self._mine(), PARK_S)
            if woke:
                return True
            if self._mine():
                self._thread = None  # a wake from now on starts a new thread
            return False

    def _play_once(self, held: _Held) -> bool:
        """Play from where the clock says. True when it's done (played out, or a sound file waiting for play):
        the thread then ends and lets its file go."""
        path = str(self.settings.get("path", ""))
        with self._lock:
            generation, start_at, speed = self._generation, self._start_at, self._speed
        if self._parked or generation != self._generation:
            return False
        if not path:
            self.status = "no file"
            held.release()
            self._wait_for_change(generation)
            return False
        try:
            return self._decode(path, generation, start_at, speed, held)
        except Exception as exc:
            held.release()  # broken partway: open it fresh next time
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
            return False

    def _wait_for_change(self, generation: int, timeout: float | None = None) -> None:
        with self._wake:
            self._wake.wait_for(lambda: self._stop.is_set() or self._generation != generation or not self._mine()
                                or self._parked, timeout)

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

    def _decode(self, path: str, generation: int, start_at: float, speed: float = 1.0,
                held: _Held | None = None) -> bool:
        """Decode until the end, a jump or sleep. True when it played out (or is a file waiting for play)."""
        import av

        if not Path(path).exists():
            raise FileNotFoundError("file not found")
        own = held is None
        held = held or _Held()
        key = (path, bool(self.settings.get("hw_decode")))
        if held.key != key:
            held.release()
        held.key = key
        offset = 0.0  # added to each pass's times when looping, so the timeline keeps running
        # The file stays open from one pass (and one play) to the next: it seeks back to the start instead.
        container, decoder = held.container, held.decoder  # decoder: libvpx (see-through webm), flushed each pass
        resampler = None  # faster or slower: the sound is resampled to fit, so it plays in step (higher or lower)
        # A silent looping clip small enough (LOOP_KEEP_BYTES): its first whole pass is kept and later passes play
        # from memory, with the file and decoder closed. OBS's image source does the same for GIFs: the
        # heavy run (2026-11-17) looped three GIFs, each decoded again on every pass.
        kept: list | None = None  # [(time in the pass, picture)] while the first pass is collected
        kept_bytes, kept_shrink, replay = 0, self.shrink, None
        try:
            while self._current(generation):
                if self.state == "stopped":  # waiting for play: just learn what the file is
                    if self.duration is None:
                        with av.open(path) as probe:
                            self.has_video = bool(probe.streams.video)
                            if probe.duration is not None:
                                self.duration = max(0.04, probe.duration / 1_000_000)
                    self.status = "ok"
                    return True  # play starts a thread again
                if replay is not None and kept_shrink == self.shrink and self.settings.get("loop"):
                    for at, picture in replay:  # from memory: nothing opened, nothing decoded
                        if not self._push_frame(offset + at, picture, generation):
                            return False
                    offset += self.duration or 0.0
                    continue
                replay = None  # shown at another size now (or loop turned off): decode as before
                if container is None:
                    container = held.container = self._open(path)
                elif not _rewind(container):  # the next pass: from the top of the open file
                    held.decoder = decoder = None
                    video = audio = streams = packet = frames = target = None  # each holds the file open
                    held.release()
                    held.key = key
                    container = held.container = self._open(path)
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
                        if decoder.is_open:
                            decoder.flush_buffers()  # the next pass: same decoder and threads, nothing left over
                    elif name in LIBVPX and LIBVPX[name] in av.codecs_available:
                        decoder = held.decoder = av.CodecContext.create(LIBVPX[name], "r")  # keeps see-through
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
                if audio is not None and resampler is None:  # one per play: a loop's next pass carries on with it
                    resampler = sound_resampler(max(8000, round(SAMPLE_RATE / speed)))
                skip_to = start_at if offset == 0.0 else 0.0
                kept, kept_bytes, kept_shrink = None, 0, self.shrink
                if (audio is None and video is not None and skip_to <= 0.0 and self.settings.get("loop")
                        and (self.duration or 0.0) > 0.05):
                    kept = []
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
                        shrink = self.shrink  # a new size shows from the next picture on
                        picture = _picture(frame, size, shrunk(size, shrink))
                        if kept is not None and shrink != kept_shrink:
                            kept = None  # pictures of two sizes: this pass isn't kept
                        if kept is not None:
                            kept_bytes += picture[0].nbytes
                            kept = kept if kept_bytes <= LOOP_KEEP_BYTES else None  # too big: decoded each pass
                            if kept is not None:
                                kept.append((at, picture))
                        if not self._push_frame(offset + at, picture, generation):
                            return
                if decoder is not None:
                    try:
                        tail = decoder.decode(None)  # flush: the last pictures libvpx was holding back
                    except EOFError:  # already flushed
                        tail = []
                    for frame in tail:
                        at = float((frame.pts or 0) * video.time_base) - origin
                        picture = _picture(frame, size, shrunk(size, self.shrink))
                        if kept is not None:
                            kept = kept + [(at, picture)] if self.shrink == kept_shrink else None
                        self._push_frame(offset + at, picture, generation)
                length = self.duration or 0.0
                if not self.settings.get("loop") or length <= 0.05:
                    with self._lock:
                        if self._generation == generation:
                            self._finished_at = offset + length
                    return True  # played out: the thread closes the file and ends (_run)
                if kept:  # the whole pass was kept: later passes play from memory, the file and decoder close
                    replay, kept = kept, None
                    container = decoder = video = audio = streams = packet = frames = target = None
                    held.release()
                offset += length
            return False
        finally:
            # Only this thread ever touches its decoder and file. They stay in `held` for the next play; the
            # thread lets them go itself when it ends (_run). Pictures it handed over are copies of their own
            # (core/frames.owned), so none points back into the decoder.
            # The streams too: each holds its file, and with it FFmpeg's decoder threads, until it is let go (PC
            # 2026-11-17: "a closed file is still held 1 more time(s), by VideoStream").
            container = decoder = video = audio = streams = packet = frames = target = None
            if own:
                held.release()

    def _push_frame(self, t: float, picture: tuple[np.ndarray, str], generation: int) -> bool:
        pixels, order = picture
        # Made one piece before the lock, not while holding it: the canvas asks for its picture under the same
        # lock, and a big picture's copy made it wait (a 237 ms media frame on the PC while live, 2026-11-18).
        pixels = np.ascontiguousarray(pixels)
        with self._wake:
            self._wake.wait_for(lambda: not self._current(generation) or len(self._frames) < AHEAD_FRAMES)
            if not self._current(generation):
                return False
            self._frames.append((t, pixels, order))
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
