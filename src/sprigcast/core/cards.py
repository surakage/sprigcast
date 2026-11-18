"""SprigCast's clock card: the on-brand card that Countdown, Stream timer and Local time draw on stream.

One look for all three, so they sit together: a dark leaf-cut card (big round corners top left and bottom
right, small ones on the other two, like a leaf), a thin green rim so it reads on any background, a round
badge on the left (a sprig, or a sun or a moon for the clock), the time in Nunito, and small words above or
under it. A countdown can show a vine along the bottom that shrinks as the time runs out.

The card fills its box exactly (card_width): the box is as wide as the widest words the card can show with
its settings (every digit counted as a 0), so a selection in Build hugs the card, and the box only changes
when the settings do, never as the seconds tick. The words sit in the middle of the room they have.

Only painted when the words on it change (Aside, on the pictures lane), so it costs nothing between those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

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
    badge: str = "sprig"  # "sprig", "sprig_quiet", "sun" or "moon"
    vine: float | None = None  # a countdown's share of time left, 0..1 (None: no vine)
    shrink: float = 1.0  # card_width: the big words may be drawn this much smaller to fit (a Done text)


def _fonts():
    from PySide6.QtGui import QFont

    big = QFont()
    big.setFamilies(FAMILIES)
    big.setWeight(QFont.Weight.ExtraBold)
    small = QFont(big)
    small.setWeight(QFont.Weight.Bold)
    small.setPixelSize(SMALL)
    small.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
    return big, small


def _big_size(words: Words) -> int:
    return BIG_WITH_LINE if (words.small or words.vine is not None) else BIG_ALONE


def even(text: str) -> str:
    """Every digit as a 0, so the width the words need stays the same while the time ticks."""
    return re.sub(r"\d", "0", text)


def card_width(widest: list[Words]) -> int:
    """The box's width: enough for the widest of these (the longest each line can be, digits as 0)."""
    from sprigcast.core import painted
    from PySide6.QtGui import QFontMetricsF

    painted.brand_fonts()
    big, small = _fonts()
    need = 0.0
    for words in widest:
        big.setPixelSize(max(1, round(_big_size(words) * words.shrink)))
        need = max(need, QFontMetricsF(big).horizontalAdvance(even(words.big)))
        if words.small:
            need = max(need, QFontMetricsF(small).horizontalAdvance(words.small) + 2)
    return int(max(LEAST_W, min(MOST_W, round(WORDS_LEFT + need + RIGHT + EDGE))))


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


def _badge(p, cx: float, cy: float, kind: str) -> None:
    """The round badge: a sprig (SprigCast's mark), or a sun by day and a moon by night for the clock."""
    import math

    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor, QPainterPath, QPen

    r = BADGE / 2
    quiet = kind == "sprig_quiet"
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
    colour = QColor(QUIET if quiet else ACCENT)
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
          detail: float = 1.0):
    """The card as a premultiplied QImage, width x H box units drawn `detail` pixels per unit."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFontMetricsF, QLinearGradient, QPen

    from sprigcast.core import painted

    image, p = painted.canvas(width, H, detail)
    x, y, w, h = EDGE, EDGE, width - 2 * EDGE, H - 2 * EDGE
    card = colour(card_colour, CARD)
    ink = colour(text_colour, TEXT)
    shape = leaf_card_path(x, y, w, h)
    if card.alpha():
        top = QColor(card).lighter(122)
        top.setAlpha(card.alpha())
        fill = QLinearGradient(0, y, 0, y + h)
        fill.setColorAt(0.0, top)
        fill.setColorAt(1.0, card)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill)
        p.drawPath(shape)
        rim = QColor(ACCENT)
        rim.setAlphaF(0.34 * card.alphaF())
        p.setPen(QPen(rim, 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(shape)
        _badge(p, x + BADGE_LEFT + BADGE / 2, y + h / 2, words.badge)

    big, small = _fonts()
    big.setPixelSize(_big_size(words))
    room = max(10.0, width - WORDS_LEFT - RIGHT - EDGE)
    big_m = QFontMetricsF(big)
    text = words.big
    if big_m.horizontalAdvance(text) > room:  # a long Done text: smaller, then cut with "…"
        big.setPixelSize(max(40, int(big.pixelSize() * room / big_m.horizontalAdvance(text))))
        big_m = QFontMetricsF(big)
        text = big_m.elidedText(text, Qt.TextElideMode.ElideRight, room)
    small_m = QFontMetricsF(small)
    line = small_m.elidedText(words.small, Qt.TextElideMode.ElideRight, room) if words.small else ""
    big_w = big_m.horizontalAdvance(even(text)) if any(c.isdigit() for c in text) else big_m.horizontalAdvance(text)
    left = WORDS_LEFT + max(0.0, (room - max(big_w, small_m.horizontalAdvance(line) if line else 0.0)) / 2)

    # Lines from the top: [small] big [vine] or big [small], centred as a block in the card.
    cap = big_m.capHeight()
    small_h = small_m.capHeight() if line else 0.0
    vine_h = 12.0 if words.vine is not None else 0.0
    gap = 26.0
    block = cap + (small_h + gap if line else 0.0) + (vine_h + gap if vine_h else 0.0)
    top = y + (h - block) / 2
    soft = QColor(SOFT)
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
        _vine(p, QRectF(WORDS_LEFT + 2, top, room - 2, vine_h), words.vine, ink)
    p.end()
    return image


def _vine(p, rect, share: float, ink) -> None:
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
    p.setBrush(QColor(ACCENT))
    p.drawRoundedRect(grown, r, r)
    tip = QPointF(grown.right() - r * 0.4, grown.center().y())
    p.drawPath(_leaf(tip, QPointF(tip.x() + r * 2.6, tip.y() - r * 2.6), 0.42))
