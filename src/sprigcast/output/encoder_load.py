"""Is the encoder keeping up? And the one tap that makes encoding lighter when it isn't.

A slow upload and an overloaded encoder both drop frames, but the fix is different: a lower bitrate for
the first, a faster encoder preset for the second. EncoderWatch looks at the encoders themselves (how long
a frame takes against the frame budget, how full their queue is, a recording easing off to half rate), so
Garden check can say which one it is and offer the right fix.
"""

from __future__ import annotations

import threading
import time
from collections import deque

WINDOW_S = 10.0  # judged over about the last ten seconds
BUSY_SHARE = 0.85  # encoding a frame takes more than this share of the frame's time
FULL_QUEUE = 0.5  # the encoder's queue is more than half full
FULL_SAMPLES = 3  # ... in this many samples of the window

X264_FASTER = ["placebo", "veryslow", "slower", "slow", "medium", "fast", "faster", "veryfast", "superfast",
               "ultrafast"]


def _encoders(streamers, recording, replay) -> list[tuple[str, object]]:
    """(kind, encoder) for every encoder running now, each once. kind is "stream", "recording" or "replay
    buffer" (the Clip button's: it said "recording" while nothing was recording, PC stream 2026-11-17)."""
    found, seen = [], set()

    def add(kind, enc):
        if enc is not None and id(enc) not in seen and hasattr(enc, "encode_seconds"):
            seen.add(id(enc))
            found.append((kind, enc))

    for _, st in streamers or []:
        add("stream", getattr(st, "shared", None) or st)
    for _, enc in getattr(recording, "recorders", None) or []:
        add("stream" if hasattr(enc, "links") else "recording", enc)
    for enc in getattr(replay, "buffers", None) or []:
        add("replay buffer", enc)
    return found


def _queue_fill(enc) -> float:
    q = getattr(enc, "_video", None)
    try:
        return q.qsize() / max(1, q.maxsize)
    except Exception:
        return 0.0


class EncoderWatch:
    """Call tick() every second or two; overloaded() says which kinds of encoder can't keep up.

    tick() runs on a worker and overloaded() on the window's thread: both hold the lock, and overloaded() judges a
    copy, so a sample added meanwhile can't change what it's reading ("deque mutated during iteration" in the
    status bar on the PC, 2026-11-18)."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.Lock()
        self._samples: dict[int, deque] = {}  # id(encoder) -> (time, frames, encode s, eased, queue fill)
        self._kinds: dict[int, str] = {}
        self.codecs: dict[str, str] = {}  # kind -> the codec its encoder runs (what make_lighter() changes)

    def tick(self, streamers=None, recording=None, replay=None) -> None:
        now = self.clock()
        live = _encoders(streamers, recording, replay)
        samples = [(kind, enc, (now, int(getattr(enc, "frames", 0)), float(getattr(enc, "encode_seconds", 0.0)),
                                int(getattr(enc, "eased", 0)), _queue_fill(enc), int(getattr(enc, "fps", 30) or 30)))
                   for kind, enc in live]
        with self._lock:
            self._add(now, live, samples)

    def _add(self, now: float, live, samples) -> None:
        keep = {id(enc) for _, enc in live}
        if not live:
            self.codecs.clear()
        for gone in [k for k in self._samples if k not in keep]:
            self._samples.pop(gone)
            self._kinds.pop(gone, None)
        for kind, enc, sample in samples:
            line = self._samples.setdefault(id(enc), deque())
            self._kinds[id(enc)] = kind
            self.codecs.setdefault(kind, str(getattr(enc, "codec", "") or ""))
            line.append(sample)
            while line and now - line[0][0] > WINDOW_S:
                line.popleft()

    def overloaded(self) -> list[str]:
        """The kinds ("stream", "recording", "replay buffer") whose encoder couldn't keep up lately, stream
        first."""
        with self._lock:
            lines = [(self._kinds.get(key, "recording"), list(line)) for key, line in self._samples.items()]
        kinds = set()
        for kind, line in lines:
            if len(line) < 3 or line[-1][0] - line[0][0] < WINDOW_S / 2:
                continue  # not long enough to judge
            first, last = line[0], line[-1]
            frames = last[1] - first[1]
            per_frame = (last[2] - first[2]) / frames if frames > 0 else 0.0
            busy = frames > 0 and per_frame > BUSY_SHARE / last[5]
            eased = last[3] > first[3]
            full = sum(1 for s in line if s[4] > FULL_QUEUE) >= FULL_SAMPLES
            if busy or eased or full:
                kinds.add(kind)
        return [k for k in ("stream", "recording", "replay buffer") if k in kinds]


def lighter(enc, codec: str = "") -> dict:
    """The changes that make these encoder settings one clear step lighter; {} when they're already lightest.

    The CPU encoder (x264, codec "libx264") goes one preset faster. Graphics-card encoders go two presets
    faster (p5 to p3) with no look-ahead, B-frames or extra passes.
    """
    if codec == "libx264":
        speeds = X264_FASTER
        here = speeds.index(enc.x264_preset) if enc.x264_preset in speeds else speeds.index("veryfast")
        wanted: dict = {"x264_preset": speeds[min(len(speeds) - 1, here + 1)]}
    else:
        number = int(str(enc.preset)[1:] or 5)
        wanted = {"lookahead": False, "b_frames": 0, "multipass": "disabled", "preset": f"p{max(1, number - 2)}"}
    return {k: v for k, v in wanted.items() if getattr(enc, k) != v}


def make_lighter(collection, kinds: list[str], codecs: dict | None = None) -> list[str]:
    """Apply lighter() to the stream and/or recording encoder settings. Returns the kinds that changed."""
    done, seen = [], set()
    for kind in kinds:
        # The replay buffer encodes with the recording's settings: one step lighter for both, not two.
        enc = collection.stream_encoder if kind == "stream" else collection.recording.encoder
        if id(enc) in seen:
            continue
        seen.add(id(enc))
        changes = lighter(enc, (codecs or {}).get(kind, ""))
        for key, value in changes.items():
            setattr(enc, key, value)
        if changes:
            done.append(kind)
    return done
