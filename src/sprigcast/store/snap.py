"""Snap: screenshots as a polaroid. A camera pops up, flashes and prints the screenshot.

Add Snap (+ → Plugins → Snap): it gets a sprig of its own, filling the canvas, on top of your
bloom. Then whenever you take a screenshot (the Screenshot button, its hotkey, or an app through
the API), the camera pops in, the button goes down, the flash goes off and the screenshot slides
out of the slot as a polaroid. It develops, grows to the middle, holds for a few seconds and slides
away. The screenshot is saved as usual (recordings folder → Screenshots, named by date and time).

Settings: Seconds (how long the polaroid stays up), Flash, and Save a
polaroid copy (also saves the framed picture next to the screenshot). It's only an animation:
nothing about your viewers is read or kept.

A SprigCast store plugin: add it from Settings → Plugins.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import register_item_menu, register_on_screenshot

log = logging.getLogger(__name__)

PLUGIN = {"id": "snap", "name": "Snap", "version": 1, "adds": "Source",
          "about": "Screenshots as a polaroid: a camera pops up on stream, flashes and prints the screenshot."}

W, H = 800, 1000  # the Snap's box
CAM_W, CAM_TOP = 420, 70  # the camera, drawn this wide, its picture's top this far down
MOST_WAITING = 3  # screenshots taken while one is up wait their turn; more than this are let go

# The art: vectors, drawn at whatever size they're shown. Where things are, in each SVG's own units.
ASSETS = Path(__file__).resolve().parent / "assets" / "snap"
CAM_SIZE = 256  # camera.svg and button.svg (the shutter button, drawn on its own so it can go down)
CAM_SLOT_Y, CAM_SLOT_X = 200, (52, 208)
CAM_FLASH = (213, 77)
FRAMES = {
    # file, size, photo window (x, y, w, h)
    "polaroid": ("frame.svg", (1060, 960), (80, 80, 900, 640)),
}

# The show, in seconds from the trigger.
POP = (0.0, 0.35)
PRESS = (0.45, 0.7)
FLASH = (0.55, 1.0)
PRINT = (0.8, 2.0)
CAMERA_OUT = (2.1, 2.5)
GROW = (2.1, 2.7)
DEVELOP = (1.0, 3.5)
LEAVE = 0.5


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


_SVG: dict[str, bytes] = {}  # the SVG files' text, read once


def draw_art(p, name: str, rect) -> None:
    """Draw one of the SVGs into rect (in the painter's units), sharp at any size."""
    from PySide6.QtCore import QByteArray
    from PySide6.QtSvg import QSvgRenderer

    data = _SVG.get(name)
    if data is None:
        data = _SVG[name] = (_assets_dir() / name).read_bytes()
    renderer = QSvgRenderer(QByteArray(data))  # made and dropped here: Qt objects stay on one thread
    renderer.render(p, rect)
    del renderer


def frame_art(frame: str):
    """(SVG file, (w, h), window QRect) for a frame ("polaroid")."""
    from PySide6.QtCore import QRect

    name, size, window = FRAMES.get(frame, FRAMES["polaroid"])
    return name, size, QRect(*window)


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


def compose(picture: np.ndarray | None, frame: str, width: int | None = None):
    """The polaroid: the picture cover-cropped into the frame's window, the frame on top.

    width scales the whole card (None: at least 1000 px across the photo). Returns (QImage, window
    QRectF), the window in the returned image's pixels.
    """
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter

    art, (art_w, art_h), window = frame_art(frame)
    scale = (width / art_w) if width else max(1.0, 1000 / window.width())
    out_w, out_h = max(1, round(art_w * scale)), max(1, round(art_h * scale))
    card = QImage(out_w, out_h, QImage.Format.Format_ARGB32_Premultiplied)
    card.fill(Qt.GlobalColor.transparent)
    win = QRectF(window.x() * scale, window.y() * scale, window.width() * scale, window.height() * scale)
    p = QPainter(card)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.fillRect(win.adjusted(-2, -2, 2, 2), QColor(24, 22, 20))
    if picture is not None and picture.size:
        photo = to_qimage(picture)
        cover = max(win.width() / photo.width(), win.height() / photo.height())
        sw, sh = win.width() / cover, win.height() / cover
        source = QRectF((photo.width() - sw) / 2, (photo.height() - sh) / 2, sw, sh)
        p.drawImage(win, photo, source)
    draw_art(p, art, QRectF(0, 0, out_w, out_h))
    p.end()
    return card, win


_SAVING = threading.Lock()
_TAKEN: set[Path] = set()  # names given out, so two snaps in the same second get different ones


def snap_name(folder: Path, when: datetime | None = None) -> Path:
    """Where a snap taken now goes: "snap 2026-11-14 20-03-05.png", with (2), (3)... in the same second."""
    when = when or datetime.now()
    with _SAVING:
        path = folder / when.strftime("snap %Y-%m-%d %H-%M-%S.png")
        n = 2
        while path.exists() or path in _TAKEN:
            path = folder / when.strftime(f"snap %Y-%m-%d %H-%M-%S ({n}).png")
            n += 1
        _TAKEN.add(path)
        return path


def save(picture: np.ndarray | None, frame: str, path: Path) -> bool:
    try:
        card, _win = compose(picture, frame)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _SAVING:
            part = path.with_name(path.name + ".part")  # written whole, then named: never a half-saved snap
            if not card.save(str(part), "PNG"):
                raise OSError(f"couldn't write {path}")
            part.replace(path)
        log.info("snap saved to %s", path)
        return True
    except Exception:
        log.exception("saving a snap failed")
        return False


class _Job:
    def __init__(self, picture: np.ndarray | None, path: Path | None):
        self.picture, self.path = picture, path
        self.card = None  # (QImage, window), made on the render thread
        self.started = 0.0
        self.saved = False


@register("snap")
class SnapSource(Source):
    label = "Snap"
    defaults = {"seconds": 6.0, "flash": True, "save_polaroid": False}
    sleeps = False  # a screenshot can come while it's hidden; it costs nothing until then
    own_sprig = (W, H)  # added from + → Plugins, it gets its own sprig filling the canvas, on top

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._lock = threading.Lock()
        self._queue: deque[_Job] = deque()
        self._job: _Job | None = None
        self._clear = True  # send one see-through frame
        self.status = "ready"
        self.last_saved: Path | None = None

    @property
    def size(self) -> tuple[int, int]:
        return W, H

    def wake(self) -> None:
        self._clear = True

    # Triggers (UI thread) ------------------------------------------------------------------------

    def show(self, picture: np.ndarray | None, path: Path | None = None) -> int:
        """Print this picture as a polaroid (and save the polaroid to path, if given).
        Returns how many are ahead of it, or -1 if too many are waiting already."""
        with self._lock:
            ahead = len(self._queue) + (self._job is not None)
            if ahead >= MOST_WAITING:
                return -1
            self._queue.append(_Job(picture, path))
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
        frame = "polaroid"
        _art, (art_w, art_h), _w = frame_art(frame)
        width = min(W * 0.86, art_w * (H * 0.80) / art_h)
        job.card = compose(job.picture, frame, int(width))
        self._save(job)

    def _save(self, job: _Job) -> None:
        if job.path is None:
            return
        args = (job.picture, "polaroid", job.path)

        def run():
            if save(*args):
                job.saved = True
                self.last_saved = job.path

        threading.Thread(target=run, name="snap-save", daemon=True).start()

    def video_frame(self) -> np.ndarray | None:
        with self._lock:
            if self._job is None and self._queue:
                self._job = self._queue.popleft()
                self._start(self._job)
            job = self._job
        if job is None:
            if self._clear:
                self._clear = False
                return np.zeros((H, W, 4), dtype=np.uint8)
            return None
        if job.card is None:
            self._picture_in(job)
        t = time.monotonic() - job.started
        if t >= self._length():
            with self._lock:
                if self._job is job:
                    self._job = None
            self.status = f"saved {job.path.name}" if job.saved and job.path else "ready"
            return np.zeros((H, W, 4), dtype=np.uint8)
        self.status = "snapping"
        return to_array(self.paint(job, t))

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
                img, win = compose(None, "polaroid", 400)
            small_w = (CAM_SLOT_X[1] - CAM_SLOT_X[0]) * k * 0.92
            small_h = small_w * img.height() / img.width()
            out = _smooth(_phase(t, PRINT))
            small = QRectF(W / 2 - small_w / 2, CAM_TOP + CAM_SLOT_Y * k - small_h + small_h * out, small_w, small_h)
            final = QRectF(W / 2 - img.width() / 2, H / 2 - img.height() / 2 + 30, img.width(), img.height())
            g = _smooth(_phase(t, GROW))
            rect = QRectF(small.x() + (final.x() - small.x()) * g, small.y() + (final.y() - small.y()) * g,
                          small.width() + (final.width() - small.width()) * g,
                          small.height() + (final.height() - small.height()) * g)
            rect.translate(0, leave * 160)
            p.save()
            p.setOpacity(1 - leave)
            if g <= 0:
                p.setClipRect(QRectF(0, slot_y, W, H))  # only what's come out of the slot
            centre = rect.center()
            p.translate(centre)
            p.rotate(-2.5 * g)
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
            draw_art(p, "button.svg", QRectF(0, dip, CAM_SIZE, CAM_SIZE))
            draw_art(p, "camera.svg", QRectF(0, 0, CAM_SIZE, CAM_SIZE))
            p.restore()

        # The flash.
        if self.settings.get("flash", True) and FLASH[0] < t < FLASH[1]:
            f = _phase(t, FLASH)
            fx, fy = cam_x + CAM_FLASH[0] * k, cam_y + CAM_FLASH[1] * k
            room = 2 * min(fx, W - fx, fy, H - fy)  # a glow cut off by the box's edge looks wrong
            size = 40 + (room - 40) * _smooth(min(1.0, f * 2.2))
            alpha = min(1.0, f * 10) * (1 - _smooth(f))
            p.save()
            p.setOpacity(alpha)
            draw_art(p, "flash.svg", QRectF(fx - size / 2, fy - size / 2, size, size))
            draw_art(p, "flash.svg", QRectF(fx - size / 4, fy - size / 4, size / 2, size / 2))  # a hot centre
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
    """Every Snap prints the first shape's screenshot (its "image"); a copy goes next to it if asked."""
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
        path = None
        if snap.settings.get("save_polaroid") and shot.get("path"):
            path = snap_name(Path(shot["path"]).parent)
        instance.show(picture, path)


register_on_screenshot(on_screenshot)


def _menu(api, source, add):
    if source is not None and source.kind == "snap":
        add("Take a screenshot", lambda: api.run("screenshot.take", {}))


register_item_menu(_menu)
