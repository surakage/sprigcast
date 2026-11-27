"""Next stream: when you're on next, in a small card for your Starting and Ending blooms.

Add Next stream (+ → Plugins → Next stream): it gets a sprig of its own with the card at the bottom right of your
bloom. When: a day and a time, "Friday 7 PM", "Fri 19:30" or just "8:30 PM" (the next time it's that). The card
shows it ("Friday 7 PM") under Label ("Next stream"), with how soon it is (Show how soon: "today", "tomorrow",
"in 3 days", "in 2 hours"). Once that time has passed it moves on to the same day next week, so a weekly stream
needs setting once.

It's SprigCast's clock card with a calendar, drawn only when the words on it change.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from sprigcast.core import cards, painted
from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

PLUGIN = {"id": "next_stream", "name": "Next stream", "version": 1, "adds": "Source",
          "about": "When you're on next (\"Friday 7 PM\", in 2 days), for your Starting and Ending blooms.",
          "new": "New: your next stream's day and time."}

KIND = "next_stream"
H = cards.H
W = 600
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
GRACE = timedelta(minutes=30)  # "today" a little past the start (you might be running late)


def parse(text: str, now: datetime) -> datetime | None:
    """The next moment When means: "Friday 7 PM" (this Friday, or next week's once it's over), or "7 PM"."""
    text = str(text or "").strip()
    m = re.match(r"\s*([a-z]+)\.?\s+(.*)$", text, re.I)
    day = None
    if m:
        word = m[1].lower()
        day = next((i for i, d in enumerate(DAYS) if len(word) >= 3 and d.startswith(word)), None)
        if day is not None:
            text = m[2]
        elif word in ("today", "tonight"):
            text = m[2]
    clock = cards.until_time(text, now)
    if clock is None:
        return None
    target = now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
    if day is not None:
        target += timedelta(days=(day - now.weekday()) % 7)
    if target + GRACE < now:
        target += timedelta(days=7 if day is not None else 1)
    return target


def when_text(target: datetime) -> str:
    hour = target.strftime("%I").lstrip("0")
    minutes = f":{target.minute:02d}" if target.minute else ""
    return f"{DAYS[target.weekday()].capitalize()} {hour}{minutes} {target.strftime('%p')}"


def soon_text(target: datetime, now: datetime) -> str:
    days = (target.date() - now.date()).days
    if days <= 0:
        hours = (target - now).total_seconds() / 3600
        if hours <= 0:
            return "starting now"
        return "today" if hours >= 1.5 else ("in an hour" if hours >= 0.75 else "soon")
    return "tomorrow" if days == 1 else f"in {days} days"


def words(settings: dict[str, Any], now: datetime) -> tuple[str, str] | None:
    target = parse(settings.get("when", ""), now)
    if target is None:
        return None
    label = str(settings.get("label", "") or "").strip()
    small = label
    if settings.get("show_soon", True):
        small = f"{label}  ·  {soon_text(target, now)}" if label else soon_text(target, now)
    return when_text(target), small


def paint_fitted(big: str, small: str, settings: dict[str, Any], detail: float = 1.0):
    look = cards.look_of(settings)
    shown = cards.Words(big, small, small_on_top=True, badge="calendar")
    return cards.paint(cards.card_width([shown], look), shown, detail=detail, look=look)


@register(KIND)
class NextStreamSource(Source):
    label = "Next stream"
    defaults = {"when": "Friday 7 PM", "label": "Next stream", "show_soon": True,
                "text_color": "#f2f7f3", "card_color": "#151c19e0", "accent_color": "#4fd88f", "font": "",
                "text_scale": 100, "corners": "leaf", "show_badge": True, "custom_look": ""}
    sleeps = True
    keeps_picture = True
    sharp = True
    own_sprig = (W, H)
    own_sprig_corner = "bottom_right"
    own_sprig_height = 0.14
    channel_order = painted.Frame.channel_order

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame = painted.Frame()
        self._card = painted.Aside(self._frame)
        self._bad: str | None = None
        self._w = W
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return self._w, H

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._bad = None
        self._card.forget()

    def sleep(self) -> None:
        self._frame.release()

    def video_frame(self) -> np.ndarray | None:
        settings = self.settings
        shown = words(settings, datetime.now())
        if shown is None:
            self.status = f'When "{settings.get("when", "")}" isn\'t a day and time (try Friday 7 PM).'
            if self._bad == settings.get("when"):
                return None
            self._bad = settings.get("when")
            self._card.drop()
            return painted.clear_frame(self._w, H)
        self.status, self._bad = "ok", None
        key = (*shown, cards.look_of(settings).key, self.detail)
        frame = self._card.picture(key, paint_fitted, *shown, dict(settings), self.detail)
        if frame is not None:
            self._w = int(frame.shape[1])
        return frame
