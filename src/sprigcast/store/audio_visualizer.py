"""Audio visualizer: bars, a wave, a circle or a line that move with a sound.

Add Audio visualizer (+ → Plugins → Audio visualizer): it gets a sprig of its own along the bottom of
your bloom, in every shape. Move or resize it like anything else.

Settings: Sound (everything viewers hear, or one sound: your mic, desktop, music, an app), Style
(Bars, Wave, Circle, Line), Colour and Peak colour (the tips light up toward it), Bars (how many),
Smoothing (0 snappy to 0.95 slow), Mirror (bass in the middle, the same both ways) and Sensitivity.

It's light: it hears the sound SprigCast already mixes (no device is opened again), a small FFT
(2048 samples) runs at most 30 times a second, and it only works while it's on air. Off air, or
hidden, it stops listening; during silence, once it has settled, nothing is drawn at all.

Apps can change its look, for a redeem or a song change:
    {"do": "visualizer.set", "style": "circle", "color": "#7fb8ff"}

A SprigCast store plugin: add it from Settings → Plugins.
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

import numpy as np

from sprigcast.audio import listen
from sprigcast.control.native import ApiError, register_action
from sprigcast.core import workers
from sprigcast.core.source import Source, register

PLUGIN = {"id": "audio_visualizer", "name": "Audio visualizer", "version": 1, "adds": "Source",
          "about": "Bars, a wave, a circle or a line that move with your mic, music or the whole stream."}

KIND = "audio_visualizer"
W, H = 1280, 400  # the box; scale it on the canvas like anything else
RATE = 48_000
N = 2048  # FFT size: 23 Hz a bin, two mixer blocks
LOW, HIGH = 40.0, 16_000.0  # the bands span this
FPS = 30  # at most this many drawings a second
STYLES = (("bars", "Bars"), ("wave", "Wave"), ("circle", "Circle"), ("line", "Line"))
STILL = 0.004  # every band below this: silence, and nothing moves


def _sounds() -> list[tuple[str, str]]:
    return [(listen.MIX, "Everything (stream)"), *listen.sounds()]


def band_edges(count: int, bins: int = N // 2 + 1, rate: int = RATE) -> np.ndarray:
    """count + 1 rising bin indexes, log-spaced from LOW to HIGH (each band at least one bin wide)."""
    hz = rate / ((bins - 1) * 2)
    freqs = np.geomspace(LOW, HIGH, count + 1)
    idx = np.round(freqs / hz).astype(int)
    for i in range(1, len(idx)):  # the low bands are narrower than a bin: give each its own
        idx[i] = max(idx[i], idx[i - 1] + 1)
    return np.minimum(idx, bins - 1)


class Analyser:
    """Turns the blocks the mixer hands over into band heights (0..1) and a wave (-1..1)."""

    def __init__(self):
        self._chunks: tuple[np.ndarray, np.ndarray] = (np.zeros(N // 2, np.float32), np.zeros(N // 2, np.float32))
        self._fed = 0
        self._used = 0
        self._unheard = 0.0  # seconds since the mixer last handed a block
        self._window = np.hanning(N).astype(np.float32)
        self._edges: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        self.bands = np.zeros(0, np.float32)
        self.wave = np.zeros(0, np.float32)

    def feed(self, block: np.ndarray) -> None:
        """On the mixer thread: keep a copy of the newest block, nothing more. A copy keeps Python's lock (a
        NumPy sum lets it go and then queues for it again, holding up the sound). Swapping a tuple is safe
        without a lock; it's made mono on the drawing thread."""
        self._chunks = (self._chunks[1], block.copy())
        self._fed += 1

    def reset(self) -> None:
        self._chunks = (np.zeros(N // 2, np.float32), np.zeros(N // 2, np.float32))
        self.bands = np.zeros(0, np.float32)
        self.wave = np.zeros(0, np.float32)

    def _bands_for(self, count: int) -> tuple[np.ndarray, np.ndarray]:
        found = self._edges.get(count)
        if found is None:
            edges = band_edges(count)
            centres = np.sqrt(np.maximum(edges[:-1], 1) * np.maximum(edges[1:], 1)) * (RATE / N)
            tilt = np.clip(3.0 * np.log2(centres / 500.0), 0.0, 18.0)  # music falls ~3 dB an octave: lift the highs
            found = self._edges[count] = (edges, tilt.astype(np.float32))
        return found

    def step(self, count: int, smoothing: float, sensitivity: float, dt: float) -> tuple[np.ndarray, np.ndarray]:
        """New band heights and wave, eased from the last ones. dt: seconds since the last step."""
        a, b = self._chunks
        samples = np.concatenate([c.mean(axis=1) if c.ndim == 2 else c for c in (a, b)])[-N:].astype(np.float32)
        if len(samples) < N:
            samples = np.pad(samples, (N - len(samples), 0))
        if self._fed != self._used:
            self._used = self._fed
            self._unheard = 0.0
        else:
            self._unheard += dt
            if self._unheard > 0.25:  # the mixer stopped handing this sound over (it went): fall away
                samples = np.zeros(N, np.float32)
        gain = max(0.05, float(sensitivity))
        edges, tilt = self._bands_for(count)
        mags = np.abs(np.fft.rfft(samples * self._window)) * (4.0 / N) * gain
        peaks = np.maximum.reduceat(mags, edges[:-1])[:count]
        db = 20.0 * np.log10(peaks + 1e-9) + tilt
        target = np.clip((db + 60.0) / 60.0, 0.0, 1.0).astype(np.float32)
        wave = np.clip(samples[-1024:].reshape(256, -1).mean(axis=1) * gain * 2.0, -1.0, 1.0).astype(np.float32)
        if len(self.bands) != count:
            self.bands = np.zeros(count, np.float32)
        if len(self.wave) != len(wave):
            self.wave = np.zeros(len(wave), np.float32)
        hold = float(np.clip(smoothing, 0.0, 0.95)) ** max(dt * FPS, 0.0)  # the same feel at any frame rate
        rising = target > self.bands
        self.bands = np.where(rising, self.bands + (target - self.bands) * 0.7,  # rise quickly
                              target + (self.bands - target) * hold).astype(np.float32)
        self.wave = (wave + (self.wave - wave) * hold * 0.6).astype(np.float32)
        return self.bands, self.wave


def mirrored(values: np.ndarray) -> np.ndarray:
    """Bass in the middle, the same both ways: every band from low to high at half the detail, out to each side."""
    n = len(values)
    half = values[np.round(np.linspace(0, n - 1, (n + 1) // 2)).astype(int)]
    return np.concatenate((half[::-1], half[n % 2:]))


def colour(text: Any, fallback: str) -> tuple[float, float, float, float]:
    """"#rrggbb" or "#rrggbbaa" as (r, g, b, a) from 0 to 1; anything else is the fallback."""
    for value in (str(text or "").strip(), fallback):
        if value.startswith("#") and len(value) in (7, 9):
            try:
                parts = [int(value[i:i + 2], 16) / 255 for i in range(1, len(value), 2)]
            except ValueError:
                continue
            return (*parts[:3], parts[3] if len(parts) == 4 else 1.0)
    return (0.31, 0.85, 0.56, 1.0)


def premul(rgba: tuple[float, float, float, float], alpha: float = 1.0) -> np.ndarray:
    """B, G, R, A premultiplied, 0..255: how the GPU takes a "pBGRA" picture."""
    r, g, b, a = rgba
    a *= alpha
    return np.array([b * a, g * a, r * a, a], np.float32) * 255.0


def gradient(stops: list[tuple[float, np.ndarray]]) -> np.ndarray:
    """A W x H picture whose colour changes from row to row through the stops: [(y, premul colour)]."""
    ys = np.arange(H, dtype=np.float32) + 0.5
    at = [y for y, _ in stops]
    rows = np.stack([np.interp(ys, at, [c[k] for _, c in stops]) for k in range(4)], axis=1)
    return np.ascontiguousarray(np.broadcast_to(rows[:, None, :], (H, W, 4))).round().astype(np.uint8)


def smooth_line(values: np.ndarray, xs: np.ndarray, pad: float) -> np.ndarray:
    """values spread from pad to W - pad, read at xs with an eased curve between each pair (no corners)."""
    at = np.clip((xs - pad) / max(1.0, W - 2 * pad) * (len(values) - 1), 0, len(values) - 1)
    i = np.minimum(at.astype(int), len(values) - 2)
    t = at - i
    t = t * t * (3 - 2 * t)
    return values[i] * (1 - t) + values[i + 1] * t


def _words(picture: np.ndarray) -> np.ndarray:
    """The picture's pixels as one 32-bit number each (H x W), to copy a pixel in one go."""
    return picture.view(np.uint32)[..., 0]


class Painter:
    """Draws the looks with NumPy straight into premultiplied BGRA frames for the GPU.

    No QPainter: Qt keeps Python's lock for the whole of every QPainter call, and an antialiased wave
    took it 2 to 13 ms a frame (the encoders wait all that time). Every look here is a span of rows in
    each column (a bar, a wave's stroke, a line and the fill under it), so a frame is a picture made once
    for the colours (the "sprite") copied in where each column's span is, plus a soft first and last
    pixel. That's whole 32-bit pixels and NumPy, which lets the lock go, at about a millisecond.
    Three frames are reused in turn (one on the GPU, one waiting to go, one being drawn on a worker)."""

    PAD = 12.0

    def __init__(self):
        self._frames: list[np.ndarray] = []
        self._turn = 0
        self._sprites: dict[tuple, Any] = {}
        self._rows = np.arange(H, dtype=np.float32) + 0.5
        self._cols = np.arange(W, dtype=np.float32) + 0.5
        self._index = np.arange(W)
        self._outside = (self._cols < self.PAD) | (self._cols > W - self.PAD)

    def release(self) -> None:
        self._frames = []
        self._sprites = {}

    def _frame(self) -> np.ndarray:
        if not self._frames:
            self._frames = [np.zeros((H, W, 4), np.uint8) for _ in range(3)]
        self._turn = (self._turn + 1) % 3
        frame = self._frames[self._turn]
        frame.fill(0)
        return frame

    def _sprite(self, key: tuple, make):
        found = self._sprites.get(key)
        if found is None:
            if len(self._sprites) >= 4:
                self._sprites.pop(next(iter(self._sprites)))
            found = self._sprites[key] = make()
        return found

    def paste(self, frame: np.ndarray, sprite: np.ndarray, top: np.ndarray, bottom: np.ndarray) -> None:
        """Copy the sprite into the frame between top and bottom (y, one each per column), softly at both ends."""
        top = np.clip(top, 0.0, H).astype(np.float32)
        bottom = np.clip(bottom, 0.0, H).astype(np.float32)
        bottom[self._outside] = top[self._outside]  # the padding at the sides stays clear
        r0, r1 = int(top.min()), min(H, int(math.ceil(float(bottom.max()))))
        if r1 <= r0:
            return
        rows = self._rows[r0:r1, None]
        np.copyto(_words(frame)[r0:r1], _words(sprite)[r0:r1], where=(rows >= top) & (rows < bottom))
        first = np.ceil(top - 0.5).astype(int)  # the first row whose middle is inside
        last = np.ceil(bottom - 0.5).astype(int) - 1
        thick = last >= first
        for row, cover in ((first - 1, first - top), (last + 1, bottom - (last + 1))):
            ok = thick & (row >= 0) & (row < H) & (cover > 0.02)
            if not ok.any():
                continue
            r, c = row[ok], self._index[ok]
            src = sprite[r, c].astype(np.float32) * np.minimum(cover[ok], 1.0)[:, None]
            dst = frame[r, c].astype(np.float32)
            frame[r, c] = (src + dst * (1.0 - src[:, 3:4] / 255.0) + 0.5).astype(np.uint8)

    def _stroke(self, ys: np.ndarray, width: float) -> tuple[np.ndarray, np.ndarray]:
        """The span a line through (column, ys) covers in each column, width pixels thick."""
        mid = (ys[1:] + ys[:-1]) / 2
        before = np.concatenate((ys[:1], mid))
        after = np.concatenate((mid, ys[-1:]))
        lo = np.minimum(ys, np.minimum(before, after)) - width / 2
        hi = np.maximum(ys, np.maximum(before, after)) + width / 2
        return lo, hi

    def paint(self, style: str, bands: np.ndarray, wave: np.ndarray, settings: dict[str, Any]) -> np.ndarray:
        frame = self._frame()
        base = colour(settings.get("color"), "#4fd88f")
        peak = colour(settings.get("peak_color"), "#bff3d6")
        pad, floor = self.PAD, H - self.PAD
        if style == "circle":
            self._circle(frame, bands, base, peak)
        elif style == "wave":
            sprite = self._sprite(("wave", base, peak), lambda: gradient(
                [(pad, premul(peak)), (H / 2, premul(base)), (floor, premul(peak))]))
            ys = np.interp(self._cols, np.linspace(pad, W - pad, len(wave)), H / 2 - wave * (H / 2 - pad * 2))
            self.paste(frame, sprite, *self._stroke(ys, 5.0))
        elif style == "line":
            fill = self._sprite(("fill", base, peak), lambda: gradient(
                [(pad, premul(peak, 0.5)), (floor, premul(base, 0.0))]))
            line = self._sprite(("line", base, peak), lambda: gradient([(pad, premul(peak)), (floor, premul(base))]))
            ys = floor - 3 - smooth_line(bands, self._cols, pad) * (floor - pad - 6)
            self.paste(frame, fill, ys, np.full(W, floor, np.float32))
            self.paste(frame, line, *self._stroke(ys, 5.0))
        else:  # bars: pills from the floor up, the tips lighting up toward the peak colour
            count = len(bands)
            slot = (W - pad * 2) / count
            r = max(1.0, slot * 0.34)
            index = np.clip(((self._cols - pad) // slot).astype(int), 0, count - 1)
            dx = np.abs(self._cols - (pad + (index + 0.5) * slot))

            def make() -> np.ndarray:
                picture = gradient([(pad, premul(peak)), (floor, premul(base))])
                sides = np.clip(r + 0.5 - dx, 0.0, 1.0)  # soft left and right edges
                return (picture * sides[None, :, None] + 0.5).astype(np.uint8)

            sprite = self._sprite(("bars", count, base, peak), make)
            heights = np.maximum(2 * r, bands * (floor - pad))[index]
            cap = r - np.sqrt(np.maximum(0.0, r * r - np.minimum(dx, r) ** 2))  # round ends
            self.paste(frame, sprite, floor - heights + cap, floor - cap)
        return frame

    def _circle(self, frame: np.ndarray, bands: np.ndarray, base, peak) -> None:
        """Bars around a ring, growing outward from it, the ring drawn in."""
        count = len(bands)
        r0 = H * 0.26
        most = H / 2 - self.PAD - r0
        size = int(2 * (r0 + most) + 4)
        x0, y0 = (W - size) // 2, (H - size) // 2

        def make():
            ys, xs = np.mgrid[0:size, 0:size].astype(np.float32) + 0.5
            dx, dy = xs - size / 2, ys - size / 2
            radius = np.hypot(dx, dy)
            turn = (np.arctan2(dx, -dy) / (2 * math.pi)) % 1.0  # 0 at the top, clockwise
            index = np.round(turn * count).astype(int) % count
            off = (turn * count - np.round(turn * count)) * (2 * math.pi * radius / count)  # from the bar's middle
            half = max(1.5, min(7.0, math.pi * r0 / count * 0.55))
            cover = np.clip(half + 0.5 - np.abs(off), 0.0, 1.0) * np.clip(radius - r0 + 0.5, 0.0, 1.0)
            shade = np.clip((radius - r0) / most, 0.0, 1.0)[..., None]
            colours = premul(base) * (1 - shade) + premul(peak) * shade
            bars = (colours * cover[..., None] + 0.5).astype(np.uint8)
            soft = (colours * cover[..., None] * 0.5 + 0.5).astype(np.uint8)
            ring = (premul(base) * np.clip(2.0 - np.abs(radius - (r0 - 10)), 0.0, 1.0)[..., None] + 0.5).astype(np.uint8)
            return radius, index, _words(bars).copy(), _words(soft).copy(), _words(ring).copy()

        radius, index, bars, soft, ring = self._sprite(("circle", count, base, peak), make)
        end = (r0 + 3.0 + bands * most).astype(np.float32)[index]
        inside = radius >= r0 - 0.5
        out = ring.copy()
        np.copyto(out, bars, where=inside & (radius < end - 0.25))
        np.copyto(out, soft, where=inside & (radius >= end - 0.25) & (radius < end + 0.25))
        _words(frame)[y0:y0 + size, x0:x0 + size] = out


def look(settings: dict[str, Any]) -> tuple[str, int, float, float, bool]:
    """(style, bars, smoothing, sensitivity, mirror) from the settings, anything odd back to its default."""
    s = settings
    style = str(s.get("style") or "bars")
    if style not in dict(STYLES):
        style = "bars"
    try:
        count = int(s.get("bars", 48))
    except (TypeError, ValueError):
        count = 48
    count = max(8, min(128, count))
    try:
        smoothing = float(s.get("smoothing", 0.6))
    except (TypeError, ValueError):
        smoothing = 0.6
    try:
        sensitivity = float(s.get("sensitivity", 1.0))
    except (TypeError, ValueError):
        sensitivity = 1.0
    return style, count, max(0.0, min(0.95, smoothing)), max(0.05, min(10.0, sensitivity)), bool(s.get("mirror"))


@register(KIND)
class AudioVisualizerSource(Source):
    """The FFT and the drawing run on a shared worker, never on the render thread.

    On the PC (2026-11-16 heavy run) drawing on the render thread took 25-105 ms a frame, though it's about
    1 ms on its own: every NumPy step lets Python's lock go and then has to queue for it again behind the
    encoders, sound and window threads, up to 5 ms each time, and a frame has dozens of steps. So
    video_frame only hands over the last finished frame and, at most 30 times a second, asks a worker for
    the next one. The render thread spends microseconds here; a busy PC just gets a frame a little later."""

    label = "Audio visualizer"
    defaults = {"sound": listen.MIX, "style": "bars", "color": "#4fd88f", "peak_color": "#bff3d6",
                "bars": 48, "smoothing": 0.6, "mirror": False, "sensitivity": 1.0}
    choices = {"sound": _sounds, "style": STYLES}
    sleeps = True  # off air it stops listening and draws nothing
    own_sprig = (W, H)
    own_sprig_corner = "bottom_center"
    channel_order = "pBGRA"  # premultiplied B, G, R, A, drawn by Painter

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self.analyser = Analyser()
        self._painter = Painter()
        self._heard: str | None = None  # the sound we're listening to now
        self._hidden = False
        self._last = 0.0  # when the last drawing was asked for
        self._still = False  # the last drawing was of silence: don't draw it again
        self._turn = 0  # goes up whenever it stops listening: drawings asked for before then are dropped
        self._busy = False  # a drawing is being made
        self._ready: np.ndarray | None = None  # a finished drawing, for the next video_frame
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return W, H

    # Listening only while on air -------------------------------------------------------------

    def _sound(self) -> str:
        return str(self.settings.get("sound") or listen.MIX)

    def _listen(self) -> None:
        sound = self._sound()
        if self._heard == sound:
            return
        self._quiet()
        listen.add(sound, self.analyser.feed)
        self._heard = sound
        known = {sid for sid, _ in listen.sounds()}
        self.status = "ok" if sound == listen.MIX or not known or sound in known else "Its sound isn't here"

    def _quiet(self) -> None:
        if self._heard is not None:
            listen.remove(self._heard, self.analyser.feed)
            self._heard = None
        self._turn += 1
        self.analyser = Analyser()  # a drawing still being made keeps the old ones; nothing is shared
        self._painter = Painter()  # (and the frames go with them)
        self._busy = False
        self._ready = None
        self._still = False

    def start(self) -> None:
        if not self._hidden:
            self._listen()

    def wake(self) -> None:
        self._still = False
        if not self._hidden:
            self._listen()

    def sleep(self) -> None:
        self._quiet()

    def set_hidden(self, hidden: bool) -> None:
        self._hidden = hidden
        if hidden:
            self._quiet()
        else:
            self._listen()

    def stop(self) -> None:
        self.sleep()

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._still = False  # draw the new look straight away
        if self._heard is not None and self._heard != self._sound():
            self._listen()

    # Drawing ---------------------------------------------------------------------------------

    def look(self) -> tuple[str, int, float, float, bool]:
        return look(self.settings)

    def video_frame(self, now: float | None = None) -> np.ndarray | None:
        """The last finished drawing (once), and maybe ask for the next. Never draws here."""
        if self._heard is None:
            return None
        ready, self._ready = self._ready, None
        now = time.monotonic() if now is None else now
        dt = now - self._last
        if not self._busy and dt >= 1.0 / FPS - 0.002:  # 30 drawings a second is plenty for this
            self._last = now
            self._busy = True
            try:
                workers.run(self.draw, self._turn, self.analyser, self._painter, dict(self.settings),
                            min(dt, 0.5), name="visualizer")
            except RuntimeError:  # the pool is shutting down (SprigCast closing)
                self._busy = False
        return ready

    def draw(self, turn: int, analyser: Analyser, painter: Painter, settings: dict[str, Any], dt: float) -> None:
        """On a worker: the next bands and their drawing, left for video_frame."""
        try:
            style, count, smoothing, sensitivity, mirror = look(settings)
            bands, wave = analyser.step(count, smoothing, sensitivity, dt)
            still = float(bands.max(initial=0.0)) < STILL and float(np.abs(wave).max(initial=0.0)) < STILL
            if still and self._still:
                return  # silence, already drawn: nothing painted, nothing uploaded
            if mirror:
                bands, wave = mirrored(bands), mirrored(wave)
            frame = painter.paint(style, bands, wave, settings)
            if turn == self._turn:
                self._still = still
                self._ready = frame
        finally:
            if turn == self._turn:
                self._busy = False


# For apps (Streamer.bot): change the look --------------------------------------------------------

def _set(api, data):
    if data.get("source") is not None:
        source = api._source(data, "source")
        if source.kind != KIND:
            raise ApiError("bad_request", f"{source.name} isn't an audio visualizer.")
        found = [source]
    else:
        found = [s for s in api.col.sources if s.kind == KIND]
        if not found:
            raise ApiError("not_found", "Add an Audio visualizer first (+ → Plugins → Audio visualizer).")
    change: dict[str, Any] = {}
    if data.get("style") is not None:
        style = str(data["style"]).lower()
        if style not in dict(STYLES):
            raise ApiError("bad_request", "style must be bars, wave, circle or line.")
        change["style"] = style
    for key in ("color", "peak_color"):
        if data.get(key) is not None:
            value = str(data[key]).strip()
            if not re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", value):
                raise ApiError("bad_request", f"{key} must be a colour like #4fd88f.")
            change[key] = value
    if data.get("sensitivity") is not None:
        value = data["sensitivity"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.05 <= value <= 10:
            raise ApiError("bad_request", "sensitivity must be a number from 0.05 to 10.")
        change["sensitivity"] = float(value)
    for source in found:
        source.settings = {**source.settings, **change}
    if change:
        api.host.commit("sources")
    return {"sources": [s.name for s in found], **{k: found[0].settings.get(k) for k in ("style", "color", "peak_color")}}


register_action("visualizer.set", "Change an audio visualizer's look (no source: every one).",
                {"source": "?which Audio visualizer (default: all of them)", "style": "?bars, wave, circle or line",
                 "color": "?colour like #4fd88f", "peak_color": "?colour the tips light up to",
                 "sensitivity": "?0.05 to 10 (1 is normal)"}, _set)
