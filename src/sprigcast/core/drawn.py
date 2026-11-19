"""A store plugin's animation as a list of pictures to draw, not a painted picture (docs/native-engine.md, phase 6).

A source's video_frame may hand back a Drawn instead of a frame: the same calls as a QPainter (save, translate,
rotate, scale, opacity, clip, draw a picture, fill a box), but nothing is painted. With the native engine, the
pictures (the art) go to the graphics card once, and each frame only says where each one goes: the engine draws
them (sc_engine_source_draws), so an animation costs Python a few microseconds a frame instead of a QPainter
painting the whole box (which holds Python's lock all the while). Python's compositor gets the same list painted
with a QPainter (Drawn.picture), the way the plugin painted it before.

    d = drawn.Drawn(W, H)
    d.save(); d.translate(cx, cy); d.rotate(5); d.opacity(0.8)
    d.image("card", card_qimage, -w / 2, -h / 2, w, h)      # a picture: its key names it (same key, same art)
    d.restore()
    d.fill((38, 30, 26, 240), x, y, w, h)                    # a box of one colour
    return d

Art is told apart by its key and its QImage's cacheKey: a new picture under the same key is sent again.
Transforms: translate, rotate and scale in any order, as long as a picture isn't skewed (scale then rotate keeps
it a rectangle). A clip box is in the box's own units and is kept for pictures that aren't turned.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

ENGINE = False  # the native engine draws lists (render/native_bridge.py sets it): plugins may hand one over


def wanted() -> bool:
    """Whether a source should hand over a Drawn rather than paint (the native engine is drawing)."""
    return ENGINE


@dataclass
class Place:
    """One picture drawn: `image` (a QImage) into the box x, y, w, h of the coordinates `transform` maps onto the
    source's box (m11, m12, m21, m22, dx, dy, as QTransform's: x' = m11 x + m21 y + dx), the part `src` (x, y, w,
    h in the picture's pixels, None: all of it)."""

    key: str
    image: object
    x: float
    y: float
    w: float
    h: float
    src: tuple[float, float, float, float] | None
    transform: tuple[float, float, float, float, float, float]
    opacity: float
    clip: tuple[float, float, float, float] | None


_SOLID: dict[tuple, object] = {}


def solid(rgba: tuple[int, int, int, int]):
    """A small picture of one colour (premultiplied), for fill()."""
    found = _SOLID.get(rgba)
    if found is None:
        from PySide6.QtGui import QColor, QImage

        found = QImage(4, 4, QImage.Format.Format_ARGB32_Premultiplied)
        found.fill(QColor(*rgba))
        if len(_SOLID) > 64:
            _SOLID.clear()
        _SOLID[rgba] = found
    return found


def disc(rgba: tuple[int, int, int, int], size: int = 32):
    """A soft-edged disc of one colour filling a square picture `size` pixels wide, for drawEllipse() (made at the
    size it's shown, its edge stays as smooth as QPainter's)."""
    size = max(2, int(size))
    key = ("disc", rgba, size)
    found = _SOLID.get(key)
    if found is None:
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter

        found = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        found.fill(Qt.GlobalColor.transparent)
        p = QPainter(found)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(*rgba))
        p.drawEllipse(QRectF(0, 0, size, size))
        p.end()
        if len(_SOLID) > 64:
            _SOLID.clear()
        _SOLID[key] = found
    return found


_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


class Drawn:
    # The transform is kept as six numbers, not a QTransform: a list of a few hundred pictures a frame (Now
    # playing's sliding words) spent most of its time crossing into Qt for each point.

    def __init__(self, width: int, height: int):
        self.width, self.height = int(width), int(height)
        self.places: list[Place] = []
        self._transform = _IDENTITY
        self._opacity = 1.0
        self._clip: tuple[float, float, float, float] | None = None
        self._stack: list[tuple] = []
        self._painted = None
        self._brush = (0, 0, 0, 255)

    # The painter's state -------------------------------------------------------------------------------------

    def save(self) -> None:
        self._stack.append((self._transform, self._opacity, self._clip, self._brush))

    def restore(self) -> None:
        if self._stack:
            self._transform, self._opacity, self._clip, self._brush = self._stack.pop()

    def translate(self, dx, dy: float | None = None) -> None:
        if dy is None:  # a QPointF, as QPainter takes it
            dx, dy = dx.x(), dx.y()
        a, b, c, d, e, f = self._transform
        self._transform = (a, b, c, d, a * dx + c * dy + e, b * dx + d * dy + f)

    def rotate(self, degrees: float) -> None:
        a, b, c, d, e, f = self._transform
        r = math.radians(degrees)
        cos, sin = math.cos(r), math.sin(r)
        if degrees % 90 == 0:  # as QTransform: exact for quarter turns
            cos, sin = round(cos), round(sin)
        self._transform = (cos * a + sin * c, cos * b + sin * d, -sin * a + cos * c, -sin * b + cos * d, e, f)

    def scale(self, sx: float, sy: float) -> None:
        a, b, c, d, e, f = self._transform
        self._transform = (a * sx, b * sx, c * sy, d * sy, e, f)

    def _map(self, x: float, y: float) -> tuple[float, float]:
        a, b, c, d, e, f = self._transform
        return a * x + c * y + e, b * x + d * y + f

    def opacity(self, value: float | None = None) -> float:
        """As QPainter.setOpacity: this opacity from here on (until restore), not multiplied. With no value, the
        opacity now (as QPainter.opacity())."""
        if value is not None:
            self._opacity = max(0.0, min(1.0, float(value)))
        return self._opacity

    def clip(self, x: float, y: float, w: float, h: float) -> None:
        """Only this box (in the current coordinates, which mustn't be turned) shows of what's drawn next."""
        corners = [self._map(px, py) for px, py in ((x, y), (x + w, y), (x, y + h), (x + w, y + h))]
        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
        box = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        if self._clip is not None:
            box = _intersect(self._clip, box)
        self._clip = box

    # Drawing -------------------------------------------------------------------------------------------------

    def image(self, key: str, image, x: float, y: float, w: float, h: float,
              src: tuple[float, float, float, float] | None = None) -> None:
        if image is None or w <= 0 or h <= 0 or self._opacity <= 0.0:
            return
        self.places.append(Place(key, image, float(x), float(y), float(w), float(h), src, self._transform,
                                 self._opacity, self._clip))
        self._painted = None

    def fill(self, rgba: tuple[int, int, int, int], x: float, y: float, w: float, h: float) -> None:
        r, g, b, a = (int(v) for v in rgba)
        self.image(f"fill:{r},{g},{b},{a}", solid((r, g, b, a)), x, y, w, h, (1, 1, 2, 2))

    # QPainter's names, so a plugin's paint() can draw into a Drawn as it painted into a QImage -----------------

    def setRenderHint(self, *_args) -> None:  # noqa: N802 (QPainter's name)
        pass

    def end(self) -> None:
        pass

    def setOpacity(self, value: float) -> None:  # noqa: N802
        self.opacity(value)

    def setClipRect(self, rect) -> None:  # noqa: N802
        self.clip(rect.x(), rect.y(), rect.width(), rect.height())

    def drawImage(self, target, image, source=None) -> None:  # noqa: N802
        """target: a QRectF, or a QPointF (the picture at its own size); source: the part of it (QRectF)."""
        if hasattr(target, "width"):
            x, y, w, h = target.x(), target.y(), target.width(), target.height()
        else:
            x, y, w, h = target.x(), target.y(), image.width(), image.height()
        src = (source.x(), source.y(), source.width(), source.height()) if source is not None else None
        self.image(f"art:{image.cacheKey()}", image, x, y, w, h, src)

    def fillRect(self, rect, color) -> None:  # noqa: N802
        self.fill(color.getRgb(), rect.x(), rect.y(), rect.width(), rect.height())

    def setPen(self, *_args) -> None:  # noqa: N802 (outlines aren't drawn: discs and boxes only)
        pass

    def setBrush(self, color) -> None:  # noqa: N802
        self._brush = tuple(int(v) for v in color.getRgb())

    def drawEllipse(self, centre, rx: float, ry: float) -> None:  # noqa: N802
        """A disc of the brush's colour (QPainter.drawEllipse(QPointF, rx, ry))."""
        image = disc(self._brush)
        self.image(f"disc:{self._brush}", image, centre.x() - rx, centre.y() - ry, 2 * rx, 2 * ry)

    # For Python's compositor -----------------------------------------------------------------------------------

    def paint(self):
        """The list painted with a QPainter (a QImage, premultiplied ARGB)."""
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QImage, QPainter, QTransform

        image = QImage(max(1, self.width), max(1, self.height), QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for place in self.places:
            p.save()
            if place.clip is not None:
                p.setClipRect(QRectF(*place.clip))
            p.setTransform(QTransform(*place.transform))
            p.setOpacity(place.opacity)
            src = QRectF(*place.src) if place.src else QRectF(QPointF(0, 0), QPointF(place.image.width(),
                                                                                       place.image.height()))
            p.drawImage(QRectF(place.x, place.y, place.w, place.h), place.image, src)
            p.restore()
        p.end()
        return image

    def picture(self) -> np.ndarray:
        """paint() as a pBGRA frame (core/painted.Frame), painted once."""
        if self._painted is None:
            from sprigcast.core.painted import Frame

            self._painted = Frame()(self.paint())
        return self._painted

    @property
    def nbytes(self) -> int:
        return self.width * self.height * 4

    # For the engine ----------------------------------------------------------------------------------------------

    def items(self) -> list[tuple[str, object, float, float, float, float, float, tuple[int, int, int, int], float]]:
        """Each picture as the engine draws it: (key, image, x, y (its box's top left, unturned), width, height,
        rotation in degrees about its middle, crop (left, top, right, bottom pixels of the picture), opacity).
        Pictures clipped away entirely are left out."""
        out = []
        for place in self.places:
            iw, ih = place.image.width(), place.image.height()
            sx, sy, sw, sh = place.src if place.src else (0.0, 0.0, float(iw), float(ih))
            a, b, c, d, e, f = place.transform
            ax, ay = a * place.w, b * place.w  # the box's top edge and left edge, mapped
            dx, dy = c * place.h, d * place.h
            width, height = math.hypot(ax, ay), math.hypot(dx, dy)
            if width <= 1e-6 or height <= 1e-6 or sw <= 0 or sh <= 0:
                continue
            mx, my = place.x + place.w / 2, place.y + place.h / 2
            middle_x, middle_y = a * mx + c * my + e, b * mx + d * my + f
            rotation = math.degrees(math.atan2(ay, ax)) if ay else (0.0 if ax > 0 else 180.0)
            crop = [sx, sy, iw - sx - sw, ih - sy - sh]
            x, y = middle_x - width / 2, middle_y - height / 2
            if place.clip is not None:
                if abs(rotation) > 3.0:
                    pass  # turned well over: not clipped (the plugins clip only upright or barely wobbling pictures)
                else:
                    kept = _intersect(place.clip, (x, y, width, height))
                    if kept[2] <= 0 or kept[3] <= 0:
                        continue
                    per_x, per_y = sw / width, sh / height  # picture pixels per box unit
                    crop[0] += (kept[0] - x) * per_x
                    crop[1] += (kept[1] - y) * per_y
                    crop[2] += (x + width - kept[0] - kept[2]) * per_x
                    crop[3] += (y + height - kept[1] - kept[3]) * per_y
                    x, y, width, height = kept
            whole = [max(0, round(c)) for c in crop]
            if iw - whole[0] - whole[2] <= 0 or ih - whole[1] - whole[3] <= 0:
                continue
            out.append((place.key, place.image, x, y, width, height, rotation, tuple(whole), place.opacity))
        return out


def _intersect(a, b) -> tuple[float, float, float, float]:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))
