"""Snap: screenshots as a polaroid. A camera pops up, flashes and prints the screenshot.

Add Snap (+ → Plugins → Snap): it gets a sprig of its own, filling the canvas, on top of your
bloom. Then whenever you take a screenshot (the Screenshot button, its hotkey, or an app through
the API), the camera pops in, the button goes down, the flash goes off and the screenshot slides
out of the slot as a polaroid. It develops, grows to the middle, holds for a few seconds and slides
away. The polaroid is shaped to the screenshot, so the whole picture shows, edges and all, and it
stays inside the canvas the whole way.

The polaroid is only for show: the file saved is the screenshot itself (recordings folder →
Screenshots, named by date and time), without the polaroid frame and without a polaroid that's
still on screen from an earlier snap.

Settings: Seconds (how long the polaroid stays up) and Flash. It's only an animation: nothing about
your viewers is read or kept.

A SprigCast store plugin: add it from Settings → Plugins.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.core import painted
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import register_item_menu, register_on_screenshot

log = logging.getLogger(__name__)

PLUGIN = {"id": "snap", "name": "Snap", "version": 4, "adds": "Source",
          "about": "Screenshots as a polaroid: a camera pops up on stream, flashes and prints the screenshot.",
          "new": "The polaroid shows the whole screenshot and stays inside the canvas; only the screenshot is saved."}

W, H = 800, 1000  # the Snap's box
CAM_W, CAM_TOP = 420, 70  # the camera, drawn this wide, its picture's top this far down
MOST_WAITING = 3  # screenshots taken while one is up wait their turn; more than this are let go

# The art: vectors, drawn at whatever size they're shown. Where things are, in each SVG's own units.
ASSETS = Path(__file__).resolve().parent / "assets" / "snap"
CAM_SIZE = 256  # camera.svg and button.svg (the shutter button, drawn on its own so it can go down)
CAM_SLOT_Y, CAM_SLOT_X = 200, (52, 208)
CAM_FLASH = (213, 77)
# The polaroid card, in its own units: the photo window fits in WINDOW_MOST (shaped like the screenshot),
# with a BORDER of card around it, a wider strip under it for the print, and room for the corner leaves.
WINDOW_MOST = (900, 760)
BORDER, STRIP, PAD = 50, 160, 30
CARD_MOST = (W * 0.86, H * 0.80)  # the card held up in the middle fits in this (before it tilts)
TILT = -2.5  # degrees the held-up card leans
EDGE = 8  # pixels kept clear inside the Snap's box: nothing it draws reaches the canvas's edge

# The show, in seconds from the trigger.
POP = (0.0, 0.35)
PRESS = (0.45, 0.7)
FLASH = (0.55, 1.0)
PRINT = (0.8, 2.0)
CAMERA_OUT = (2.1, 2.5)
GROW = (2.1, 2.7)
DEVELOP = (1.0, 3.5)
LEAVE = 0.5
STILL_FROM = max(GROW[1], DEVELOP[1], CAMERA_OUT[1], FLASH[1], PRINT[1])  # from here it's held still


def _assets_dir() -> Path:
    if (ASSETS / "camera.svg").exists():
        return ASSETS
    from sprigcast import plugin_store  # installed in the plugins folder: the art stays with SprigCast

    return plugin_store.STORE_DIR / "assets" / "snap"


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
    """Draw one of the SVGs into rect (in the painter's units), sharp at sharp_at pixels per unit."""
    painted.draw(p, _assets_dir() / name, rect, sharp_at)  # drawn once per size, then reused


def window_size(picture_size: tuple[int, int] | None) -> tuple[int, int]:
    """The photo window for a picture this size (w, h): its shape, as big as fits in WINDOW_MOST."""
    most_w, most_h = WINDOW_MOST
    if not picture_size or picture_size[0] <= 0 or picture_size[1] <= 0:
        return 900, 640
    w, h = picture_size
    scale = min(most_w / w, most_h / h)
    return max(1, round(w * scale)), max(1, round(h * scale))


def card_layout(window: tuple[int, int]):
    """((art w, art h), window (x, y, w, h)) for a polaroid with this photo window, in card units."""
    win_w, win_h = window
    x = y = PAD + BORDER
    return (win_w + 2 * (PAD + BORDER), win_h + PAD * 2 + BORDER + STRIP), (x, y, win_w, win_h)


def frame_svg(window: tuple[int, int]) -> bytes:
    """Snap's polaroid as an SVG, made to fit a photo window this size: a warm cream card with leaves on two
    corners and the print along the bottom. The photo shows through the window."""
    (art_w, art_h), (x, y, ww, wh) = card_layout(window)
    l, t, r, b = PAD, PAD, art_w - PAD, art_h - PAD  # the card's edges
    wr, wb = x + ww, y + wh
    hole = f"M{x} {y} V{wb} H{wr} V{y} Z"
    card = f"M{l + 12} {t} H{r - 12} Q{r} {t} {r} {t + 12} V{b - 12} Q{r} {b} {r - 12} {b} H{l + 12} Q{l} {b} {l} {b - 12} " \
           f"V{t + 12} Q{l} {t} {l + 12} {t} Z"
    shadow = f"M{l + 22} {t + 14} H{r - 6} Q{r + 8} {t + 14} {r + 8} {t + 28} V{b} Q{r + 8} {b + 14} {r - 6} {b + 14} " \
             f"H{l + 22} Q{l + 8} {b + 14} {l + 8} {b} V{t + 28} Q{l + 8} {t + 14} {l + 22} {t + 14} Z"
    text_y, mark_x, mark_y = wb + 52, wr - 16, wb + 44
    words = "S P R I G C A S T   ·   S N A P" if ww >= 420 else ""  # a very narrow card keeps just the mark
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {art_w} {art_h}" width="{art_w}" height="{art_h}">
  <defs>
    <linearGradient id="paper" x1="0" y1="0" x2="0.25" y2="1">
      <stop offset="0" stop-color="#f3e6c4"/><stop offset="1" stop-color="#ddc79a"/>
    </linearGradient>
  </defs>
  <path fill-rule="evenodd" fill="#000000" opacity="0.28" d="{shadow} {hole}"/>
  <path fill-rule="evenodd" fill="url(#paper)" d="{card} {hole}"/>
  <path fill="none" stroke="#ffffff" stroke-opacity="0.35" stroke-width="3" d="M{l + 14} {t + 3} H{r - 14}"/>
  <rect x="{x - 2}" y="{y - 2}" width="{ww + 4}" height="{wh + 4}" fill="none" stroke="#b49a68" stroke-width="4"/>
  <text x="{x + 4}" y="{text_y}" font-family="Segoe UI, Arial, sans-serif" font-size="22" font-weight="600" fill="#a88f63">{words}</text>
  <circle cx="{mark_x}" cy="{mark_y}" r="14" fill="none" stroke="#b49a68" stroke-width="2.5"/>
  <path d="M{mark_x - 8} {mark_y + 7} C{mark_x - 8} {mark_y - 3} {mark_x - 1} {mark_y - 8} {mark_x + 9} {mark_y - 8} C{mark_x + 9} {mark_y + 2} {mark_x + 2} {mark_y + 7} {mark_x - 8} {mark_y + 7} Z" fill="#b49a68"/>
  <ellipse cx="{r - 30}" cy="{t + 4}" rx="58" ry="26" transform="rotate(-35 {r - 30} {t + 4})" fill="#6fa53d"/>
  <ellipse cx="{r - 18}" cy="{t + 28}" rx="44" ry="20" transform="rotate(20 {r - 18} {t + 28})" fill="#8cc152"/>
  <path d="M{r - 52} {t + 20} L{r - 14} {t + 8}" stroke="#4f8a2c" stroke-width="2.5" stroke-linecap="round"/>
  <ellipse cx="{l + 38}" cy="{b - 10}" rx="62" ry="27" transform="rotate(25 {l + 38} {b - 10})" fill="#6fa53d"/>
  <ellipse cx="{l + 70}" cy="{b + 4}" rx="50" ry="22" transform="rotate(-10 {l + 70} {b + 4})" fill="#8cc152"/>
  <ellipse cx="{l + 14}" cy="{b - 46}" rx="40" ry="18" transform="rotate(70 {l + 14} {b - 46})" fill="#a5d36a"/>
</svg>
""".encode()


def cover_scaled(picture: np.ndarray, width: int, height: int) -> np.ndarray:
    """The middle of an RGBA picture cut to width:height and scaled to exactly that size."""
    from PIL import Image

    h, w = picture.shape[:2]
    cover = max(width / w, height / h)
    sw, sh = min(w, max(1, round(width / cover))), min(h, max(1, round(height / cover)))
    x, y = (w - sw) // 2, (h - sh) // 2
    crop = np.ascontiguousarray(picture[y:y + sh, x:x + sw, :4])
    if (sw, sh) == (width, height):
        return crop
    return np.asarray(Image.fromarray(crop, "RGBA").resize((width, height), Image.Resampling.BILINEAR,
                                                           reducing_gap=2.0))


def to_qimage(picture: np.ndarray):
    from PySide6.QtGui import QImage

    picture = np.ascontiguousarray(picture)
    h, w = picture.shape[:2]
    return QImage(picture.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()


def to_array(image) -> np.ndarray:
    from PySide6.QtGui import QImage

    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = image.width(), image.height()
    rows = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(h, image.bytesPerLine())[:, : w * 4]
    return rows.reshape(h, w, 4).copy()


def compose(picture: np.ndarray | None, width: int | None = None):
    """The polaroid: the whole picture in a card shaped to it (nothing cut off), the frame on top.

    width scales the whole card (None: at least 1000 px across the photo). Returns (QImage, window
    QRectF), the window in the returned image's pixels.
    """
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    shape = (picture.shape[1], picture.shape[0]) if picture is not None and picture.size else None
    window = window_size(shape)
    (art_w, art_h), (wx, wy, ww, wh) = card_layout(window)
    scale = (width / art_w) if width else max(1.0, 1000 / ww)
    out_w, out_h = max(1, round(art_w * scale)), max(1, round(art_h * scale))
    card = QImage(out_w, out_h, QImage.Format.Format_ARGB32_Premultiplied)
    card.fill(Qt.GlobalColor.transparent)
    win = QRectF(wx * scale, wy * scale, ww * scale, wh * scale)
    p = QPainter(card)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.fillRect(win.adjusted(-2, -2, 2, 2), QColor(24, 22, 20))
    if picture is not None and picture.size:
        tw, th = max(1, round(win.width())), max(1, round(win.height()))
        # Shrunk to the window first by Pillow, which lets go of Python's lock while it works; QPainter then
        # only copies it in. QPainter scaling a whole screenshot took ~30 ms with the lock held (every
        # other thread waited: the 09:09:27 late frame). The window has the picture's shape, so this only
        # scales it (cover_scaled trims at most a pixel of rounding).
        p.drawImage(QRectF(win.x(), win.y(), tw, th), to_qimage(cover_scaled(picture, tw, th)))
    renderer = QSvgRenderer(QByteArray(frame_svg(window)))
    renderer.render(p, QRectF(0, 0, out_w, out_h))
    p.end()
    del renderer
    return card, win


def held_rect(card_w: float, card_h: float) -> tuple[float, float, float, float, float]:
    """Where the card is held up in the middle (x, y, w, h), and how far it can slide down as it leaves,
    so that, tilted, nothing of it reaches the box's edge."""
    import math

    tilt = math.radians(abs(TILT))
    # The tilted card's height (its corners swing out).
    tall = card_w * math.sin(tilt) + card_h * math.cos(tilt)
    cy = min(H / 2 + 30, H - EDGE - tall / 2)
    cy = max(cy, EDGE + tall / 2)
    cx = W / 2
    room = max(0.0, H - EDGE - (cy + tall / 2))
    return cx - card_w / 2, cy - card_h / 2, card_w, card_h, room


class _Job:
    def __init__(self, picture: np.ndarray | None):
        self.picture = picture
        self.card = None  # (QImage, window), made on a worker thread; the animation starts once it's ready
        self.composing = False
        self.started = 0.0


@register("snap")
class SnapSource(Source):
    label = "Snap"
    defaults = {"seconds": 6.0, "flash": True}
    sleeps = False  # a screenshot can come while it's hidden; it costs nothing until then
    in_screenshots = False  # only for show: a screenshot taken while a polaroid is up doesn't have it in
    own_sprig = (W, H)  # added from + → Plugins, it gets its own sprig filling the canvas, on top

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._lock = threading.Lock()
        self._queue: deque[_Job] = deque()
        self._job: _Job | None = None
        self._clear = True  # send one see-through frame
        self._frame = painted.Frame()
        self._still: int | None = None  # the job whose still frame was sent (the polaroid held up)
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
        self._still = None

    # Triggers (UI thread) ------------------------------------------------------------------------

    def show(self, picture: np.ndarray | None, _path: Any = None) -> int:
        """Print this picture as a polaroid: only on stream, nothing is saved (the screenshot already is).
        Returns how many are ahead of it, or -1 if too many are waiting already."""
        with self._lock:
            ahead = len(self._queue) + (self._job is not None)
            if ahead >= MOST_WAITING:
                return -1
            self._queue.append(_Job(picture))
            return ahead

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
        return max(1.0, float(self.settings.get("seconds", 8.0) or 8.0))

    def _length(self) -> float:
        return GROW[1] + self._hold() + LEAVE

    def _start(self, job: _Job) -> None:
        job.started = time.monotonic()

    def _picture_in(self, job: _Job) -> None:
        shape = (job.picture.shape[1], job.picture.shape[0]) if job.picture is not None and job.picture.size else None
        (art_w, art_h), _window = card_layout(window_size(shape))
        width = min(CARD_MOST[0], art_w * CARD_MOST[1] / art_h)
        card = compose(job.picture, int(width))
        job.started = time.monotonic()  # the animation starts now, from its first frame
        job.card = card

    def _compose_later(self, job: _Job) -> None:
        """Make the card off the render thread (it used to take ~36 ms there)."""
        job.composing = True

        def run():
            try:
                self._picture_in(job)
            except Exception:
                log.exception("snap: making the polaroid failed")
                job.card = (None, None)

        from sprigcast.core import workers

        workers.run(run, name="snap-card")

    def video_frame(self) -> np.ndarray | None:
        with self._lock:
            if self._job is None and self._queue:
                self._job = self._queue.popleft()
                self._start(self._job)
            job = self._job
        if job is None:
            if self._clear:
                self._clear = False
                self._frame.release()
                return painted.clear_frame(W, H)
            return None
        if job.card is None:
            if not job.composing:
                self._compose_later(job)
            return None  # a moment until the card is made
        if job.card[0] is None:  # couldn't make it: skip this snap
            with self._lock:
                if self._job is job:
                    self._job = None
            return None
        t = time.monotonic() - job.started
        if t >= self._length():
            with self._lock:
                if self._job is job:
                    self._job = None
            self._still = None
            self._frame.release()
            return painted.clear_frame(W, H)
        if STILL_FROM <= t < GROW[1] + self._hold():
            if self._still == id(job):
                return None  # the polaroid is held up: nothing moves until it leaves
            self._still = id(job)
        return self._frame(self.paint(job, t))

    def paint(self, job: _Job, t: float):
        """The Snap at t seconds into this snap, as a QImage."""
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter

        image = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        k = CAM_W / CAM_SIZE
        cam_x = (W - CAM_W) / 2
        lift = _smooth(_phase(t, CAMERA_OUT))
        cam_y = CAM_TOP - 70 * lift
        cam_alpha = min(1.0, _phase(t, (0.0, 0.12)) * 1.0) * (1 - lift)
        pop = _back(_phase(t, POP)) if t < POP[1] else 1.0
        pop = 0.6 + 0.4 * pop

        # The photo: printing out of the slot, then growing to the middle.
        card = job.card
        hold_end = GROW[1] + self._hold()
        leave = _smooth(_phase(t, (hold_end, hold_end + LEAVE)))
        slot_y = cam_y + CAM_SLOT_Y * k
        if t >= PRINT[0]:
            if card is not None:
                img, win = card
            else:
                img, win = compose(None, 400)
            small_w = (CAM_SLOT_X[1] - CAM_SLOT_X[0]) * k * 0.92
            small_h = small_w * img.height() / img.width()
            below = H - EDGE - (CAM_TOP + CAM_SLOT_Y * k)  # a tall print comes out smaller, inside the box
            if small_h > below:
                small_w, small_h = small_w * below / small_h, below
            out = _smooth(_phase(t, PRINT))
            small = QRectF(W / 2 - small_w / 2, CAM_TOP + CAM_SLOT_Y * k - small_h + small_h * out, small_w, small_h)
            *held, room = held_rect(img.width(), img.height())
            final = QRectF(*held)
            g = _smooth(_phase(t, GROW))
            rect = QRectF(small.x() + (final.x() - small.x()) * g, small.y() + (final.y() - small.y()) * g,
                          small.width() + (final.width() - small.width()) * g,
                          small.height() + (final.height() - small.height()) * g)
            rect.translate(0, leave * min(160.0, room))  # slides down, never past the edge
            p.save()
            p.setOpacity(1 - leave)
            if g <= 0:
                p.setClipRect(QRectF(0, slot_y, W, H))  # only what's come out of the slot
            centre = rect.center()
            p.translate(centre)
            p.rotate(TILT * g)
            sx, sy = rect.width() / img.width(), rect.height() / img.height()
            p.scale(sx, sy)
            p.translate(-img.width() / 2, -img.height() / 2)
            p.drawImage(QPointF(0, 0), img)
            develop = 1 - _smooth(_phase(t, DEVELOP))
            if develop > 0:
                p.fillRect(win, QColor(38, 30, 26, int(240 * develop)))
            p.restore()

        # The camera, in front of the photo coming out of it.
        if cam_alpha > 0:
            p.save()
            p.setOpacity(cam_alpha)
            p.translate(QPointF(W / 2, cam_y + CAM_SIZE * k / 2))
            p.scale(pop * k, pop * k)
            p.translate(-CAM_SIZE / 2, -CAM_SIZE / 2)
            press = _phase(t, PRESS)
            dip = 5 * (1 - abs(2 * press - 1)) if 0 < press < 1 else 0
            draw_art(p, "button.svg", QRectF(0, dip, CAM_SIZE, CAM_SIZE), k)
            draw_art(p, "camera.svg", QRectF(0, 0, CAM_SIZE, CAM_SIZE), k)
            p.restore()

        # The flash.
        if self.settings.get("flash", True) and FLASH[0] < t < FLASH[1]:
            f = _phase(t, FLASH)
            fx, fy = cam_x + CAM_FLASH[0] * k, cam_y + CAM_FLASH[1] * k
            room = 2 * (min(fx, W - fx, fy, H - fy) - EDGE)  # a glow cut off by the box's edge looks wrong
            size = 40 + (room - 40) * _smooth(min(1.0, f * 2.2))
            alpha = min(1.0, f * 10) * (1 - _smooth(f))
            p.save()
            p.setOpacity(alpha)
            glow = painted.art(_assets_dir() / "flash.svg", 512, 512)  # one size, scaled: it grows every frame
            whole = QRectF(0, 0, 512, 512)
            p.drawImage(QRectF(fx - size / 2, fy - size / 2, size, size), glow, whole)
            p.drawImage(QRectF(fx - size / 4, fy - size / 4, size / 2, size / 2), glow, whole)  # a hot centre
            p.restore()
        p.end()
        return image


# Screenshots --------------------------------------------------------------------------------------


def _picture(image) -> np.ndarray | None:
    """A screenshot (QImage or RGBA array) as an RGBA array."""
    if image is None or isinstance(image, np.ndarray):
        return image
    return to_array(image)


def on_screenshot(api, shots: list[dict]) -> None:
    """Every Snap prints the first shape's screenshot (its "image"). Only the screenshot is saved (by
    SprigCast, before this): the polaroid is just for show."""
    shot = next((s for s in shots if s.get("image") is not None), None)
    if shot is None:
        return
    picture = None
    for snap in (s for s in api.col.sources if s.kind == "snap"):
        instance = api.host.source_instance(snap.id)
        if instance is None or not hasattr(instance, "show"):
            continue
        if picture is None:
            picture = _picture(shot["image"])
        instance.show(picture)


register_on_screenshot(on_screenshot)


def _menu(api, source, add):
    if source is not None and source.kind == "snap":
        add("Take a screenshot", lambda: api.run("screenshot.take", {}))


register_item_menu(_menu)
