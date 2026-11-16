"""Instant replay: play the last few seconds back on stream, with a leafy wipe in and out.

Add Instant replay (+ → Plugins → Instant replay): it gets a sprig of its own, filling the canvas, on
top of your bloom. In its settings pick what it replays:
- Replay: one sprig or source, for example your game capture or Elgato capture.
- In the corner (optional): a second one shown small in a corner over it, like your camera.

While its bloom is on air it keeps the last Seconds of just that, encoded on the graphics card like
the clips buffer. Press its hotkey (Settings → Hotkeys → Plugins), right-click it, or trigger it from
an app or Stream Deck ({"do": "replay.play"}): canopy bands sweep across, REPLAY shows while the
screen is covered, and the replay plays with a badge and a thin progress line, at normal speed or in
slow motion. When it ends the bands sweep back and it hides itself. Apps hear replay.started and
replay.ended.

Light by design: nothing is kept while the replay's bloom is off air or its eye is off, only what you
picked is encoded (720p unless you pick Sharp), and the replay is played from a temporary file that's
deleted when it ends.

A SprigCast store plugin: add it from Settings → Plugins.
"""

from __future__ import annotations

import logging
import math
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

PLUGIN = {"id": "instant_replay", "name": "Instant replay", "version": 1, "adds": "Source",
          "about": "Play the last few seconds of your game (and camera) back on stream, with a replay wipe."}

KIND = "instant_replay"
W, H = 1920, 1080  # the replay's box on the canvas (its pictures are 720p or 1080p, scaled to fit)
SIZES = {"light": (1280, 720), "sharp": (1920, 1080)}
BITRATES = {"light": 5000, "sharp": 9000}  # kbps: replays are often slowed down, so a little extra
IN_S, OUT_S = 0.9, 0.8  # the wipe in and out
COVER_AT = 0.5  # how far into a wipe the screen is fully covered
HOLD = 0.16  # the share of a wipe the screen stays covered (REPLAY shows then)
BADGE_IN_S = 0.35
AHEAD = 5  # pictures decoded ahead
TAP_NAME = "replay-{id}"
AUDIO_KINDS = {"audio_input", "audio_output", "app_audio"}
SPEEDS = (("1", "Normal"), ("0.75", "Slower"), ("0.5", "Slow motion"), ("0.25", "Very slow"))
BADGES = (("top_left", "Top left"), ("top_right", "Top right"), ("off", "Off"))
CORNERS = (("bottom_right", "Bottom right"), ("bottom_left", "Bottom left"), ("top_right", "Top right"),
           ("top_left", "Top left"))

# Night Canopy: deep greens, the leaf accent and cream text. Pink stays for Live and Recording only.
DEEP = (14, 19, 17)  # theme.BG
CANOPY = (21, 28, 25)  # theme.PANEL
MOSS = (29, 52, 40)  # theme.SELECT_BG
LEAF = (79, 216, 143)  # theme.ACCENT
LEAF_DARK = (47, 140, 92)
LEAF_LIGHT = (165, 211, 106)
CREAM = (242, 247, 243)  # theme.TEXT_BRIGHT
MUTED = (139, 154, 146)


def _smooth(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def _out_cubic(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return 1 - (1 - x) ** 3


def band_lead(k: float, w: float, slant: float) -> float:
    """Where the big band's leading edge is (at the bottom of the picture) k of the way through a wipe.

    It comes in fast and slows as it covers the screen, stays while covered (HOLD), then speeds away."""
    span = w + 2 * slant  # the band's width: exactly covers the screen at the cover point
    covered = w + slant
    start, end = -slant, w + span + slant
    a, b = COVER_AT - HOLD / 2, COVER_AT + HOLD / 2
    if k <= a:
        return start + (covered - start) * _smooth(k / a)
    if k <= b:
        return covered
    x = (k - b) / (1 - b)
    return covered + (end - covered) * x * x


def covered(k: float) -> bool:
    """Is the screen fully covered at k? (The picture underneath changes then.)"""
    return COVER_AT - HOLD / 2 <= k <= COVER_AT + HOLD / 2


def title_alpha(k: float) -> float:
    a, b = COVER_AT - HOLD / 2, COVER_AT + HOLD / 2
    if k < a - 0.12 or k > b + 0.1:
        return 0.0
    if k < a:
        return _smooth((k - (a - 0.12)) / 0.12)
    if k <= b:
        return 1.0
    return 1 - _smooth((k - b) / 0.1)


# The art ---------------------------------------------------------------------------------------------

_CACHE: dict[tuple, Any] = {}


def _qcolor(rgb, alpha: float = 1.0):
    from PySide6.QtGui import QColor

    return QColor(rgb[0], rgb[1], rgb[2], max(0, min(255, round(255 * alpha))))


def _font(px: float, weight: int = 700, spacing: float = 0.0):
    from PySide6.QtGui import QFont

    font = QFont()
    font.setFamilies(["Segoe UI", "Inter", "Arial"])
    font.setPixelSize(max(6, round(px)))
    font.setWeight(QFont.Weight(weight))
    if spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return font


def _band(p, lead: float, width: float, slant: float, h: float, colour) -> None:
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPolygonF

    p.setPen(_no_pen())
    p.setBrush(colour)
    p.drawPolygon(QPolygonF([QPointF(lead - width + slant, -2), QPointF(lead + slant, -2),
                             QPointF(lead, h + 2), QPointF(lead - width, h + 2)]))


def _no_pen():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPen

    return QPen(Qt.PenStyle.NoPen)


def _leaf(p, x: float, y: float, length: float, angle: float, colour, vein) -> None:
    """A pointed leaf, its stem at x, y, pointing along angle (degrees)."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPainterPath, QPen

    p.save()
    p.translate(x, y)
    p.rotate(angle)
    w = length * 0.38
    path = QPainterPath(QPointF(0, 0))
    path.cubicTo(QPointF(length * 0.3, -w), QPointF(length * 0.75, -w * 0.8), QPointF(length, 0))
    path.cubicTo(QPointF(length * 0.75, w * 0.8), QPointF(length * 0.3, w), QPointF(0, 0))
    p.setPen(_no_pen())
    p.setBrush(colour)
    p.drawPath(path)
    pen = QPen(vein)
    pen.setWidthF(max(1.0, length * 0.035))
    p.setPen(pen)
    p.drawLine(QPointF(length * 0.06, 0), QPointF(length * 0.82, 0))
    p.restore()


def paint_wipe(p, w: int, h: int, k: float, leaving: bool = False, slow: bool = False) -> None:
    """The canopy wipe k of the way through (0..1): a leaf-green line, a moss band and the deep canopy
    band sweep across, with leaves on its edge. REPLAY shows while it covers the screen on the way in;
    leaving: mirrored, right to left, without the title. slow: SLOW MOTION under it."""
    if k <= 0 or k >= 1:
        return
    p.save()
    if leaving:
        p.translate(w, 0)
        p.scale(-1, 1)
    slant = 0.32 * h
    lead = band_lead(k, w, slant)
    big = w + 2 * slant
    # Back to front: the lighter bands run ahead of the deep one.
    _band(p, lead + 0.16 * w, 0.2 * w, slant, h, _qcolor(MOSS, 0.92))
    _band(p, lead + 0.215 * w, 0.022 * w, slant, h, _qcolor(LEAF))
    _band(p, lead, big, slant, h, _qcolor(DEEP))
    _band(p, lead + 0.004 * w, 0.03 * w, slant, h, _qcolor(CANOPY))  # a soft lit edge on the deep band
    _band(p, lead - big + 0.026 * w, 0.02 * w, slant, h, _qcolor(MOSS))  # and a trailing one as it leaves
    _band(p, lead - big + 0.008 * w, 0.006 * w, slant, h, _qcolor(LEAF, 0.85))
    # Leaves along the deep band's edge, swaying a little as it moves.
    sway = math.sin(k * math.pi * 3) * 8
    for share, size, angle, colour in ((0.12, 0.13, -28, LEAF), (0.36, 0.1, 18, LEAF_LIGHT),
                                       (0.62, 0.14, -12, LEAF_DARK), (0.88, 0.11, 24, LEAF)):
        y = share * h
        x = lead + slant * (1 - share) + 0.012 * w  # on the slanted edge
        _leaf(p, x, y, size * h, angle + sway, _qcolor(colour), _qcolor(DEEP, 0.55))
    p.restore()
    alpha = 0.0 if leaving else title_alpha(k)
    if alpha > 0:
        card = title_card(w, h, slow)
        from PySide6.QtCore import QRectF

        grow = 0.94 + 0.06 * _smooth(alpha)
        cw, ch = card.width() * grow, card.height() * grow
        p.save()
        p.setOpacity(alpha)
        p.drawImage(QRectF((w - cw) / 2, (h - ch) / 2, cw, ch), card, QRectF(0, 0, card.width(), card.height()))
        p.restore()


def _rewind(p, x: float, y: float, size: float, colour) -> None:
    """Two chevrons pointing back (◀◀), size tall, x, y their top-left."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPolygonF

    p.setPen(_no_pen())
    p.setBrush(colour)
    tri = size * 0.62
    for i in range(2):
        left = x + i * tri * 0.78
        p.drawPolygon(QPolygonF([QPointF(left + tri, y), QPointF(left + tri, y + size), QPointF(left, y + size / 2)]))


def title_card(w: int, h: int, slow: bool = False):
    """REPLAY with rewind chevrons and a leaf underline (SLOW MOTION under it when slowed), shown while the
    wipe covers the screen (cached)."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QFontMetricsF, QImage, QPainter, QPen

    key = ("title", w, h, slow)
    image = _CACHE.get(key)
    if image is not None:
        return image
    text_px = 0.13 * h
    font = _font(text_px, 800, text_px * 0.16)
    metrics = QFontMetricsF(font)
    text_w = metrics.horizontalAdvance("REPLAY")
    icon = text_px * 0.72
    gap = text_px * 0.35
    cw = int(icon * 1.3 + gap + text_w + text_px * 0.4)
    ch = int(text_px * (2.25 if slow else 1.75))
    image = QImage(cw, ch, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    base = text_px * 1.05
    _rewind(p, 0, base - icon * 0.92, icon, _qcolor(LEAF))
    p.setFont(font)
    p.setPen(_qcolor(CREAM))
    p.drawText(QPointF(icon * 1.3 + gap, base), "REPLAY")
    pen = QPen(_qcolor(LEAF))
    pen.setWidthF(max(2.0, text_px * 0.05))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    line_y = base + text_px * 0.3
    p.drawLine(QPointF(icon * 1.3 + gap + text_px * 0.04, line_y), QPointF(cw - text_px * 0.75, line_y))
    _leaf(p, cw - text_px * 0.72, line_y, text_px * 0.48, -24, _qcolor(LEAF), _qcolor(DEEP, 0.5))
    if slow:
        p.setFont(_font(text_px * 0.24, 700, text_px * 0.09))
        p.setPen(_qcolor(MUTED))
        p.drawText(QPointF(icon * 1.3 + gap + text_px * 0.04, line_y + text_px * 0.48), "SLOW MOTION")
    p.end()
    _CACHE[key] = image
    return image


def badge_image(h: int, speed: float):
    """The badge in the corner while a replay plays: ◀◀ REPLAY, and the speed when it's slowed down."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QFontMetricsF, QImage, QPainter, QPen

    tall = max(24, round(0.07 * h))
    font = _font(tall * 0.42, 800, tall * 0.07)
    small = _font(tall * 0.36, 600)
    label = "" if speed >= 0.999 else f"{speed:g}×"
    m, ms = QFontMetricsF(font), QFontMetricsF(small)
    pad = tall * 0.32
    icon_box = tall * 0.78
    width = pad * 0.6 + icon_box + pad * 0.7 + m.horizontalAdvance("REPLAY") + pad
    if label:
        width += ms.horizontalAdvance(label) + pad * 1.4
    width = int(width + 4)
    image = QImage(width, tall + 4, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    pill = QRectF(1, 1, width - 2, tall)
    pen = QPen(_qcolor((43, 54, 49)))  # theme.EDGE_STRONG
    pen.setWidthF(1.5)
    p.setPen(pen)
    p.setBrush(_qcolor(DEEP, 0.86))
    p.drawRoundedRect(pill, tall / 2, tall / 2)
    circle = QRectF(pad * 0.6, (tall - icon_box) / 2 + 1, icon_box, icon_box)
    p.setPen(_no_pen())
    p.setBrush(_qcolor(LEAF))
    p.drawEllipse(circle)
    _rewind(p, circle.x() + icon_box * 0.2, circle.y() + icon_box * 0.28, icon_box * 0.44, _qcolor(DEEP))
    p.setFont(font)
    p.setPen(_qcolor(CREAM))
    x = circle.right() + pad * 0.7
    base = tall / 2 + 1 + m.capHeight() / 2
    p.drawText(QPointF(x, base), "REPLAY")
    if label:
        x += m.horizontalAdvance("REPLAY") + pad * 0.7
        chip = QRectF(x, tall * 0.2 + 1, ms.horizontalAdvance(label) + pad * 0.9, tall * 0.6)
        p.setPen(_no_pen())
        p.setBrush(_qcolor(MOSS))
        p.drawRoundedRect(chip, chip.height() / 2, chip.height() / 2)
        p.setFont(small)
        p.setPen(_qcolor(LEAF))
        p.drawText(chip, Qt.AlignmentFlag.AlignCenter, label)
    p.end()
    return image


def image_array(image) -> np.ndarray:
    """A premultiplied ARGB32 QImage as an H x W x 4 array (B, G, R, A), a copy of its own."""
    w, h = image.width(), image.height()
    rows = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(h, image.bytesPerLine())[:, : w * 4]
    return rows.reshape(h, w, 4).copy()


def blend(frame: np.ndarray, over: np.ndarray, x: int, y: int) -> None:
    """Draw a premultiplied picture onto an opaque frame at x, y (cut to fit). In place, NumPy only:
    no QPainter on the frames of a playing replay (it holds Python's lock; see core/painted.py)."""
    fh, fw = frame.shape[:2]
    oh, ow = over.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(fw, x + ow), min(fh, y + oh)
    if x1 <= x0 or y1 <= y0:
        return
    part = over[y0 - y:y1 - y, x0 - x:x1 - x]
    region = frame[y0:y1, x0:x1]
    keep = 255 - part[..., 3:4].astype(np.uint16)
    region[...] = (part + (region.astype(np.uint16) * keep + 127) // 255).astype(np.uint8)


def progress_line(frame: np.ndarray, share: float) -> None:
    """A thin leaf-green line along the bottom: how much of the replay has played."""
    h, w = frame.shape[:2]
    tall = max(3, round(h * 0.006))
    rows = frame[h - tall:]
    rows[..., :3] = (rows[..., :3].astype(np.uint16) * 3 // 5).astype(np.uint8)  # a darker track
    done = max(0, min(w, round(w * share)))
    rows[:, :done, 0], rows[:, :done, 1], rows[:, :done, 2] = LEAF[2], LEAF[1], LEAF[0]


def wrap(frame: np.ndarray):
    """A QImage over a B, G, R, A frame (premultiplied), to paint on in place. Keep the frame alive."""
    from PySide6.QtGui import QImage

    h, w = frame.shape[:2]
    return QImage(frame.data, w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied)


def wipe_frame(base: np.ndarray | None, size: tuple[int, int], k: float, leaving: bool,
               slow: bool = False) -> np.ndarray:
    """One frame of a wipe: the canopy over base (a copy of it), or over nothing (see-through)."""
    from PySide6.QtGui import QPainter

    w, h = size
    frame = np.zeros((h, w, 4), dtype=np.uint8) if base is None else base.copy()
    image = wrap(frame)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    paint_wipe(p, w, h, k, leaving, slow)
    p.end()
    del image
    return frame


# What to replay ----------------------------------------------------------------------------------------


def _main_collection():
    """The scene collection open in the window (for the settings' drop-downs), or None."""
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        for widget in app.topLevelWidgets() if app is not None else []:
            col = getattr(widget, "collection", None)
            if col is not None and hasattr(col, "parts"):
                return col
    except Exception:
        log.exception("instant replay: couldn't find the open blooms")
    return None


def _replay_parts(col) -> list:
    return [p for p in col.part_scenes() if any(_kind(col, i.source) == KIND for i in p.items)]


def _kind(col, source_id: str) -> str | None:
    return next((s.kind for s in col.sources if s.id == source_id), None)


def placements(col, part) -> list:
    """The sources that place this sprig in something (any shape)."""
    from sprigcast.core.scene import PART_KIND

    ids = {s.id for s in col.shapes(part)}
    return [s for s in col.sources if s.kind == PART_KIND and s.settings.get("scene") in ids]


def capture_options(col) -> list[tuple[str, str]]:
    """What a replay can show, A-Z: sprigs that are placed somewhere, then sources that make pictures.
    Sprigs holding an Instant replay are left out (a replay of itself would show replays forever)."""
    from sprigcast.core.scene import PART_KIND

    if col is None:
        return []
    mine = _replay_parts(col)
    sprigs = [p for p in col.parts() if placements(col, p) and not any(col.contains(p, r.id) for r in mine)]
    sources = [s for s in col.sources if s.kind not in AUDIO_KINDS | {PART_KIND, KIND}]
    out = [(f"sprig:{p.id}", f"{p.name} (sprig)") for p in sorted(sprigs, key=lambda p: p.name.lower())]
    out += [(f"source:{s.id}", s.name) for s in sorted(sources, key=lambda s: s.name.lower())]
    return out


def _capture_choices():
    return [("", "Pick one")] + capture_options(_main_collection())


def _corner_choices():
    return [("", "Nothing")] + capture_options(_main_collection())


def _scenes_placing(col, source_id: str) -> list:
    return [sc for sc in col.scenes if any(i.source == source_id for i in sc.items)]


def pick_ref(col, value: Any) -> str:
    """What an app or older settings name ("Camera", an id, "sprig:Gameplay", "source:cam-1") as the choice
    the settings keep ("sprig:<id>" or "source:<id>"), or "" for nothing. A sprig's name wins over a
    source's. Raises ValueError saying what's wrong (no such thing, or nothing it can picture)."""
    from sprigcast.core.scene import PART_KIND

    text = str(value or "").strip()
    if not text:
        return ""
    what, _, ident = text.partition(":")
    if what not in ("sprig", "source") or not ident:
        what, ident = "", text
    ident = ident.strip()
    if what in ("", "sprig"):
        for part in col.parts():
            if part.id == ident or part.name.casefold() == ident.casefold():
                if not placements(col, part):
                    raise ValueError(f"The sprig {part.name!r} isn't in any bloom yet.")
                return f"sprig:{part.id}"
    if what in ("", "source"):
        for source in col.sources:
            if source.kind == PART_KIND:
                continue
            if source.id == ident or source.name.casefold() == ident.casefold():
                if source.kind in AUDIO_KINDS or source.kind == KIND:
                    raise ValueError(f"{source.name!r} has no picture to replay.")
                if not _scenes_placing(col, source.id):
                    raise ValueError(f"{source.name!r} isn't in any bloom or sprig yet.")
                return f"source:{source.id}"
    raise ValueError(f"There's no sprig or source called {ident!r}.")


def resolve(col, ref: str) -> tuple[str, str] | None:
    """A capture choice ("sprig:<id>" or "source:<id>", or a name: see pick_ref) as (source id to draw,
    canvas it sits on), or None when it's gone or not placed. A landscape canvas is picked first: replays
    are mostly gameplay."""
    try:
        ref = pick_ref(col, ref)
    except ValueError:
        return None
    if not ref:
        return None
    what, ident = ref.split(":", 1)
    canvases = {c.id: c for c in col.canvases}
    pairs: list[tuple[str, str]] = []
    if what == "sprig":
        part = next((p for p in col.parts() if p.id == ident), None)
        if part is None:
            return None
        for placement in placements(col, part):
            pairs += [(placement.id, sc.canvas) for sc in _scenes_placing(col, placement.id)]
    elif what == "source":
        if not any(s.id == ident for s in col.sources):
            return None
        pairs = [(ident, sc.canvas) for sc in _scenes_placing(col, ident)]
    pairs = [(s, c) for s, c in pairs if c in canvases]
    if not pairs:
        return None
    pairs.sort(key=lambda sc: canvases[sc[1]].width < canvases[sc[1]].height)
    return pairs[0]


def view_scene(col, settings: dict[str, Any], name: str):
    """(canvas, Scene) the replay records: what was picked, filling the canvas, and the corner pick small
    in its corner. Raises ValueError with a short reason when it can't."""
    from sprigcast.core.scene import Scene, SceneItem

    if not settings.get("capture"):
        raise ValueError("pick what to replay")
    pick_ref(col, settings.get("capture"))  # says what's wrong, by name
    main = resolve(col, str(settings.get("capture") or ""))
    if main is None:
        raise ValueError(f"can't show {settings.get('capture')!r}")
    source_id, canvas_id = main
    canvas = col.canvas(canvas_id)
    items = [SceneItem(source=source_id, bounds="fit", bounds_size=(canvas.width, canvas.height))]
    corner = resolve(col, str(settings.get("corner") or ""))
    if corner is not None and corner[0] != source_id:
        share = min(60, max(10, int(settings.get("corner_size", 30) or 30))) / 100
        at = str(settings.get("corner_at") or "bottom_right")
        margin = round(min(canvas.width, canvas.height) * 0.035)
        bw, bh = canvas.width * share, canvas.height * share
        x = margin if "left" in at else canvas.width - bw - margin
        y = margin if "top" in at else canvas.height - bh - margin
        items.append(SceneItem(source=corner[0], x=x, y=y, bounds="fit", bounds_size=(bw, bh),
                               bounds_align=at if at in ("top_left", "top_right", "bottom_left", "bottom_right")
                               else "bottom_right"))
    return canvas, Scene(id=f"view:{name}", name=name, canvas=canvas_id, items=items)


# Playing a replay ----------------------------------------------------------------------------------------


def fit_into(size: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """The picture's size scaled to fit the box (even numbers, for the scaler)."""
    w, h = size
    k = min(box[0] / max(1, w), box[1] / max(1, h))
    return max(2, round(w * k / 2) * 2), max(2, round(h * k / 2) * 2)


class Player:
    """Decodes a saved replay on a thread of its own, a few pictures ahead: B, G, R, A at `size` (letterboxed
    on the canopy colour when its shape differs), and its sound slowed with the picture (lower, like a
    tape). Starts skip_to seconds in."""

    def __init__(self, path: Path, size: tuple[int, int], speed: float = 1.0, sound: bool = True,
                 skip_to: float = 0.0):
        self.path, self.size, self.speed, self.sound, self.skip_to = path, size, speed, sound, skip_to
        self._lock = threading.Condition()
        self.frames: deque[tuple[float, np.ndarray]] = deque()
        self.audio: deque[np.ndarray] = deque()
        self.done = False
        self.error: str | None = None
        self.length = 0.0  # the newest picture's time so far: the replay's length once done
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="replay-play", daemon=True)

    def start(self) -> Player:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._lock.notify_all()

    def wait_first(self, timeout: float) -> bool:
        with self._lock:
            return self._lock.wait_for(lambda: self.frames or self.done, timeout) and bool(self.frames)

    def take(self, clip_t: float) -> np.ndarray | None:
        """The picture due at clip_t (seconds into the replay), or None when it's the same one as before."""
        with self._lock:
            picked = None
            while self.frames and self.frames[0][0] <= clip_t + 0.004:
                picked = self.frames.popleft()
                self._lock.notify_all()
            return None if picked is None else picked[1]

    def finished(self) -> bool:
        with self._lock:
            return self.done and not self.frames

    def read(self, count: int) -> np.ndarray:
        out = np.zeros((count, 2), dtype=np.float32)
        filled = 0
        with self._lock:
            while self.audio and filled < count:
                chunk = self.audio[0]
                take = min(count - filled, len(chunk))
                out[filled:filled + take] = chunk[:take]
                filled += take
                if take == len(chunk):
                    self.audio.popleft()
                else:
                    self.audio[0] = chunk[take:]
        return out

    def _run(self) -> None:
        import av

        from sprigcast.core.frames import picture_array
        from sprigcast.sources.audio import SAMPLE_RATE
        from sprigcast.sources.media import sound_resampler

        try:
            with av.open(str(self.path)) as container:
                video = container.streams.video[0]
                audio = container.streams.audio[0] if self.sound and container.streams.audio else None
                video.thread_type = "AUTO"
                video.codec_context.thread_count = 2
                fit = fit_into((video.codec_context.width, video.codec_context.height), self.size)
                resampler = sound_resampler(max(8000, round(SAMPLE_RATE / self.speed))) if audio is not None else None
                origin: float | None = None
                streams = [s for s in (video, audio) if s is not None]
                for packet in container.demux(*streams):
                    if self._stop.is_set():
                        return
                    if packet.stream is audio:
                        for frame in packet.decode():
                            if frame.pts is None or origin is None:
                                continue
                            t = float(frame.pts * frame.time_base) - origin
                            for out in resampler.resample(frame):
                                if t + out.samples * self.speed / SAMPLE_RATE < self.skip_to:
                                    continue
                                samples = out.to_ndarray().reshape(-1, 2).astype(np.float32, copy=True)
                                with self._lock:
                                    self.audio.append(samples)
                        continue
                    for frame in packet.decode():
                        pts = frame.pts if frame.pts is not None else packet.pts
                        t = float(pts * video.time_base) if pts is not None else 0.0
                        if origin is None:
                            origin = t
                        t -= origin
                        if t < self.skip_to - 0.001:
                            continue
                        picture = picture_array(frame, width=fit[0], height=fit[1], format="bgra")
                        if fit != self.size:
                            picture = self._letterbox(picture)
                        with self._lock:
                            self._lock.wait_for(lambda: self._stop.is_set() or len(self.frames) < AHEAD)
                            if self._stop.is_set():
                                return
                            self.frames.append((t - self.skip_to, picture))
                            self.length = max(self.length, t - self.skip_to)
                            self._lock.notify_all()
        except Exception as exc:
            log.warning("instant replay: couldn't play %s: %s", self.path.name, exc)
            self.error = str(exc) or exc.__class__.__name__
        finally:
            with self._lock:
                self.done = True
                self._lock.notify_all()

    def _letterbox(self, picture: np.ndarray) -> np.ndarray:
        w, h = self.size
        out = np.empty((h, w, 4), dtype=np.uint8)
        out[...] = (DEEP[2], DEEP[1], DEEP[0], 255)
        ph, pw = picture.shape[:2]
        x, y = (w - pw) // 2, (h - ph) // 2
        out[y:y + ph, x:x + pw] = picture
        return out


class _Show:
    """One replay on screen: the wipe in, the replay, the wipe out."""

    def __init__(self, player: Player, seconds: float, speed: float, size: tuple[int, int],
                 badge: np.ndarray | None, badge_at: str = "top_left"):
        self.player, self.seconds, self.speed, self.size, self.badge = player, seconds, speed, size, badge
        self.badge_at = badge_at
        self.phase = "in"
        self.started = time.monotonic()
        self.clock: float | None = None  # when the replay's own time started (the middle of the wipe in)
        self.first: np.ndarray | None = None  # the replay's first picture, under the wipe in
        self.last: np.ndarray | None = None  # the newest picture shown (held under the wipe out)
        self.out_at = 0.0
        self.reason = "ended"
        self.path: Path | None = None  # the saved replay, deleted when the show ends


# The source ---------------------------------------------------------------------------------------------


@register(KIND)
class InstantReplaySource(Source):
    label = "Instant replay"
    defaults = {"capture": "", "corner": "", "corner_at": "bottom_right", "corner_size": 30, "seconds": 20,
                "speed": "1", "badge": "top_left", "replay_sound": True, "sound_level": 100, "quality": "light"}
    choices = {"capture": _capture_choices, "corner": _corner_choices, "corner_at": CORNERS, "speed": SPEEDS, "badge": BADGES,
               "quality": (("light", "Light (720p)"), ("sharp", "Sharp (1080p)"))}
    sleeps = True  # off air (or its eye off), it keeps nothing and costs nothing

    @staticmethod
    def check_settings(col, settings: dict[str, Any]) -> dict[str, Any]:
        """For apps (source.set / source.new): Replay and In the corner take a name or id and are kept as
        the choice they match; anything unknown is refused, saying why."""
        for key in ("capture", "corner"):
            if key in settings:
                settings[key] = pick_ref(col, settings[key])
        return settings
    wants_engine = True  # the engine hands itself over, for the frame and sound taps
    own_sprig = (W, H)  # added from + → Plugins, it gets its own sprig filling the canvas, on top
    channel_order = "pBGRA"

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self.engine = None
        self._arm_lock = threading.Lock()
        self._gen = 0
        self._want = False
        self._buffer = None
        self._attached: tuple | None = None  # (tap name, frame callback, audio callback)
        self._show: _Show | None = None
        self._busy = False  # a replay is being made ready, or on screen
        self._clear = True
        self._hidden = False
        self._held: np.ndarray | None = None  # the frame handed over last: alive until the next
        self.outbox: deque[tuple[str, dict]] = deque()  # ("started" | "ended", details) for apps
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return W, H

    @property
    def gain(self) -> float:
        if not self.settings.get("replay_sound", True):
            return 0.0
        return max(0.0, min(2.0, float(self.settings.get("sound_level", 100) or 0) / 100))

    @property
    def out_size(self) -> tuple[int, int]:
        return SIZES.get(str(self.settings.get("quality")), SIZES["light"])

    # Keeping the last seconds ------------------------------------------------------------------------

    def start(self) -> None:
        self._arm()

    def wake(self) -> None:
        self._clear = True
        if not self._hidden:
            self._arm()

    def sleep(self) -> None:
        self._disarm()
        self._end_now()

    def stop(self) -> None:
        self._disarm()
        self._end_now()

    def set_hidden(self, hidden: bool) -> None:
        self._hidden = hidden
        if hidden:
            self._disarm()
            self._end_now()
            self._clear = True
        else:
            self._arm()

    def _end_now(self) -> None:
        """Off air: a replay that was playing ends at once (nothing would draw its wipe out)."""
        show = self._show
        if show is not None:
            show.reason = "stopped"
            self._finish(show)

    def update(self, settings: dict[str, Any]) -> None:
        keys = ("capture", "corner", "corner_at", "corner_size", "seconds", "quality")
        again = any(settings.get(k) != self.settings.get(k) for k in keys)
        super().update(settings)
        if again and self._want:
            self._arm()

    @property
    def ready_seconds(self) -> float:
        buf = self._buffer
        return buf.buffered_seconds if buf is not None and buf.running else 0.0

    def _arm(self) -> None:
        from sprigcast.core import workers

        with self._arm_lock:
            self._want = True
            self._gen += 1
            gen = self._gen
        workers.run(self._arm_now, gen, name="replay-arm")

    def _disarm(self) -> None:
        from sprigcast.core import workers

        with self._arm_lock:
            if not self._want and self._buffer is None:
                return
            self._want = False
            self._gen += 1
        workers.run(self._detach, name="replay-disarm")

    def _arm_now(self, gen: int) -> None:
        with self._arm_lock:
            if gen != self._gen or not self._want:
                return
            self._detach_locked()
            engine = self.engine
            if engine is None:
                self.status = "not running"
                return
            name = TAP_NAME.format(id=self.id)
            try:
                canvas, scene = view_scene(engine._collection, self.settings, name)
            except ValueError as exc:
                self.status = str(exc)
                log.warning("instant replay %s: can't keep replays: %s", self.id, exc)
                return
            from sprigcast.output.recorder import resolve_codec, shrink_to
            from sprigcast.output.replay import ReplayBuffer

            codec = resolve_codec(None)
            quality = str(self.settings.get("quality") or "light")
            light = codec == "libx264"  # no graphics-card encoder: keep it light whatever was picked
            short = SIZES["light" if light else quality if quality in SIZES else "light"][1]
            out = shrink_to(canvas.width, canvas.height, short) or (canvas.width // 2 * 2, canvas.height // 2 * 2)
            fps = min(canvas.fps, 30) if light else canvas.fps
            seconds = float(min(120, max(3, int(self.settings.get("seconds", 20) or 20))))
            buf = ReplayBuffer(f"replay {self.id}", canvas.width, canvas.height, fps,
                               BITRATES["light" if light or quality not in BITRATES else quality], codec,
                               time.perf_counter(), seconds, "mkv", out_size=out, tracks=("clips",))
            buf.start()
            tap = f"view:{name}"
            engine.set_view(name, canvas.id, "scene", scene)
            engine.add_frame_tap(tap, buf.push_frame, buf.gpu_size)
            engine.mixer.add_tap(buf.push_audio, buf.tracks)
            self._buffer, self._attached = buf, (name, buf.push_frame, buf.push_audio)
            self.status = "ok"
            log.info("instant replay %s: keeping the last %g s of %s (%dx%d, %s)", self.id, seconds,
                     self.settings.get("capture"), out[0], out[1], codec)

    def _detach(self) -> None:
        with self._arm_lock:
            if not self._want:
                self._detach_locked()

    def _detach_locked(self) -> None:
        buf, attached = self._buffer, self._attached
        self._buffer = self._attached = None
        if attached is not None and self.engine is not None:
            name, frame_cb, audio_cb = attached
            self.engine.remove_frame_tap(f"view:{name}", frame_cb)
            self.engine.mixer.remove_tap(audio_cb)
            self.engine.set_view(name, None)
        if buf is not None:
            buf.stop()
            log.info("instant replay %s: stopped keeping replays", self.id)

    # Playing (any thread) --------------------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    def play(self, seconds: float | None = None, speed: float | None = None) -> str | None:
        """Play the last seconds back. Returns why it can't (a short note), or None when it starts."""
        if self._busy:
            return "A replay is already playing."
        buf = self._buffer
        if buf is None or not buf.running:
            return "Instant replay isn't on air." if self.status == "ok" else f"Instant replay: {self.status.rstrip('.')}."
        have = buf.buffered_seconds
        if have < 1.0:
            return "The replay isn't ready yet."
        wanted = float(seconds if seconds is not None else self.settings.get("seconds", 20) or 20)
        wanted = max(1.0, min(wanted, have))
        pace = float(speed if speed is not None else self.settings.get("speed", "1") or 1)
        pace = min(1.0, max(0.25, pace))
        self._busy = True
        from sprigcast.core import workers

        workers.run(self._prepare, buf, have, wanted, pace, name="replay-prepare")
        return None

    def cancel(self, reason: str = "stopped") -> bool:
        """End the replay now: the wipe out runs (straight away off air). True if one was playing."""
        show = self._show
        if show is None:
            return False
        show.reason = reason
        if show.phase in ("in", "play"):
            show.phase = "out"
            show.out_at = time.monotonic()
        return True

    def _prepare(self, buf, have: float, seconds: float, speed: float) -> None:
        folder = Path(tempfile.gettempdir()) / "SprigCast"
        path = folder / f"replay-{self.id}-{int(time.time() * 1000)}.mkv"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            buf.save(path)
            player = Player(path, self.out_size, speed, bool(self.settings.get("replay_sound", True)),
                            skip_to=max(0.0, have - seconds)).start()
            if not player.wait_first(5.0):
                player.stop()
                raise RuntimeError(player.error or "nothing to play")
        except Exception as exc:
            log.warning("instant replay %s: couldn't make the replay: %s", self.id, exc)
            _remove_later(path)
            self._busy = False
            self.outbox.append(("ended", {"reason": "failed", "error": str(exc)}))
            return
        h = self.out_size[1]
        at = str(self.settings.get("badge") or "top_left")
        badge = None if at == "off" else image_array(badge_image(h, speed))
        show = _Show(player, seconds, speed, self.out_size, badge, at)
        show.path = path
        self._show = show
        if not self._want:  # went off air while it was being made ready: nothing would draw it
            show.reason = "stopped"
            self._finish(show)
            return
        self.outbox.append(("started", {"seconds": round(seconds, 2), "speed": speed}))

    # The picture (render thread) ------------------------------------------------------------------------

    def video_frame(self) -> np.ndarray | None:
        show = self._show
        if show is None:
            if self._clear:
                self._clear = False
                self._held = None
                w, h = self.out_size
                return np.zeros((h, w, 4), dtype=np.uint8)
            return None
        try:
            frame = self._show_frame(show, time.monotonic())
        except Exception:
            log.exception("instant replay %s: drawing the replay failed", self.id)
            show.reason = "failed"
            frame = None
            show.phase = "done"
        if show.phase == "done":
            self._finish(show)
            self._held = None
            w, h = self.out_size
            return np.zeros((h, w, 4), dtype=np.uint8)
        if frame is not None:
            self._held = frame
        return frame

    def _show_frame(self, show: _Show, now: float) -> np.ndarray | None:
        player = show.player
        if show.phase == "in":
            k = (now - show.started) / IN_S
            if k >= COVER_AT and show.clock is None:
                show.clock = now  # the replay starts moving under the leaving bands
            if k >= 1:
                show.phase = "play"
            else:
                base = None
                if k >= COVER_AT:
                    picture = player.take((now - show.clock) * show.speed)
                    if picture is not None:
                        show.first = picture
                    base = show.first
                return wipe_frame(base, show.size, k, leaving=False, slow=show.speed < 0.999)
        if show.phase == "play":
            clip_t = (now - (show.clock or now)) * show.speed
            picture = player.take(clip_t)
            ended = player.finished() and clip_t >= player.length + 0.02
            if picture is None and not ended:
                return None
            if picture is not None:
                show.first = None
                if show.badge is not None:
                    slide = 1 - _out_cubic((now - show.started - IN_S) / BADGE_IN_S)  # slides in from its edge
                    margin = round(show.size[1] * 0.04)
                    bw = show.badge.shape[1]
                    x = margin if show.badge_at == "top_left" else show.size[0] - bw - margin
                    x += round(slide * (bw + margin)) * (-1 if show.badge_at == "top_left" else 1)
                    blend(picture, show.badge, x, margin)
                progress_line(picture, clip_t / max(0.01, player.length if player.done else show.seconds))
                show.last = picture
            if ended:
                show.phase = "out"
                show.out_at = now
            elif picture is not None:
                return picture
        if show.phase == "out":
            k = (now - show.out_at) / OUT_S
            if k >= 1:
                show.phase = "done"
                return None
            return wipe_frame(show.last if k < COVER_AT else None, show.size, k, leaving=True)
        return None

    def _finish(self, show: _Show) -> None:
        if self._show is not show:
            return
        self._show = None
        show.player.stop()
        _remove_later(show.path)
        self._busy = False
        self.outbox.append(("ended", {"reason": show.reason}))

    # Sound (mixer thread) ---------------------------------------------------------------------------------

    def read_block(self, count: int) -> np.ndarray:
        show = self._show
        if show is None or show.clock is None or show.phase == "done" or not self.settings.get("replay_sound", True):
            return np.zeros((count, 2), dtype=np.float32)
        return show.player.read(count)


def _remove_later(path: Path | None) -> None:
    if path is None:
        return
    from sprigcast.core import workers

    def remove():
        for _ in range(20):  # the player may still have it open for a moment
            try:
                path.unlink(missing_ok=True)
                return
            except OSError:
                time.sleep(0.25)

    workers.run(remove, name="replay-tidy")


# Hotkeys, right-click, apps ------------------------------------------------------------------------------

from sprigcast.control.native import ApiError, register_action, send_event  # noqa: E402
from sprigcast.plugin_hooks import register_hotkeys, register_item_menu  # noqa: E402

_WATCH: dict[str, Any] = {}


def _replays(api) -> list:
    return [s for s in api.col.sources if s.kind == KIND]


def _watch(api) -> None:
    """Tell apps when replays start and end: a light timer on the UI thread, only while one is going."""
    from PySide6.QtCore import QTimer

    timer = _WATCH.get("timer")
    _WATCH["api"] = api
    if timer is None:
        timer = QTimer()
        timer.setInterval(150)
        timer.timeout.connect(_tell_apps)
        _WATCH["timer"] = timer
    if not timer.isActive():
        timer.start()


def _tell_apps() -> None:
    api = _WATCH.get("api")
    if api is None:
        return
    going = False
    for config in _replays(api):
        instance = api.host.source_instance(config.id)
        if instance is None or not hasattr(instance, "outbox"):
            continue
        while instance.outbox:
            what, details = instance.outbox.popleft()
            try:
                send_event(api, f"replay.{what}", {"source": config.name, "sourceId": config.id, **details})
            except Exception:
                log.exception("instant replay: telling apps failed")
        going = going or instance.busy
    if not going:
        _WATCH["timer"].stop()


def play(api, configs, seconds: float | None = None, speed: float | None = None) -> dict:
    if not configs:
        raise ApiError("not_allowed", "Add Instant replay to a bloom first (+ → Plugins → Instant replay).")
    started, notes = [], []
    for config in configs:
        instance = api.host.source_instance(config.id)
        note = instance.play(seconds, speed) if instance is not None and hasattr(instance, "play") \
            else "Instant replay isn't on air."
        if note is None:
            started.append(config.name)
        else:
            notes.append(note)
    if not started:
        raise ApiError("not_allowed", notes[0] if notes else "Instant replay isn't on air.")
    _watch(api)
    return {"playing": started}


def _pick(api, data) -> list:
    if data.get("source") is not None:
        config = api._source(data, "source")
        if config.kind != KIND:
            raise ApiError("bad_request", f"{config.name} isn't an Instant replay.")
        return [config]
    return _replays(api)


def _play_action(api, data):
    seconds, speed = data.get("seconds"), data.get("speed")
    for key, value in (("seconds", seconds), ("speed", speed)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ApiError("bad_request", f"{key} must be a number.")
    if seconds is not None and not 1 <= seconds <= 120:
        raise ApiError("bad_request", "seconds must be from 1 to 120.")
    if speed is not None and not 0.25 <= speed <= 1:
        raise ApiError("bad_request", "speed must be from 0.25 to 1.")
    return play(api, _pick(api, data), seconds, speed)


def _stop_action(api, data):
    stopped = []
    for config in _pick(api, data):
        instance = api.host.source_instance(config.id)
        if instance is not None and hasattr(instance, "cancel") and instance.cancel("stopped"):
            stopped.append(config.name)
    return {"stopped": stopped}


register_action("replay.play", "Play the last seconds back on stream (every Instant replay, or one).",
                {"source": "?name or id of an Instant replay (default: all)", "seconds": "?how far back, 1-120",
                 "speed": "?1 normal, 0.5 slow motion (0.25-1)"}, _play_action)
register_action("replay.stop", "End a replay that's playing (it wipes out).",
                {"source": "?name or id of an Instant replay (default: all)"}, _stop_action)


def _hotkeys(collection):
    found = [s for s in collection.sources if s.kind == KIND]
    if len(found) == 1:
        return [(f"replay:{found[0].id}", "Instant replay")]
    return [(f"replay:{s.id}", f"Instant replay: {s.name}") for s in found]


def _run_hotkey(api, hid):
    source_id = hid.split(":", 1)[1]
    config = next((s for s in api.col.sources if s.id == source_id), None)
    if config is None:
        return "That Instant replay is gone."
    try:
        play(api, [config])
    except ApiError as exc:
        return exc.message
    return None


register_hotkeys(_hotkeys, _run_hotkey)


def _menu(api, source, add):
    if source is None or source.kind != KIND:
        return
    add("Play replay", lambda: _run_hotkey(api, f"replay:{source.id}"))
    add("Stop replay", lambda: _stop_action(api, {"source": source.id}))


register_item_menu(_menu)
