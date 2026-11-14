"""Plays a stinger video (a short clip, usually with see-through parts) for a transition.

A small thread decodes the file with PyAV a few frames ahead of where the transition is, and the render
thread takes the frame for the moment it is drawing. The clip's sound is handed to the mixer as a
temporary source, so it reaches the stream, recordings and your speakers. Everything is let go the
moment the transition ends: the file is closed, the thread stops and the frames are dropped.

WebM VP9 keeps its see-through parts only through libvpx's decoder (FFmpeg's own VP9 decoder drops
them), so VP8/VP9 are decoded with libvpx when it is there. MOV ProRes 4444, QuickTime Animation and
PNG keep theirs with the normal decoders.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

import numpy as np

from sprigcast.sources.audio import SAMPLE_RATE

log = logging.getLogger(__name__)

AHEAD = 3  # frames decoded ahead of the one on screen
MAX_AUDIO_S = 10  # a stinger is a few seconds; never hold more sound than this
LIBVPX = {"vp9": "libvpx-vp9", "vp8": "libvpx"}


def fit_size(video: tuple[int, int], limit: tuple[int, int]) -> tuple[int, int]:
    """The video's size shrunk (never grown) to fit inside limit, kept even for the scaler."""
    w, h = video
    scale = min(1.0, limit[0] / max(1, w), limit[1] / max(1, h))
    return max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)


def probe(path: str) -> float | None:
    """The clip's length in seconds, or None if it cannot be read. For the settings panel."""
    try:
        import av

        with av.open(path) as container:
            if not container.streams.video:
                return None
            stream = container.streams.video[0]
            if stream.duration is not None and stream.time_base is not None:
                return float(stream.duration * stream.time_base)
            if container.duration is not None:
                return container.duration / 1_000_000
    except Exception:
        return None
    return None


class StingerPlayer:
    """One stinger clip being played. Use start(), then frame_at() each tick, then close()."""

    kind = "stinger"

    def __init__(self, path: str, limit: tuple[int, int] = (3840, 3840), gain: float = 1.0):
        self.id = f"stinger:{path}"
        self.path = path
        self.limit = limit
        self.gain = gain
        self.settings = {"monitor": True}  # you hear it too, like OBS's stinger "monitor and output"
        self.duration = 1.0
        self.error: str | None = None
        self.ready = threading.Event()  # the first frame is decoded, or opening failed
        self._frames: deque[tuple[float, np.ndarray]] = deque()
        self._audio: deque[np.ndarray] = deque()
        self._audio_samples = 0
        self._playing = False
        self._shown: float | None = None
        self._lock = threading.Lock()
        self._room = threading.Condition(self._lock)
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="sprigcast-stinger", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        with self._room:
            self._stop = True
            self._room.notify_all()
            self._frames.clear()
            self._audio.clear()
            self._audio_samples = 0
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=0.5)

    @property
    def size(self) -> tuple[int, int]:
        with self._lock:
            return self._frames[0][1].shape[1::-1] if self._frames else (0, 0)

    # Render thread ---------------------------------------------------------------

    def frame_at(self, t: float) -> np.ndarray | None:
        """The frame to show t seconds into the clip if it is new since the last call, else None."""
        with self._room:
            self._playing = True
            while len(self._frames) >= 2 and self._frames[1][0] <= t:
                self._frames.popleft()
                self._room.notify_all()
            if not self._frames or self._frames[0][0] == self._shown:
                return None
            self._shown = self._frames[0][0]
            return self._frames[0][1]

    # Mixer thread ------------------------------------------------------------------

    def read_block(self, count: int) -> np.ndarray:
        out = np.zeros((count, 2), dtype=np.float32)
        with self._lock:
            if not self._playing:
                return out
            filled = 0
            while filled < count and self._audio:
                chunk = self._audio[0]
                take = min(count - filled, len(chunk))
                out[filled:filled + take] = chunk[:take]
                filled += take
                if take == len(chunk):
                    self._audio.popleft()
                else:
                    self._audio[0] = chunk[take:]
            self._audio_samples -= filled
        return out

    # Decoder thread ---------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._decode()
        except Exception as exc:
            log.warning("stinger %s could not be played: %s", self.path, exc)
            self.error = str(exc) or type(exc).__name__
        finally:
            self.ready.set()

    def _decode(self) -> None:
        import av

        with av.open(self.path) as container:
            if not container.streams.video:
                raise ValueError("no video in the file")
            video = container.streams.video[0]
            audio = container.streams.audio[0] if container.streams.audio and self.gain > 0 else None
            if video.duration is not None and video.time_base is not None:
                self.duration = max(0.1, float(video.duration * video.time_base))
            elif container.duration is not None:
                self.duration = max(0.1, container.duration / 1_000_000)
            name = video.codec_context.name
            decoder = None
            if name in LIBVPX and LIBVPX[name] in av.codecs_available:
                decoder = av.CodecContext.create(LIBVPX[name], "r")  # keeps the alpha channel
            else:
                video.thread_type = "AUTO"
            resampler = av.AudioResampler(format="flt", layout="stereo", rate=SAMPLE_RATE) if audio else None
            size = fit_size((video.codec_context.width, video.codec_context.height), self.limit)
            first = None
            streams = [video] + ([audio] if audio else [])
            for packet in container.demux(*streams):
                if self._stop:
                    return
                if packet.stream is audio:
                    for frame in packet.decode():
                        for out in resampler.resample(frame):
                            self._add_audio(out.to_ndarray().reshape(-1, 2))
                    continue
                frames = decoder.decode(packet) if decoder is not None else packet.decode()
                for frame in frames:
                    pts = frame.pts if frame.pts is not None else packet.pts
                    t = float(pts * video.time_base) if pts is not None else 0.0
                    if first is None:
                        first = t
                    picture = frame.reformat(width=size[0], height=size[1], format="rgba").to_ndarray()
                    if not self._add_frame(t - first, picture):
                        return
            if decoder is not None:
                for frame in decoder.decode(None):
                    picture = frame.reformat(width=size[0], height=size[1], format="rgba").to_ndarray()
                    pts = frame.pts or 0
                    self._add_frame(float(pts * video.time_base) - (first or 0.0), picture)

    def _add_frame(self, t: float, picture: np.ndarray) -> bool:
        with self._room:
            while len(self._frames) >= AHEAD and not self._stop:
                self.ready.set()
                self._room.wait(0.25)
            if self._stop:
                return False
            self._frames.append((t, np.ascontiguousarray(picture)))
        self.ready.set()
        return True

    def _add_audio(self, samples: np.ndarray) -> None:
        with self._lock:
            if self._stop or self._audio_samples > MAX_AUDIO_S * SAMPLE_RATE:
                return
            self._audio.append(samples.astype(np.float32, copy=False))
            self._audio_samples += len(samples)
