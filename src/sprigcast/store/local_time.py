"""Local time: your own time (and date) on stream, so viewers know what time it is where you are.

Add Local time (+ → Plugins → Local time): it gets a sprig of its own with the clock in the top
right corner of your bloom, in every shape. Move or resize it like anything else.

Settings: Show date (adds e.g. "Sat, Nov 14" under the time), 24-hour, Seconds, Time zone (adds
e.g. "CST"), Text colour and Card colour (a colour ending in 00 is see-through: no card).

It's SprigCast's clock card (core/cards.py): a leaf-cut card with a sun by day and a moon by night. Its box
is the card, so a selection hugs it; the box is as wide as the widest time and date the settings can show,
so it never changes as the minutes go by.

It's light: the clock is only drawn again when the words on it change, so once a minute (once a
second with Seconds on). Between those it costs nothing.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import numpy as np

from sprigcast.core import cards, painted
from sprigcast.core.source import Source, register

PLUGIN = {"id": "local_time", "name": "Local time", "version": 4, "adds": "Source",
          "about": "Your local time, and the date if you like, in a small card on stream.",
          "new": "A Style tab: your own colors, font, text size and corners, and a custom look box."}

KIND = "local_time"
H = cards.H
W = 603  # how wide the card starts (its box follows the settings: card_width)
OLD_W = 640  # the box before version 3, with the card at its right
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


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


def daytime(now: datetime) -> bool:
    return 6 <= now.hour < 18


def card_words(clock: str, under: str, day: bool) -> cards.Words:
    return cards.Words(clock, under, badge="sun" if day else "moon")


def widest(settings: dict[str, Any]) -> list[cards.Words]:
    """The widest the card's words can be with these settings: two-digit hours, either half of the day,
    every weekday and month (its box is made this wide, so it stays put all day)."""
    seconds = bool(settings.get("seconds", False))
    clock = "00:00:00" if seconds else "00:00"
    clocks = [clock] if settings.get("hour_24", False) else [f"{clock} AM", f"{clock} PM"]
    unders = [""]
    if settings.get("show_date", True):
        unders = [f"{d}, {m} 00" for d in DAYS for m in MONTHS]
    if settings.get("time_zone", False):
        zone = zone_name()
        if zone:
            unders = ["  ·  ".join(u for u in (under, zone) if u) for under in unders]
    return [cards.Words(c, u) for c in clocks for u in unders]


def paint(clock: str, under: str, settings: dict[str, Any], detail: float = 1.0, width: int = W,
          day: bool = False):
    """The card as a QImage (premultiplied), width x H: the time with the date (and zone) under it."""
    return cards.paint(width, card_words(clock, under, day), detail=detail, look=cards.look_of(settings))


@register(KIND)
class LocalTimeSource(Source):
    label = "Local time"
    defaults = {"show_date": True, "hour_24": False, "seconds": False, "time_zone": False,
                "text_color": "#f2f7f3", "card_color": "#151c19e0", "accent_color": "#4fd88f", "font": "", "text_scale": 100,
                "corners": "leaf", "show_badge": True, "custom_look": ""}
    sleeps = True  # hidden, it does nothing; shown again, it draws the time as it is then
    keeps_picture = True  # (only if the time on the card changed meanwhile: its texture stays while it sleeps)
    sharp = True  # drawn at the size it's shown
    own_sprig = (W, H)
    own_sprig_corner = "top_right"
    card_box = (OLD_W, "right")  # the box it had before (main_window keeps the card where it was)
    own_sprig_height = 0.14  # starts readable: the card about a seventh of the canvas tall
    channel_order = painted.Frame.channel_order

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame = painted.Frame()
        self._card = painted.Aside(self._frame)  # painted on the pictures lane, not the render thread
        self._want = self._w = cards.card_width(widest(self.settings), cards.look_of(self.settings))
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return self._w, H  # the card on screen (a new width counts once its picture lands)

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._want = cards.card_width(widest(self.settings), cards.look_of(self.settings))  # only when the settings change, never per frame
        self._card.forget()  # draw it again with the new look

    def wake(self) -> None:
        pass  # the card is drawn again only if the time on it changed (keeps_picture)

    def sleep(self) -> None:
        self._frame.release()  # the texture has the picture

    def video_frame(self) -> np.ndarray | None:
        now = datetime.now()
        clock, under = words(now, self.settings)
        day = daytime(now)
        key = (self._want, clock, under, day, cards.look_of(self.settings).key, self.detail)
        # nothing changed: nothing drawn, nothing uploaded
        frame = self._card.picture(key, paint, clock, under, dict(self.settings), self.detail, self._want, day)
        if frame is not None:
            self._w = self._card.landed[0]
        return frame
