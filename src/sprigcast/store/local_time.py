"""Local time: your own time (and date) on stream, so viewers know what time it is where you are.

Add Local time (+ → Plugins → Local time): it gets a sprig of its own with the clock in the top
right corner of your bloom, in every shape. Move or resize it like anything else.

Settings: Show date (adds e.g. "Sat, Nov 14" under the time), 24-hour, Seconds, Time zone (adds
e.g. "CST"), Text colour and Card colour (a colour ending in 00 is see-through: no card).

It's light: the clock is only drawn again when the words on it change, so once a minute (once a
second with Seconds on). Between those it costs nothing.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

import numpy as np

from sprigcast.core import painted
from sprigcast.core.source import Source, register

PLUGIN = {"id": "local_time", "name": "Local time", "version": 2, "adds": "Source",
          "about": "Your local time, and the date if you like, in a small card on stream.",
          "new": "Stays crisp at any size, and starts bigger."}

KIND = "local_time"
W, H = 640, 220  # the card's box; scale it on the canvas like anything else
ACCENT = "#4fd88f"
MUTED = "#8b9a92"


def zone_name() -> str:
    """The time zone's short name: "CST". Windows gives "Central Standard Time", so take its initials."""
    name = time.strftime("%Z") or ""
    if len(name) > 5 and " " in name:
        name = "".join(word[0] for word in name.split() if word[:1].isalpha()).upper()
    return name


def words(now: datetime, settings: dict[str, Any]) -> tuple[str, str]:
    """(the time, the line under it) as the clock shows them."""
    seconds = bool(settings.get("seconds", False))
    if settings.get("hour_24", False):
        clock = now.strftime("%H:%M:%S" if seconds else "%H:%M")
    else:
        hour = now.hour % 12 or 12
        clock = f"{hour}:{now:%M}" + (f":{now:%S}" if seconds else "") + (" AM" if now.hour < 12 else " PM")
    under = []
    if settings.get("show_date", True):
        under.append(f"{now:%a}, {now:%b} {now.day}")
    if settings.get("time_zone", False):
        zone = zone_name()
        if zone:
            under.append(zone)
    return clock, "  ·  ".join(under)


def _colour(text: str, fallback: str):
    from PySide6.QtGui import QColor

    text = str(text or "").strip()
    if text.startswith("#") and len(text) == 9:  # #rrggbbaa
        c = QColor(text[:7])
        c.setAlpha(int(text[7:], 16))
        return c if c.isValid() else QColor(fallback)
    c = QColor(text)
    return c if c.isValid() else QColor(fallback)


def paint(clock: str, under: str, settings: dict[str, Any], detail: float = 1.0):
    """The card as a QImage (premultiplied), W x H. The card hugs the words and sits at the right of
    the box, so it grows to the left and stays tucked in the corner. detail: pixels drawn per box unit."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetricsF

    image, p = painted.canvas(W, H, detail)
    big = QFont()
    big.setFamilies(["Nunito", "Segoe UI Variable Display", "Segoe UI", "Inter", "Arial"])
    big.setWeight(QFont.Weight.Bold)
    big.setPixelSize(84 if under else 100)
    small = QFont(big)
    small.setPixelSize(36)
    small.setWeight(QFont.Weight.DemiBold)
    text_w = QFontMetricsF(big).horizontalAdvance(re.sub(r"\d", "0", clock))  # same width all minute long
    if under:
        text_w = max(text_w, QFontMetricsF(small).horizontalAdvance(under) + 4)
    pad, dot_room, right = 14, 70, 40
    card_w = min(W - 2 * pad, dot_room + text_w + right)
    x0 = W - pad - card_w
    card = _colour(settings.get("card_color"), "#151c19e0")
    clock_mid = pad + 18 + 52 if under else H / 2
    if card.alpha():
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(card)
        p.drawRoundedRect(QRectF(x0, pad, card_w, H - 2 * pad), 40, 40)
        p.setBrush(QColor(ACCENT))  # the little sprig dot, like the logo's
        p.drawEllipse(QRectF(x0 + 34, clock_mid - 9, 18, 18))
    text = _colour(settings.get("text_color"), "#f2f7f3")
    left = x0 + dot_room
    flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    p.setFont(big)
    p.setPen(text)
    if under:
        p.drawText(QRectF(left, clock_mid - 52, W, 104), flags, clock)
        p.setFont(small)
        muted = QColor(text)
        muted.setAlphaF(muted.alphaF() * 0.62)
        p.setPen(muted)
        p.drawText(QRectF(left + 4, pad + 118, W, 52), flags, under)
    else:
        p.drawText(QRectF(left, 0, W, H), flags, clock)
    p.end()
    return image


@register(KIND)
class LocalTimeSource(Source):
    label = "Local time"
    defaults = {"show_date": True, "hour_24": False, "seconds": False, "time_zone": False,
                "text_color": "#f2f7f3", "card_color": "#151c19e0"}
    sleeps = True  # hidden, it does nothing; shown again, it draws the time as it is then
    keeps_picture = True  # (only if the time on the card changed meanwhile: its texture stays while it sleeps)
    sharp = True  # drawn at the size it's shown
    own_sprig = (W, H)
    own_sprig_corner = "top_right"
    own_sprig_height = 0.14  # starts readable: the card about a seventh of the canvas tall
    channel_order = painted.Frame.channel_order

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame = painted.Frame()
        self._shown: tuple | None = None  # what's on the card now
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return W, H

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._shown = None  # draw it again with the new look

    def wake(self) -> None:
        pass  # the card is drawn again only if the time on it changed (keeps_picture)

    def sleep(self) -> None:
        self._frame.release()  # the texture has the picture

    def video_frame(self) -> np.ndarray | None:
        clock, under = words(datetime.now(), self.settings)
        key = (clock, under, self.settings.get("text_color"), self.settings.get("card_color"), self.detail)
        if key == self._shown:
            return None  # nothing changed: nothing drawn, nothing uploaded
        self._shown = key
        return self._frame(paint(clock, under, self.settings, self.detail))
