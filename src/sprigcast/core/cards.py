"""SprigCast's clock card: the on-brand card that Countdown, Stream timer and Local time draw on stream.

One look for all three, so they sit together: a dark leaf-cut card (big round corners top left and bottom
right, small ones on the other two, like a leaf), a thin green rim so it reads on any background, a round
badge on the left (a sprig, or a sun or a moon for the clock), the time in Nunito, and small words above or
under it. A countdown can show a vine along the bottom that shrinks as the time runs out.

The card fills its box exactly (card_width): the box is as wide as the widest words the card can show with
its settings (every digit counted as a 0), so a selection in Build hugs the card, and the box only changes
when the settings do, never as the seconds tick. The words sit in the middle of the room they have.

Its Style tab (core/style.py) changes the colours, font, text size, corners (leaf by default), border and
shadow, and hides the badge, the small words or the vine (show_badge, or "hide: badge" in a custom look).

Only painted when the words on it change (Aside, on the pictures lane), so it costs nothing between those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from sprigcast.core import style

H = 220  # every card's box is this tall (box units); scale it on the canvas like anything else
EDGE = 4  # room around the card for its rim's soft edge
BADGE = 96  # the round badge's size
BADGE_LEFT = 28  # card edge to badge
GAP = 28  # badge to words
RIGHT = 50  # words to the card's right edge
WORDS_LEFT = EDGE + BADGE_LEFT + BADGE + GAP
MOST_W = 1400  # a very long label or Done text is cut to fit this
LEAST_W = 360

ACCENT = "#4fd88f"
LEAF_DARK = "#07130d"
CARD = "#151c19e8"
TEXT = "#f2f7f3"
SOFT = "#bff3d6"  # the small words: a pale leaf green
QUIET = "#8b9a92"
BADGE_FILL = "#1d3428"
BADGE_QUIET = "#222c27"
SUN = "#f2cf63"
MOON = "#dfeee6"

FAMILIES = ["Nunito", "Segoe UI Variable Display", "Segoe UI", "Inter", "Arial"]
LOOK_BASE = {"text_color": TEXT, "card_color": CARD, "accent_color": ACCENT, "corners": "leaf"}
PLAIN = style.resolve({}, LOOK_BASE)
SMALL_PARTS = ("label", "date", "line")  # what "hide:" calls the small words


def look_of(settings: dict[str, Any]) -> style.Style:
    """The card's look from a plugin's settings (cached: free to ask every frame)."""
    return style.resolve(settings, LOOK_BASE)


def _badge_shown(look: style.Style) -> bool:
    return look.shows("badge", default=look.shows("dot"))


def _small_shown(look: style.Style) -> bool:
    return not any(part in look.hidden for part in SMALL_PARTS)


def _words_left(look: style.Style) -> float:
    return WORDS_LEFT if _badge_shown(look) else EDGE + RIGHT
BIG_ALONE, BIG_WITH_LINE, SMALL = 100, 86, 34


def clock_text(seconds: float) -> str:
    """1:02:03 from an hour on, 4:05 below it."""
    s = max(0, int(seconds))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def until_time(text: str, now: datetime) -> datetime | None:
    """The moment "8:30 PM", "8pm" or "20:30" means: today, or tomorrow if that was over 12 hours ago."""
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:.](\d{2}))?\s*([ap])?\.?\s*(?:m\.?)?\s*", str(text or ""), re.I)
    if not m or not (m[2] or m[3]):  # a bare "8" could be morning or night: say which
        return None
    hour, minute = int(m[1]), int(m[2] or 0)
    if m[3]:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if m[3].lower() == "p" else 0)
    if hour > 23 or minute > 59:
        return None
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if (now - target).total_seconds() > 12 * 3600:
        target += timedelta(days=1)
    return target


@dataclass(frozen=True)
class Words:
    big: str  # the time
    small: str = ""  # a label above it, or a line under it
    small_on_top: bool = False
    badge: str = "sprig"  # "sprig", "sun", "moon", "viewers" (an eye), "drop", "cup", "clip", "hourglass",
    # "bubble", "calendar", "gamepad", "pin" or "tag";
    # "sprig_quiet", "viewers_quiet", "cup_quiet"...: the same in grey
    vine: float | None = None  # a countdown's share of time left, 0..1 (None: no vine)
    shrink: float = 1.0  # card_width: the big words may be drawn this much smaller to fit (a Done text)
    openness: float = 1.0  # the "viewers" eye: 1 open, 0 shut (Live count blinks it now and then)
    art: str = ""  # an SVG file drawn where the badge goes, instead of it (Socials: the platform's icon)


def _fonts(look: style.Style = PLAIN):
    from PySide6.QtGui import QFont

    big = QFont()
    big.setFamilies(look.families())
    big.setWeight(QFont.Weight.ExtraBold)
    small = QFont(big)
    small.setWeight(QFont.Weight.Bold)
    small.setPixelSize(look.px(SMALL))
    small.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
    return big, small


def _big_size(words: Words, look: style.Style = PLAIN) -> int:
    """The time's size: smaller with a line or vine beside it, and never taller than the card has room for."""
    size = look.px(BIG_WITH_LINE if (words.small or words.vine is not None) else BIG_ALONE)
    return min(size, 130 if (words.small or words.vine is not None) else 150)


def even(text: str) -> str:
    """Every digit as a 0, so the width the words need stays the same while the time ticks."""
    return re.sub(r"\d", "0", text)


def card_width(widest: list[Words], look: style.Style | None = None) -> int:
    """The box's width: enough for the widest of these (the longest each line can be, digits as 0)."""
    from sprigcast.core import painted
    from PySide6.QtGui import QFontMetricsF

    look = look or PLAIN
    painted.brand_fonts()
    big, small = _fonts(look)
    need = 0.0
    for words in widest:
        big.setPixelSize(max(1, round(_big_size(words, look) * words.shrink)))
        need = max(need, QFontMetricsF(big).horizontalAdvance(even(words.big)))
        if words.small and _small_shown(look):
            need = max(need, QFontMetricsF(small).horizontalAdvance(words.small) + 2)
    return int(max(LEAST_W, min(MOST_W, round(_words_left(look) + need + RIGHT + EDGE))))


def card_path(x: float, y: float, w: float, h: float, look: style.Style):
    """The card's outline in its look: leaf-cut (its own), round, square, or a custom look's corner size."""
    from PySide6.QtCore import QRectF

    if look.corners == "leaf" and look.radius is None:
        return leaf_card_path(x, y, w, h)
    return style.card_path(QRectF(x, y, w, h), h * 0.3, look)


def leaf_card_path(x: float, y: float, w: float, h: float):
    """The card's outline: big round corners top left and bottom right, small ones top right and bottom left."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QPainterPath

    big, small = min(h * 0.34, w / 2), min(h * 0.11, w / 2)
    path = QPainterPath()
    path.moveTo(x + big, y)
    path.lineTo(x + w - small, y)
    path.arcTo(QRectF(x + w - 2 * small, y, 2 * small, 2 * small), 90, -90)
    path.lineTo(x + w, y + h - big)
    path.arcTo(QRectF(x + w - 2 * big, y + h - 2 * big, 2 * big, 2 * big), 0, -90)
    path.lineTo(x + small, y + h)
    path.arcTo(QRectF(x, y + h - 2 * small, 2 * small, 2 * small), 270, -90)
    path.lineTo(x, y + big)
    path.arcTo(QRectF(x, y, 2 * big, 2 * big), 180, -90)
    path.closeSubpath()
    return path


def _leaf(base, tip, bulge: float):
    """A leaf from base to tip, fattest in the middle; bulge > 0 bows it to the left of base -> tip."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPainterPath

    dx, dy = tip.x() - base.x(), tip.y() - base.y()
    nx, ny = -dy, dx
    path = QPainterPath(base)
    path.cubicTo(QPointF(base.x() + dx * 0.25 + nx * bulge, base.y() + dy * 0.25 + ny * bulge),
                 QPointF(base.x() + dx * 0.8 + nx * bulge * 0.9, base.y() + dy * 0.8 + ny * bulge * 0.9), tip)
    path.cubicTo(QPointF(base.x() + dx * 0.75 - nx * bulge * 0.55, base.y() + dy * 0.75 - ny * bulge * 0.55),
                 QPointF(base.x() + dx * 0.2 - nx * bulge * 0.55, base.y() + dy * 0.2 - ny * bulge * 0.55), base)
    return path


def _badge(p, cx: float, cy: float, kind: str, accent: str = ACCENT, openness: float = 1.0) -> None:
    """The round badge: a sprig (SprigCast's mark), or a sun by day and a moon by night for the clock."""
    import math

    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QPainterPath, QPen

    r = BADGE / 2
    quiet = kind.endswith("_quiet")
    if quiet and kind[:-6] in _GLYPHS:
        kind = kind[:-6]
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(BADGE_QUIET if quiet else BADGE_FILL))
    p.drawEllipse(QPointF(cx, cy), r, r)
    if kind == "sun":
        p.setBrush(QColor(SUN))
        p.drawEllipse(QPointF(cx, cy), r * 0.3, r * 0.3)
        p.setPen(QPen(QColor(SUN), r * 0.09, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for i in range(8):
            a = math.pi * 2 * i / 8
            p.drawLine(QPointF(cx + math.cos(a) * r * 0.45, cy + math.sin(a) * r * 0.45),
                       QPointF(cx + math.cos(a) * r * 0.62, cy + math.sin(a) * r * 0.62))
        return
    if kind == "moon":
        moon = QPainterPath()
        moon.addEllipse(QPointF(cx - r * 0.04, cy), r * 0.42, r * 0.42)
        bite = QPainterPath()
        bite.addEllipse(QPointF(cx + r * 0.2, cy - r * 0.16), r * 0.36, r * 0.36)
        p.setBrush(QColor(MOON))
        p.drawPath(moon.subtracted(bite))
        p.drawEllipse(QPointF(cx + r * 0.42, cy + r * 0.3), r * 0.05, r * 0.05)  # a star
        return
    if kind in ("viewers", "viewers_quiet"):  # Live count: an eye, green while live (it blinks: openness)
        colour = QColor(QUIET) if quiet else style.qcolor(accent)
        open_ = max(0.0, min(1.0, openness))
        w, h = r * 0.62, r * 0.36 * max(open_, 0.04)  # half the eye's width and height
        eye = QPainterPath(QPointF(cx - w, cy))
        eye.quadTo(QPointF(cx, cy - h * 2), QPointF(cx + w, cy))
        eye.quadTo(QPointF(cx, cy + h * 2), QPointF(cx - w, cy))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(colour, r * 0.11, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        if open_ < 0.3:  # shut: one lid, curved like a closed eye
            lid = QPainterPath(QPointF(cx - w, cy))
            lid.quadTo(QPointF(cx, cy + r * 0.3), QPointF(cx + w, cy))
            p.drawPath(lid)
            return
        p.drawPath(eye)
        p.save()
        p.setClipPath(eye)  # the iris stays inside the lids as they close
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(colour)
        p.drawEllipse(QPointF(cx, cy), r * 0.25, r * 0.25)  # the iris
        p.setBrush(QColor(BADGE_QUIET if quiet else BADGE_FILL))
        p.drawEllipse(QPointF(cx + r * 0.08, cy - r * 0.08), r * 0.08, r * 0.08)  # its glint
        p.restore()
        return
    if kind in _GLYPHS:  # the store plugins' marks: Hydration, Break & End Guard, Clip player, Ad break
        _GLYPHS[kind](p, cx, cy, r, QColor(QUIET) if quiet else style.qcolor(accent))
        return
    colour = QColor(QUIET) if quiet else style.qcolor(accent)
    k = r * 1.5 / 24  # the sprig is drawn on a 24 grid, like the app's icon
    ox, oy = cx - 12 * k, cy - 12.6 * k

    def at(x: float, y: float) -> QPointF:
        return QPointF(ox + x * k, oy + y * k)

    stem = QPainterPath(at(12, 21))
    stem.cubicTo(at(12, 16), at(11, 12), at(12, 7.5))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(colour, 2.0 * k, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawPath(stem)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    p.drawPath(_leaf(at(12, 12.5), at(20.5, 6), 0.42))
    p.drawPath(_leaf(at(11.7, 16), at(3.5, 11), -0.42))
    p.drawEllipse(at(12, 5.2), 1.9 * k, 1.9 * k)  # the bud, like the logo's dot


def _pen(colour, r):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPen

    return QPen(colour, r * 0.11, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)


def _drop(p, cx, cy, r, colour) -> None:
    """A water drop (Hydration)."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QPainterPath

    d = QPainterPath(QPointF(cx, cy - r * 0.5))
    d.cubicTo(QPointF(cx + r * 0.1, cy - r * 0.3), QPointF(cx + r * 0.4, cy), QPointF(cx + r * 0.4, cy + r * 0.15))
    d.cubicTo(QPointF(cx + r * 0.4, cy + r * 0.4), QPointF(cx + r * 0.2, cy + r * 0.5), QPointF(cx, cy + r * 0.5))
    d.cubicTo(QPointF(cx - r * 0.2, cy + r * 0.5), QPointF(cx - r * 0.4, cy + r * 0.4), QPointF(cx - r * 0.4, cy + r * 0.15))
    d.cubicTo(QPointF(cx - r * 0.4, cy), QPointF(cx - r * 0.1, cy - r * 0.3), QPointF(cx, cy - r * 0.5))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    p.drawPath(d)
    p.setBrush(QColor(BADGE_FILL))
    p.drawEllipse(QPointF(cx - r * 0.15, cy + r * 0.18), r * 0.08, r * 0.12)  # its shine


def _cup(p, cx, cy, r, colour) -> None:
    """A steaming cup (Break & End Guard)."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QPainterPath

    p.setPen(_pen(colour, r))
    p.setBrush(Qt.BrushStyle.NoBrush)
    cup = QPainterPath(QPointF(cx - r * 0.42, cy - r * 0.12))
    cup.lineTo(QPointF(cx + r * 0.28, cy - r * 0.12))
    cup.cubicTo(QPointF(cx + r * 0.28, cy + r * 0.3), QPointF(cx + r * 0.15, cy + r * 0.42),
                QPointF(cx - r * 0.07, cy + r * 0.42))
    cup.cubicTo(QPointF(cx - r * 0.3, cy + r * 0.42), QPointF(cx - r * 0.42, cy + r * 0.3),
                QPointF(cx - r * 0.42, cy - r * 0.12))
    p.drawPath(cup)
    p.drawArc(QRectF(cx + r * 0.16, cy - r * 0.04, r * 0.3, r * 0.3), -90 * 16, 180 * 16)  # the handle
    for dx in (-0.22, 0.08):  # steam
        s = QPainterPath(QPointF(cx + r * dx, cy - r * 0.28))
        s.cubicTo(QPointF(cx + r * (dx - 0.08), cy - r * 0.38), QPointF(cx + r * (dx + 0.08), cy - r * 0.46),
                  QPointF(cx + r * dx, cy - r * 0.56))
        p.drawPath(s)


def _clip(p, cx, cy, r, colour) -> None:
    """A screen with a play mark (Clip player)."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QPainterPath

    p.setPen(_pen(colour, r))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(cx - r * 0.48, cy - r * 0.32, r * 0.96, r * 0.7), r * 0.12, r * 0.12)
    t = QPainterPath(QPointF(cx - r * 0.1, cy - r * 0.14))
    t.lineTo(QPointF(cx + r * 0.2, cy + r * 0.03))
    t.lineTo(QPointF(cx - r * 0.1, cy + r * 0.2))
    t.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    p.drawPath(t)


def _hourglass(p, cx, cy, r, colour) -> None:
    """An hourglass (Ad break countdown)."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QPainterPath

    p.setPen(_pen(colour, r))
    p.setBrush(Qt.BrushStyle.NoBrush)
    h = QPainterPath(QPointF(cx - r * 0.3, cy - r * 0.45))
    h.lineTo(QPointF(cx + r * 0.3, cy - r * 0.45))
    h.lineTo(QPointF(cx - r * 0.3, cy + r * 0.45))
    h.lineTo(QPointF(cx + r * 0.3, cy + r * 0.45))
    h.closeSubpath()
    p.drawPath(h)
    sand = QPainterPath(QPointF(cx - r * 0.16, cy + r * 0.38))
    sand.lineTo(QPointF(cx + r * 0.16, cy + r * 0.38))
    sand.lineTo(QPointF(cx, cy + r * 0.16))
    sand.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    p.drawPath(sand)


def _bubble(p, cx, cy, r, colour) -> None:
    """A chat bubble (Chat word counter)."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QPainterPath

    p.setPen(_pen(colour, r))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(cx - r * 0.45, cy - r * 0.36, r * 0.9, r * 0.6), r * 0.18, r * 0.18)
    tail = QPainterPath(QPointF(cx - r * 0.2, cy + r * 0.24))
    tail.lineTo(QPointF(cx - r * 0.28, cy + r * 0.44))
    tail.lineTo(QPointF(cx, cy + r * 0.24))
    p.drawPath(tail)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    for dx in (-0.2, 0.0, 0.2):
        p.drawEllipse(QPointF(cx + r * dx, cy - r * 0.06), r * 0.06, r * 0.06)


def _calendar(p, cx, cy, r, colour) -> None:
    """A calendar page (Next stream)."""
    from PySide6.QtCore import QPointF, QRectF, Qt

    p.setPen(_pen(colour, r))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(cx - r * 0.42, cy - r * 0.3, r * 0.84, r * 0.72), r * 0.1, r * 0.1)
    p.drawLine(QPointF(cx - r * 0.42, cy - r * 0.08), QPointF(cx + r * 0.42, cy - r * 0.08))
    for dx in (-0.2, 0.2):
        p.drawLine(QPointF(cx + r * dx, cy - r * 0.44), QPointF(cx + r * dx, cy - r * 0.24))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    p.drawEllipse(QPointF(cx + r * 0.14, cy + r * 0.16), r * 0.09, r * 0.09)


def _gamepad(p, cx, cy, r, colour) -> None:
    """A game controller (Playing now)."""
    from PySide6.QtCore import QPointF, QRectF, Qt

    p.setPen(_pen(colour, r))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(cx - r * 0.5, cy - r * 0.26, r * 1.0, r * 0.56), r * 0.26, r * 0.26)
    p.drawLine(QPointF(cx - r * 0.3, cy + r * 0.02), QPointF(cx - r * 0.1, cy + r * 0.02))
    p.drawLine(QPointF(cx - r * 0.2, cy - r * 0.08), QPointF(cx - r * 0.2, cy + r * 0.12))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    p.drawEllipse(QPointF(cx + r * 0.2, cy - r * 0.04), r * 0.06, r * 0.06)
    p.drawEllipse(QPointF(cx + r * 0.32, cy + r * 0.08), r * 0.06, r * 0.06)


def _pin(p, cx, cy, r, colour) -> None:
    """A pin (Pinned message)."""
    from PySide6.QtCore import QPointF, Qt

    p.setPen(_pen(colour, r))
    p.drawLine(QPointF(cx, cy + r * 0.5), QPointF(cx, cy + r * 0.12))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    p.drawEllipse(QPointF(cx, cy - r * 0.18), r * 0.3, r * 0.3)


def _tag(p, cx, cy, r, colour) -> None:
    """A name tag (Name tag)."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QPainterPath

    p.setPen(_pen(colour, r))
    p.setBrush(Qt.BrushStyle.NoBrush)
    t = QPainterPath(QPointF(cx - r * 0.45, cy - r * 0.3))
    for x, y in ((0.15, -0.3), (0.45, 0.0), (0.15, 0.3), (-0.45, 0.3)):
        t.lineTo(QPointF(cx + r * x, cy + r * y))
    t.closeSubpath()
    p.drawPath(t)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    p.drawEllipse(QPointF(cx + r * 0.12, cy), r * 0.07, r * 0.07)


_GLYPHS = {"drop": _drop, "cup": _cup, "clip": _clip, "hourglass": _hourglass, "bubble": _bubble,
           "calendar": _calendar, "gamepad": _gamepad, "pin": _pin, "tag": _tag}


def colour(text, fallback: str):
    """A colour from settings: #rrggbb, #rrggbbaa (aa 00 is see-through) or a name; else the fallback."""
    from PySide6.QtGui import QColor

    def read(text: str):
        if text.startswith("#") and len(text) == 9:  # #rrggbbaa (Qt alone would read it as #aarrggbb)
            c = QColor(text[:7])
            try:
                c.setAlpha(int(text[7:], 16))
            except ValueError:
                return None
            return c if c.isValid() else None
        c = QColor(text)
        return c if c.isValid() else None

    return read(str(text or "").strip()) or read(fallback) or QColor(0, 0, 0, 0)


def paint(width: int, words: Words, text_colour: str | None = None, card_colour: str | None = None,
          detail: float = 1.0, look: style.Style | None = None):
    """The card as a premultiplied QImage, width x H box units drawn `detail` pixels per unit. With a look
    (look_of(settings)), its colours, font, size, corners and parts win over text_colour and card_colour."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFontMetricsF, QLinearGradient, QPen

    from sprigcast.core import painted

    image, p = painted.canvas(width, H, detail)
    x, y, w, h = EDGE, EDGE, width - 2 * EDGE, H - 2 * EDGE
    if look is None:
        look = PLAIN
        card, ink = colour(card_colour, CARD), colour(text_colour, TEXT)
    else:
        card, ink = style.qcolor(look.card), style.qcolor(look.text)
    accent = look.accent
    shape = card_path(x, y, w, h, look)
    if look.shadow and card.alpha():
        for dy, a in ((3, 40), (1.5, 50)):  # a soft shadow in the card's own room (its box hugs it)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, int(a * card.alphaF())))
            p.drawPath(shape.translated(0, dy))
    if card.alpha():
        top = QColor(card).lighter(122)
        top.setAlpha(card.alpha())
        fill = QLinearGradient(0, y, 0, y + h)
        fill.setColorAt(0.0, top)
        fill.setColorAt(1.0, card)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawPath(shape)
    if look.border_width:
        p.setPen(QPen(style.qcolor(look.border_color), look.border_width))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(shape)
    elif card.alpha():
        rim = style.qcolor(accent)
        rim.setAlphaF(0.34 * card.alphaF())
        p.setPen(QPen(rim, 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(shape)
    if card.alpha() and _badge_shown(look):
        if words.art:
            from pathlib import Path

            side = BADGE * 1.05
            cx, cy = x + BADGE_LEFT + BADGE / 2, y + h / 2
            painted.draw(p, Path(words.art), QRectF(cx - side / 2, cy - side / 2, side, side), detail)
        else:
            _badge(p, x + BADGE_LEFT + BADGE / 2, y + h / 2, words.badge, accent, words.openness)
    if not _small_shown(look):
        words = replace(words, small="")
    if not look.shows("vine"):
        words = replace(words, vine=None)

    big, small = _fonts(look)
    big.setPixelSize(_big_size(words, look))
    words_left = _words_left(look)
    room = max(10.0, width - words_left - RIGHT - EDGE)
    big_m = QFontMetricsF(big)
    text = words.big
    if big_m.horizontalAdvance(text) > room:  # a long Done text: smaller, then cut with "…"
        big.setPixelSize(max(40, int(big.pixelSize() * room / big_m.horizontalAdvance(text))))
        # A font isn't exactly as wide as its size says: step down until it fits, so "Starting now" isn't cut to
        # "Starting no…" by a pixel (PC check 2026-11-19). Only at the smallest size is it cut.
        while big.pixelSize() > 40 and QFontMetricsF(big).horizontalAdvance(text) > room:
            big.setPixelSize(big.pixelSize() - 1)
        big_m = QFontMetricsF(big)
        text = big_m.elidedText(text, Qt.TextElideMode.ElideRight, room)
    small_m = QFontMetricsF(small)
    line = small_m.elidedText(words.small, Qt.TextElideMode.ElideRight, room) if words.small else ""
    big_w = big_m.horizontalAdvance(even(text)) if any(c.isdigit() for c in text) else big_m.horizontalAdvance(text)
    left = words_left + max(0.0, (room - max(big_w, small_m.horizontalAdvance(line) if line else 0.0)) / 2)

    # Lines from the top: [small] big [vine] or big [small], centred as a block in the card.
    cap = big_m.capHeight()
    small_h = small_m.capHeight() if line else 0.0
    vine_h = 12.0 if words.vine is not None else 0.0
    gap = 26.0 * min(1.0, look.scale)
    block = cap + (small_h + gap if line else 0.0) + (vine_h + gap if vine_h else 0.0)
    top = y + (h - block) / 2
    soft = QColor(SOFT) if look.text == PLAIN.text else QColor(ink)  # their own text colour: the line in it too
    soft.setAlphaF(0.82 * ink.alphaF())
    if line and words.small_on_top:
        p.setFont(small)
        p.setPen(soft)
        p.drawText(QPointF(left + 2, top + small_h), line)
        top += small_h + gap
    p.setFont(big)
    p.setPen(ink)
    p.drawText(QPointF(left, top + cap), text)
    top += cap + gap
    if line and not words.small_on_top:
        p.setFont(small)
        p.setPen(soft)
        p.drawText(QPointF(left + 2, top + small_h), line)
        top += small_h + gap
    if words.vine is not None:
        _vine(p, QRectF(words_left + 2, top, room - 2, vine_h), words.vine, ink, accent)
    p.end()
    return image


def _vine(p, rect, share: float, ink, accent: str = ACCENT) -> None:
    """A countdown's vine: a faint track, the green part as long as the time left, a leaf at its tip."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor

    share = max(0.0, min(1.0, share))
    track = QColor(ink)
    track.setAlphaF(0.14 * ink.alphaF())
    r = rect.height() / 2
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(track)
    p.drawRoundedRect(rect, r, r)
    if share <= 0:
        return
    grown = QRectF(rect.x(), rect.y(), max(rect.height(), rect.width() * share), rect.height())
    p.setBrush(style.qcolor(accent))
    p.drawRoundedRect(grown, r, r)
    tip = QPointF(grown.right() - r * 0.4, grown.center().y())
    p.drawPath(_leaf(tip, QPointF(tip.x() + r * 2.6, tip.y() - r * 2.6), 0.42))
