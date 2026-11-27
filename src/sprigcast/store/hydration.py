"""Hydration: a small "Time for a sip" card that shows up every so often while you're live, then hides.

Add Hydration (+ → Plugins → Hydration): it gets a sprig of its own with the card in the bottom left corner of
your bloom, in every shape. Move or resize it like anything else.

While you're live it comes on every Every minutes (45 to start) for Show for seconds (10), and counts the sips
this stream ("Sip 3 this stream"). A new stream starts the count over. Off air it stays hidden.

Show it now or put it off with a hotkey (Tools → Hotkeys → Plugins), a right-click on it, the Stream Deck (Run
action) or from an app:
    {"do": "hydrate.show"}                  show it now (and count a sip)
    {"do": "hydrate.snooze", "minutes": 10} the next reminder that much later
    {"do": "hydrate.get"}                   {"sips", "showing", "nextInSeconds"}

Settings: Every, Show for, Text ("Time for a sip"), Count sips (the small words count them, or show Label
instead), Label, and the Style tab's colors, font, text size and corners.

It's SprigCast's clock card (core/cards.py) with a water drop. No internet, nothing to sign in to, and the card
is drawn only when it comes on or goes. Off screen it does nothing at all.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

import numpy as np

from sprigcast.control.native import ApiError, register_action
from sprigcast.core import cards, painted
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import output_since, register_hotkeys, register_item_menu

log = logging.getLogger(__name__)

PLUGIN = {"id": "hydration", "name": "Hydration", "version": 1, "adds": "Source",
          "about": "A small \"Time for a sip\" card every so often while you're live, with a sip count.",
          "new": "New: sip reminders while you're live."}

KIND = "hydration"
H = cards.H
W = 640
MOST_MINUTES = 240
SNOOZE_MINUTES = 10.0


def _number(settings: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    try:
        value = float(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value)) if math.isfinite(value) else default


def every_s(settings: dict[str, Any]) -> float:
    return _number(settings, "every", 45.0, 5.0, MOST_MINUTES) * 60.0


def show_for_s(settings: dict[str, Any]) -> float:
    return _number(settings, "show_for", 10.0, 3.0, 120.0)


# The schedule: one for every Hydration card (they're the same reminder) ----------------------------


class Schedule:
    """When the next reminder is due and how many came this stream. now is time.monotonic(); live_since is when
    going live started (None off air). Safe from the render thread and the UI thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stream: float | None = None  # the stream this count is for
        self.next_at: float | None = None
        self.shown_at = -1e9
        self.sips = 0

    def tick(self, settings: dict[str, Any], live_since: float | None, now: float) -> bool:
        """Moves the schedule on; True while the card is showing."""
        with self.lock:
            if live_since != self.stream:  # went live (a new count) or off air
                self.stream, self.sips = live_since, 0
                self.next_at = None if live_since is None else live_since + every_s(settings)
                if live_since is None:
                    self.shown_at = -1e9
            if self.next_at is not None and now >= self.next_at:
                if now - self.next_at < show_for_s(settings):  # due now (not long past while it was off screen)
                    self.shown_at, self.sips = self.next_at, self.sips + 1
                self.next_at = now + every_s(settings)
            return now - self.shown_at < show_for_s(settings)

    def show_now(self, settings: dict[str, Any], now: float) -> None:
        with self.lock:
            self.shown_at, self.sips = now, self.sips + 1
            if self.next_at is not None:
                self.next_at = now + every_s(settings)

    def snooze(self, minutes: float, now: float) -> None:
        with self.lock:
            self.shown_at = -1e9  # hide it now
            base = self.next_at if self.next_at is not None else now
            self.next_at = max(base, now) + minutes * 60.0

    def answer(self, settings: dict[str, Any], now: float) -> dict[str, Any]:
        with self.lock:
            left = None if self.next_at is None else max(0, round(self.next_at - now))
            return {"sips": self.sips, "showing": now - self.shown_at < show_for_s(settings), "nextInSeconds": left}


SCHEDULE = Schedule()


def words(settings: dict[str, Any], sips: int) -> tuple[str, str]:
    big = str(settings.get("text", "") or "").strip() or "Time for a sip"
    if settings.get("count_sips", True) and sips > 0:
        return big, f"Sip {sips} this stream"
    return big, str(settings.get("label", "") or "").strip()


def paint_fitted(big: str, small: str, settings: dict[str, Any], detail: float = 1.0):
    look = cards.look_of(settings)
    shown = cards.Words(big, small, badge="drop")
    return cards.paint(cards.card_width([shown], look), shown, detail=detail, look=look)


@register(KIND)
class HydrationSource(Source):
    label = "Hydration"
    defaults = {"every": 45, "show_for": 10, "text": "Time for a sip", "count_sips": True,
                "label": "Hydration check", "text_color": "#f2f7f3", "card_color": "#151c19e0",
                "accent_color": "#5cc8f0", "font": "", "text_scale": 100, "corners": "leaf", "show_badge": True,
                "custom_look": ""}
    sleeps = True  # off screen it does nothing
    keeps_picture = True
    sharp = True
    own_sprig = (W, H)
    own_sprig_corner = "bottom_left"
    own_sprig_height = 0.14
    channel_order = painted.Frame.channel_order

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame = painted.Frame()
        self._card = painted.Aside(self._frame)
        self._hidden_key: tuple | None = None
        self._w = W
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return self._w, H

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._hidden_key = None
        self._card.forget()

    def sleep(self) -> None:
        self._frame.release()

    def video_frame(self) -> np.ndarray | None:
        settings = self.settings
        showing = SCHEDULE.tick(settings, output_since("stream"), time.monotonic())
        if not showing:
            key = ("hidden", self._w)
            if key == self._hidden_key:
                return None
            self._hidden_key = key
            self._card.drop()
            self._frame.release()
            return painted.clear_frame(self._w, H)
        self._hidden_key = None
        big, small = words(settings, SCHEDULE.sips)
        key = (big, small, cards.look_of(settings).key, self.detail)
        frame = self._card.picture(key, paint_fitted, big, small, dict(settings), self.detail)
        if frame is not None:
            self._w = int(frame.shape[1])
        return frame


# Apps, hotkeys and right-click ---------------------------------------------------------------------


def _settings(api) -> dict[str, Any]:
    found = [s for s in api.col.sources if s.kind == KIND]
    if not found:
        raise ApiError("not_found", "Add Hydration first (+ → Plugins → Hydration).")
    return {**HydrationSource.defaults, **found[0].settings}


def _show(api, data):
    settings = _settings(api)
    SCHEDULE.show_now(settings, time.monotonic())
    return SCHEDULE.answer(settings, time.monotonic())


def _snooze(api, data):
    settings = _settings(api)
    minutes = data.get("minutes", SNOOZE_MINUTES)
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        raise ApiError("bad_request", "minutes must be a number.") from None
    if not (math.isfinite(minutes) and 0 < minutes <= MOST_MINUTES):
        raise ApiError("bad_request", f"minutes must be above 0 and at most {MOST_MINUTES}.")
    SCHEDULE.snooze(minutes, time.monotonic())
    return SCHEDULE.answer(settings, time.monotonic())


def _get(api, data):
    return SCHEDULE.answer(_settings(api), time.monotonic())


register_action("hydrate.show", "Show the Hydration card now (and count a sip).", {}, _show)
register_action("hydrate.snooze", "Put the next Hydration reminder off (10 minutes, or minutes).",
                {"minutes": "?how many minutes (default 10)"}, _snooze)
register_action("hydrate.get", "How many sips this stream, whether it's showing, and when the next one is.", {}, _get)

HOTKEYS = {"hydration:show": ("Hydration: show now", _show), "hydration:snooze": ("Hydration: snooze", _snooze)}


def _hotkeys(collection):
    if not any(s.kind == KIND for s in collection.sources):
        return []
    return [(hid, label) for hid, (label, _fn) in HOTKEYS.items()]


def _run_hotkey(api, hid):
    entry = HOTKEYS.get(hid)
    if entry is None:
        return None
    try:
        entry[1](api, {})
    except ApiError as exc:
        return exc.message
    return None


register_hotkeys(_hotkeys, _run_hotkey)


def _menu(api, source, add):
    if source is None or source.kind != KIND:
        return
    add("Show now", lambda: _show(api, {}))
    add("Snooze 10 minutes", lambda: _snooze(api, {}))


register_item_menu(_menu)
