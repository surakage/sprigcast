"""Death count: a gravestone rises out of the ground, the number ticks over, and it sinks back down.

Add Death count (+ → Plugins → Death count): it gets a sprig of its own, filling the canvas, on top
of your bloom. Each time the count changes the ground pops up, the stone grows out of it showing the
old number, the number flips to the new one, it holds a few seconds and sinks back into the ground.

Change the count with hotkeys (Tools → Hotkeys → Plugins), a right-click on it, or from an app:
    {"do": "deaths.add"}                 one more (or "amount": 3)
    {"do": "deaths.remove"}              one less, never below 0
    {"do": "deaths.set", "count": 12}
    {"do": "deaths.reset"}
    {"do": "deaths.show"}                pop up with the count as it is
    {"do": "deaths.get"}                 just the number

The count is kept with your blooms, so it's still there after a restart. Settings: Count, Label
(the word above the number) and Seconds (how long it stays up).

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.control.native import ApiError, commit_settings, register_action, send_event
from sprigcast.core import drawn, painted
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import not_here, on_air, register_hotkeys, register_item_menu

log = logging.getLogger(__name__)

PLUGIN = {"id": "death_count", "name": "Death count", "version": 6, "adds": "Source",
          "about": "A gravestone grows out of the ground, ticks the death count over and sinks back down.",
          "new": "Lighter: its animation is drawn by SprigCast's engine."}

KIND = "death_count"
W, H = 800, 1000  # the box (same as Snap's, so it sits the same way on both shapes)
MOST_WAITING = 3  # pop-ups queued while it's sinking; more than this are let go
MOST = 999_999

# The art: vectors, drawn at whatever size they're shown. Where things are, in each SVG's own units.
ASSETS = Path(__file__).resolve().parent / "assets" / "death_count"
STONE = (400, 520)  # stone.svg
PLAQUE = (84, 150, 232, 252)  # the sunken panel the words are engraved in
NUMERAL_BOX = (44, 200, 312, 220)  # around the number's spot, with room for its pop and edge
MOUND = (600, 170)  # mound.svg
MOUND_LIP = 44  # where the stone comes out of the mound, in mound.svg's units

# Where things go in the box.
STONE_W = 300
MOUND_W = 560

# The show, in seconds from the trigger.
GROUND_IN = (0.0, 0.3)
RISE = (0.15, 1.05)
FLIP = 0.35  # how long the number takes to change
FLIP_AT = 1.3  # when it changes, the first time
SINK = 0.75
GROUND_OUT = 0.3


def _assets_dir() -> Path:
    if (ASSETS / "stone.svg").exists():
        return ASSETS
    from sprigcast import plugin_store  # installed in the plugins folder: the art stays with SprigCast

    return plugin_store.STORE_DIR / "assets" / "death_count"


def _phase(t: float, span: tuple[float, float]) -> float:
    a, b = span
    return 0.0 if t <= a else 1.0 if t >= b else (t - a) / (b - a)


def _smooth(x: float) -> float:
    return x * x * (3 - 2 * x)


def _back(x: float) -> float:
    """Eases out past 1 and settles back: a pop."""
    c = 1.70158
    x -= 1
    return 1 + (c + 1) * x * x * x + c * x * x


def draw_art(p, name: str, rect, sharp_at: float = 1.0) -> None:
    painted.draw(p, _assets_dir() / name, rect, sharp_at)  # drawn once per size, then reused


def to_array(image) -> np.ndarray:
    from PySide6.QtGui import QImage

    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = image.width(), image.height()
    rows = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(h, image.bytesPerLine())[:, : w * 4]
    return rows.reshape(h, w, 4).copy()


def count_of(settings: dict[str, Any]) -> int:
    try:
        return max(0, min(MOST, int(settings.get("count", 0) or 0)))
    except (TypeError, ValueError):
        return 0


class _Job:
    """One trip up and down. The number can change more than once while it's up."""

    def __init__(self, old: int, new: int):
        self.old, self.new = old, new
        self.flip_at = FLIP_AT if old != new else -1.0
        self.started = 0.0
        self.hold_from = FLIP_AT + FLIP  # when the hold starts (moves on if the number changes again)
        self.seed = random.randrange(1 << 30)


@register(KIND)
class DeathCountSource(Source):
    label = "Death count"
    defaults = {"count": 0, "label": "Deaths", "seconds": 3.0}
    sleeps = False  # a death can come while it's hidden; it costs nothing until then
    own_sprig = (W, H)  # added from + → Plugins, it gets its own sprig filling the canvas, on top

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._lock = threading.Lock()
        self._queue: deque[_Job] = deque()
        self._job: _Job | None = None
        self._clear = True
        self._count = count_of(self.settings)  # the number last shown (or asked for)
        self._frame = painted.Frame()
        self._still: tuple | None = None  # the still frame already sent (nothing moves until it sinks)
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return W, H

    channel_order = painted.Frame.channel_order

    def wake(self) -> None:
        self._clear = True
        self._still = None

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._still = None  # a new label or hold time: draw it again
        new = count_of(self.settings)
        if new != self._count:  # changed in its settings window: show it
            self.show(self._count, new)

    # Triggers (UI thread) ------------------------------------------------------------------------

    def show(self, old: int, new: int) -> bool:
        """Pop up and change old to new (the same number just pops up). False if too many are waiting."""
        with self._lock:
            self._count = new
            job = self._job
            if job is not None and not self._queue:
                t = time.monotonic() - job.started
                if t < self._sink_at(job):  # still up: change the number again and stay up longer
                    if new != job.new:
                        job.old = self._showing(job, t)
                        job.new = new
                        job.flip_at = max(t, RISE[1])
                        job.hold_from = job.flip_at + FLIP
                    else:
                        job.hold_from = max(job.hold_from, t)
                    return True
            if len(self._queue) >= MOST_WAITING:
                return False
            last = self._queue[-1].new if self._queue else job.new if job else old
            self._queue.append(_Job(last if job or self._queue else old, new))
            return True

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()
            self._job = None
            self._clear = True

    @property
    def busy(self) -> bool:
        return self._job is not None or bool(self._queue)

    # The show (render thread) --------------------------------------------------------------------

    def _hold(self) -> float:
        try:
            return max(0.5, float(self.settings.get("seconds", 3.0) or 3.0))
        except (TypeError, ValueError):
            return 3.0

    def _sink_at(self, job: _Job) -> float:
        return max(RISE[1], job.hold_from) + self._hold()

    def _length(self, job: _Job) -> float:
        return self._sink_at(job) + SINK + GROUND_OUT

    @staticmethod
    def _showing(job: _Job, t: float) -> int:
        return job.old if job.flip_at < 0 or t < job.flip_at + FLIP / 2 else job.new

    def video_frame(self) -> np.ndarray | None:
        with self._lock:
            if self._job is None and self._queue:
                self._job = self._queue.popleft()
                self._job.started = time.monotonic()
            job = self._job
        if job is None:
            if self._clear:
                self._clear = False
                self._frame.release()
                # Nothing to show: a tiny see-through picture (it's stretched to the box). A whole clear card
                # was sent on every wake, 26 ms of the PC's switch frame (2026-11-18, "death_count-1 26.1").
                return painted.clear_frame(2, 2)
            return None
        t = time.monotonic() - job.started
        if t >= self._length(job):
            with self._lock:
                if self._job is job:
                    self._job = None
            self._still = None
            self._frame.release()
            return painted.clear_frame(W, H)
        if self._still_from(job) <= t < self._sink_at(job):
            key = (id(job), job.flip_at, job.new)
            if self._still == key:
                return None  # standing still: the last frame is still right
            self._still = key
        picture = self.paint(job, t, listed=drawn.wanted())
        return picture if isinstance(picture, drawn.Drawn) else self._frame(picture)

    @staticmethod
    def _still_from(job: _Job) -> float:
        """When it stops moving: risen, the dirt landed, the number flipped and the thud over."""
        settled = max(RISE[1], GROUND_IN[1], RISE[0] + 1.05)
        if job.flip_at >= 0:
            settled = max(settled, job.flip_at + max(FLIP, 0.4))
        return settled

    def paint(self, job: _Job, t: float, listed: bool = False):
        """The death count at t seconds into this trip, as a QImage; listed: as a list of pictures for the engine
        to draw (core/drawn.py), nothing painted."""
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter

        if listed:
            image = p = drawn.Drawn(W, H)
        else:
            image = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            p = QPainter(image)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        sink_at = self._sink_at(job)
        end = sink_at + SINK
        ground_in = _back(_phase(t, GROUND_IN)) if t < GROUND_IN[1] else 1.0
        ground_out = _smooth(_phase(t, (end, end + GROUND_OUT)))
        ground = max(0.0, ground_in * (1 - ground_out))
        rise = _back(_phase(t, RISE)) if t < RISE[1] else 1.0
        down = _phase(t, (sink_at, end))
        up = rise * (1 - down * down)  # sinks slowly at first, then drops away

        k = STONE_W / STONE[0]
        stone_h = STONE[1] * k
        mound_k = MOUND_W / MOUND[0]
        mound_top = H - MOUND[1] * mound_k
        lip = mound_top + MOUND_LIP * mound_k  # the ground line the stone grows out of
        sunk_by = stone_h * 0.9  # how much of the stone stays above ground when up: all but its foot
        stone_top = lip - sunk_by * up

        # A thud when the number changes, and a wobble as it rises and sinks.
        shake = 0.0
        if job.flip_at >= 0 and job.flip_at <= t < job.flip_at + 0.4:
            shake = 5 * math.sin((t - job.flip_at) * 60) * (1 - (t - job.flip_at) / 0.4)
        elif RISE[0] < t < RISE[1] or sink_at < t < end:
            shake = 2.2 * math.sin(t * 70)

        # The stone, only the part above the ground.
        if up > 0.001:
            p.save()
            p.setClipRect(QRectF(0, 0, W, lip + 6))
            cx = W / 2
            p.translate(cx + shake, stone_top + stone_h)
            p.rotate(shake * 0.4)
            p.translate(-STONE_W / 2, -stone_h)
            flipping = job.flip_at >= 0 and job.flip_at <= t < job.flip_at + FLIP
            if flipping:  # the number is moving: the blank stone, then the numbers as pictures
                p.drawImage(QRectF(0, 0, STONE_W, stone_h), self._plate(k))
                p.scale(k, k)
                self._flip_numbers(p, job, t, k, QRectF)
            else:  # the stone as it stands, engraved once: text is the slowest thing QPainter draws
                stone = self._stone(job, t, k)
                p.drawImage(QRectF(0, 0, STONE_W, stone_h), stone)
            p.restore()

        # Dirt thrown up as it breaks out and as it goes back in.
        self._dirt(p, job, t, lip, sink_at, QColor, QPointF)

        # The ground, in front of the stone's foot.
        if ground > 0.001:
            p.save()
            p.translate(W / 2, H)
            p.scale(max(0.0, ground), max(0.0, min(1.2, ground)))
            p.translate(-MOUND_W / 2, -MOUND[1] * mound_k)
            p.scale(mound_k, mound_k)
            draw_art(p, "mound.svg", QRectF(0, 0, *MOUND), mound_k * 1.2)
            p.restore()
        p.end()
        return image

    def _stone(self, job: _Job, t: float, k: float):
        """The stone with its label and number engraved, put together once per (label, number) and reused."""
        from PySide6.QtCore import QRectF

        shown = self._showing(job, t)
        key = (self._label(), shown)
        cached = getattr(self, "_stone_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        image = self._plate(k).copy()
        p = _painter(image)
        p.scale(k, k)
        self._flip_numbers(p, _Job(shown, shown), 0.0, k, QRectF)
        p.end()
        self._stone_cache = (key, image)
        return image

    def _label(self) -> str:
        return str(self.settings.get("label", "Deaths") or "").strip().upper()

    def _plate(self, k: float):
        """The stone with only its label cut in: drawn once per label."""
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QFont, QImage

        label = self._label()
        cached = getattr(self, "_plate_cache", None)
        if cached is not None and cached[0] == label:
            return cached[1]
        image = QImage(round(STONE[0] * k), round(STONE[1] * k), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        p = _painter(image)
        p.scale(k, k)
        draw_art(p, "stone.svg", QRectF(0, 0, *STONE), k)
        if label:
            x, y, w, _h = PLAQUE
            font = QFont("Segoe UI")
            font.setPixelSize(30)
            font.setWeight(QFont.Weight.Bold)
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 5)
            p.setFont(font)
            _cut(p, QRectF(x, y + 20, w, 44), label, QColor, Qt, 30)
            p.setPen(QColor(111, 106, 93, 140))
            p.drawLine(int(x + 46), int(y + 76), int(x + w - 46), int(y + 76))
        p.end()
        self._plate_cache = (label, image)
        self._stone_cache = None
        return image

    def _numeral(self, value: int, k: float):
        """A number cut in stone, as a picture of NUMERAL_BOX (stone units) at k pixels per unit.

        The flip used to cut its two numbers as text every frame: 2-4 ms a frame here, and 15-24 ms on a
        PC busy with games and other apps. Now each number is cut once and the flip moves pictures."""
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QFont, QImage

        cache = self.__dict__.setdefault("_numerals", {})
        if value in cache:
            return cache[value]
        bx, by, bw, bh = NUMERAL_BOX
        image = QImage(round(bw * k), round(bh * k), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        p = _painter(image)
        p.scale(k, k)
        x, y, w, h = PLAQUE
        text = f"{value:,}"
        fit = min(140, 140 * 4 / max(4, len(text)))  # long numbers shrink to fit the plaque
        font = QFont("Segoe UI")
        font.setPixelSize(max(10, round(fit)))
        font.setWeight(QFont.Weight.Black)
        p.setFont(font)
        _cut(p, QRectF(x - bx, y + 84 - by, w, h - 100), text, QColor, Qt, fit)
        p.end()
        if len(cache) >= 4:
            cache.clear()
        cache[value] = image
        return image

    def _flip_numbers(self, p, job: _Job, t: float, k: float, QRectF) -> None:
        """The number on the plaque (in stone units): the old one lifting away as the new one pops in."""
        bx, by, bw, bh = NUMERAL_BOX
        x, y, w, h = PLAQUE
        cx, cy = x + w / 2, y + 84 + (h - 100) / 2

        def number(value: int, scale: float, alpha: float, dy: float) -> None:
            if alpha <= 0.001:
                return
            p.save()
            p.setOpacity(p.opacity() * min(1.0, alpha))
            p.translate(cx, cy + dy)
            p.scale(scale, scale)
            p.translate(-cx, -cy)
            p.drawImage(QRectF(bx, by, bw, bh), self._numeral(value, k))
            p.restore()

        if job.flip_at < 0 or t < job.flip_at:
            number(job.old, 1.0, 1.0, 0)
            return
        f = _phase(t, (job.flip_at, job.flip_at + FLIP))
        if f < 1:
            number(job.old, 1 - 0.3 * f, 1 - f, -40 * f)  # the old one lifts away
        number(job.new, _back(f) if f < 1 else 1.0, min(1.0, f * 2.5), 0)

    def _dirt(self, p, job: _Job, t: float, lip: float, sink_at: float, QColor, QPointF) -> None:
        bursts = [(RISE[0], 1.0), (sink_at + SINK * 0.55, 0.6)]
        rng = random.Random(job.seed)
        colours = [QColor(122, 75, 42), QColor(90, 55, 31), QColor(165, 122, 82), QColor(111, 165, 61)]
        from PySide6.QtCore import Qt

        p.save()
        p.setPen(Qt.PenStyle.NoPen)
        for start, amount in bursts:
            for _ in range(int(16 * amount)):
                vx = rng.uniform(-260, 260)
                vy = rng.uniform(-520, -260)
                r = rng.uniform(3, 8)
                colour = colours[rng.randrange(len(colours))]
                age = t - start - rng.uniform(0, 0.12)
                if age <= 0 or age > 0.9:
                    continue
                px = W / 2 + rng.uniform(-110, 110) + vx * age
                py = lip + 6 + vy * age + 900 * age * age
                if py > lip + 30:
                    continue
                p.setOpacity(min(1.0, (0.9 - age) * 3))
                p.setBrush(colour)
                p.drawEllipse(QPointF(px, py), r, r)
        p.restore()


def _painter(image):
    from PySide6.QtGui import QPainter

    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    return p


def _cut(p, box, text: str, QColor, Qt, size: float) -> None:
    """Text cut into stone: a dark groove with a light edge under it."""
    edge = max(1.0, size / 40)
    p.setPen(QColor(236, 230, 212, 150))
    p.drawText(box.translated(edge, edge), int(Qt.AlignmentFlag.AlignCenter), text)
    p.setPen(QColor(58, 54, 47, 235))
    p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)


# The count ----------------------------------------------------------------------------------------


def _counters(api, data: dict | None = None) -> list:
    """The death counts this is for: the one named (source), or every one."""
    if data and data.get("source") is not None:
        source = api._source(data, "source")
        if source.kind != KIND:
            raise ApiError("bad_request", f"{source.name} isn't a death count.")
        return [source]
    found = [s for s in api.col.sources if s.kind == KIND]
    if not found:
        raise ApiError("not_found", "Add a Death count first (+ → Plugins → Death count).")
    return found


def change(api, sources, how) -> dict:
    """Set each death count to how(old count), save it, and pop it up. Returns the new count."""
    count = 0
    for source in sources:
        old = count_of(source.settings)
        count = max(0, min(MOST, int(how(old))))
        source.settings = {**source.settings, "count": count}
        instance = api.host.source_instance(source.id)
        if instance is not None and hasattr(instance, "show"):
            instance.show(old, count)
    commit_settings(api.host, sources)  # kept with your blooms: there after a restart
    try:
        send_event(api, "deaths.changed", {"count": count})
    except Exception:
        log.debug("couldn't tell apps about the death count", exc_info=True)
    return {"count": count}


def _number(data, key: str, default: int | None = None) -> int:
    value = data.get(key, default)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value)  # web links send text
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise ApiError("bad_request", f"{key} must be a whole number.")
    return int(value)


def _add(api, data):
    amount = _number(data, "amount", 1)
    return change(api, _counters(api, data), lambda n: n + amount)


def _remove(api, data):
    amount = _number(data, "amount", 1)
    return change(api, _counters(api, data), lambda n: n - amount)


def _set(api, data):
    count = _number(data, "count")
    if count < 0:
        raise ApiError("bad_request", "count can't be below 0.")
    return change(api, _counters(api, data), lambda n: count)


def _reset(api, data):
    return change(api, _counters(api, data), lambda n: 0)


def _show(api, data):
    return change(api, _counters(api, data), lambda n: n)


def _get(api, data):
    return {"count": count_of(_counters(api, data)[0].settings)}


_WHICH = {"source": "?which Death count (default: all of them)"}
register_action("deaths.add", "One more death (or amount more). The gravestone pops up with the new count.",
                {"amount": "?how many (default 1)", **_WHICH}, _add)
register_action("deaths.remove", "One less death (or amount less), never below 0.",
                {"amount": "?how many (default 1)", **_WHICH}, _remove)
register_action("deaths.set", "Set the death count to a number.", {"count": "the new count", **_WHICH}, _set)
register_action("deaths.reset", "Back to 0 deaths.", dict(_WHICH), _reset)
register_action("deaths.show", "Pop the gravestone up with the count as it is.", dict(_WHICH), _show)
register_action("deaths.get", "The death count, without showing it.", dict(_WHICH), _get)


# Hotkeys and right-click --------------------------------------------------------------------------

HOTKEYS = {
    "deaths:add": ("Death count: +1", lambda n: n + 1),
    "deaths:remove": ("Death count: -1", lambda n: n - 1),
    "deaths:reset": ("Death count: reset", lambda n: 0),
    "deaths:show": ("Death count: show", lambda n: n),
}


def _hotkeys(collection):
    if not any(s.kind == KIND for s in collection.sources):
        return []
    return [(hid, label) for hid, (label, _fn) in HOTKEYS.items()]


def _run_hotkey(api, hid):
    entry = HOTKEYS.get(hid)
    if entry is None:
        return None
    try:
        counters = _counters(api)
        count = change(api, counters, entry[1])["count"]
    except ApiError as exc:
        log.info("death count hotkey: %s", exc)
        return exc.message
    if not any(on_air(api.col, s.id) for s in counters):  # still counted, but nobody can see it: say so
        return f"Deaths: {count}. " + not_here(counters[0].name)
    return None


register_hotkeys(_hotkeys, _run_hotkey)


def _menu(api, source, add):
    if source is None or source.kind != KIND:
        return
    add("Add a death", lambda: change(api, [source], lambda n: n + 1))
    add("Take one away", lambda: change(api, [source], lambda n: n - 1))
    add("Show the count", lambda: change(api, [source], lambda n: n))
    add("Reset to 0", lambda: change(api, [source], lambda n: 0))


register_item_menu(_menu)
