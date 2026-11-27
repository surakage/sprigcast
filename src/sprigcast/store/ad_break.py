"""Ad break countdown: when a Twitch ad break is coming, and how long it has left, in a small card on stream.

Add Ad break countdown (+ → Plugins → Ad break countdown): it gets a sprig of its own with the card at the top
right of your bloom. It shows only around an ad break:
    before    "Ads in 0:45" with "Quick break coming", from Lead seconds (60) before Twitch runs them
    during    "Back in 0:30" with "Ads are playing"
The last 10 seconds turn the Last seconds color. The rest of the time it's hidden.

Where the times come from: Twitch's ad schedule (Get Ad Schedule), read by SprigCast with the Twitch sign-in it
already has, about every 30 seconds while you're live and a countdown is in your garden (every 10 seconds close
to an ad). It needs Twitch's channel:read:ads permission: if you connected Twitch before SprigCast asked for it,
connect Twitch once more in Settings → Stream (the card says so in its status). Nothing new to approve elsewhere.

Settings: Lead, Before and During words, Last seconds color, and the Style tab's colors, font, text size and
corners. Apps can read it with {"do": "adbreak.get"}: {"nextAdIn", "adLeft", "duration"} (seconds or null).

It's SprigCast's clock card with an hourglass, drawn once a second only while it shows.

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
from sprigcast.core.cards import clock_text
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import output_since

log = logging.getLogger(__name__)

PLUGIN = {"id": "ad_break", "name": "Ad break countdown", "version": 1, "adds": "Source",
          "about": "When a Twitch ad break is coming and how long it has left, in a small card on stream.",
          "new": "New: a countdown to your Twitch ads, and while they play."}

KIND = "ad_break"
H = cards.H
W = 560
LOOK_EVERY_MS = 5000
READ_EVERY_S = 30.0
READ_CLOSE_S = 10.0  # close to an ad, or during one
FAILED_AGAIN_S = 120.0
LAST_S = 10.0


class Schedule:
    """Twitch's ad schedule as last read, in wall-clock seconds (time.time()). Swapped whole: safe to read."""

    def __init__(self):
        self.lock = threading.Lock()
        self.next_ad = 0.0  # 0: none scheduled
        self.last_ad = 0.0
        self.duration = 0.0
        self.read_at = -1e9  # time.monotonic() of the last read (or failed read)
        self.busy = False
        self.problem = ""

    def take(self, data: dict[str, Any], now_mono: float) -> None:
        def num(key):
            try:
                value = float(data.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0
            return value if math.isfinite(value) and value > 0 else 0.0

        with self.lock:
            self.next_ad, self.last_ad, self.duration = num("next_ad_at"), num("last_ad_at"), num("duration")
            self.read_at, self.problem = now_mono, ""

    def clear(self) -> None:
        with self.lock:
            self.next_ad = self.last_ad = self.duration = 0.0
            self.read_at = -1e9

    def state(self, now: float, lead: float) -> tuple[str, float, float] | None:
        """("during", seconds left, length) while ads play, ("soon", seconds to go, lead) within lead of the next,
        else None. now is time.time()."""
        with self.lock:
            if self.last_ad and self.duration and self.last_ad <= now < self.last_ad + self.duration:
                return "during", self.last_ad + self.duration - now, self.duration
            if self.next_ad and 0 < self.next_ad - now <= lead:
                return "soon", self.next_ad - now, lead
            if self.next_ad and self.duration and self.next_ad <= now < self.next_ad + self.duration:
                return "during", self.next_ad + self.duration - now, self.duration  # began, not read yet
            return None

    def due(self, now_mono: float, now: float) -> bool:
        with self.lock:
            if self.busy:
                return False
            close = (self.next_ad and -self.duration - 5 < self.next_ad - now < 90) or (
                self.last_ad and now < self.last_ad + self.duration + 5)
            return now_mono - self.read_at >= (READ_CLOSE_S if close else READ_EVERY_S)


SCHEDULE = Schedule()


def _read() -> None:
    from sprigcast.output import twitch_auth

    try:
        data = twitch_auth.helix_get(lambda uid: f"/channels/ads?broadcaster_id={uid}", twitch_auth.ADS_SCOPE)
        rows = data.get("data") or []
        SCHEDULE.take(rows[0] if rows and isinstance(rows[0], dict) else {}, time.monotonic())
    except Exception as exc:  # noqa: BLE001 (signed out, offline: tried again later)
        log.info("ad break: couldn't read the ad schedule: %s", exc)
        with SCHEDULE.lock:
            SCHEDULE.read_at = time.monotonic() + FAILED_AGAIN_S - READ_EVERY_S
            SCHEDULE.problem = str(exc) if isinstance(exc, twitch_auth.TwitchAuthError) else "Couldn't reach Twitch."
    finally:
        with SCHEDULE.lock:
            SCHEDULE.busy = False


def look_now(api) -> None:
    """Read the schedule on a worker when it's due, while live with a countdown in the garden."""
    if api is None or not any(s.kind == KIND for s in api.col.sources):
        return
    if output_since("stream") is None:
        SCHEDULE.clear()
        return
    if SCHEDULE.due(time.monotonic(), time.time()):
        with SCHEDULE.lock:
            SCHEDULE.busy = True
        threading.Thread(target=_read, name="ad-schedule", daemon=True).start()


def _window_api():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    for widget in app.topLevelWidgets() if app is not None else []:
        if isinstance(getattr(type(widget), "api", None), property) and getattr(widget, "control", None) is not None:
            try:
                return widget.api
            except Exception:
                return None
    return None


def _make_timer():
    from PySide6.QtCore import QTimer

    timer = QTimer()
    timer.setInterval(LOOK_EVERY_MS)
    timer.timeout.connect(lambda: look_now(_window_api()))
    timer.start()
    return timer


_TIMER = None
try:
    from PySide6.QtWidgets import QApplication as _App

    if _App.instance() is not None:
        _TIMER = _make_timer()
except Exception:  # pragma: no cover
    log.debug("ad break: no timer", exc_info=True)


# The card ----------------------------------------------------------------------------------------------


def lead_s(settings: dict[str, Any]) -> float:
    try:
        value = float(settings.get("lead", 60))
    except (TypeError, ValueError):
        return 60.0
    return max(15.0, min(300.0, value)) if math.isfinite(value) else 60.0


def card_words(settings: dict[str, Any], what: str, left: float, length: float) -> cards.Words:
    shown = clock_text(math.ceil(left - 1e-6))
    share = max(0.0, min(1.0, left / length)) if length > 0 else 0.0
    if what == "during":
        return cards.Words(f"Back in {shown}", str(settings.get("during_text", "") or "").strip(), badge="hourglass",
                           vine=share)
    return cards.Words(f"Ads in {shown}", str(settings.get("before_text", "") or "").strip(), badge="hourglass",
                       vine=share)


def paint_fitted(words: cards.Words, settings: dict[str, Any], last: bool, detail: float = 1.0):
    if last:
        settings = {**settings, "accent_color": settings.get("last_color") or "#f2b84b"}
    look = cards.look_of(settings)
    widest = cards.Words(cards.even(words.big), words.small, vine=words.vine)
    return cards.paint(cards.card_width([widest], look), words, detail=detail, look=look)


@register(KIND)
class AdBreakSource(Source):
    label = "Ad break countdown"
    defaults = {"lead": 60, "before_text": "Quick break coming", "during_text": "Ads are playing",
                "last_color": "#f2b84b", "text_color": "#f2f7f3", "card_color": "#151c19e0",
                "accent_color": "#a970ff", "font": "", "text_scale": 100, "corners": "leaf", "show_badge": True,
                "custom_look": ""}
    sleeps = True
    keeps_picture = True
    sharp = True
    own_sprig = (W, H)
    own_sprig_corner = "top_right"
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
        self.status = SCHEDULE.problem or "ok"
        shown = SCHEDULE.state(time.time(), lead_s(settings))
        if shown is None:
            key = ("hidden", self._w)
            if key == self._hidden_key:
                return None
            self._hidden_key = key
            self._card.drop()
            self._frame.release()
            return painted.clear_frame(self._w, H)
        self._hidden_key = None
        words = card_words(settings, *shown)
        last = shown[1] <= LAST_S
        key = (words.big, words.small, round(words.vine or 0, 3), last, cards.look_of(settings).key, self.detail)
        frame = self._card.picture(key, paint_fitted, words, dict(settings), last, self.detail)
        if frame is not None:
            self._w = int(frame.shape[1])
        return frame


def _get(api, data):
    if not any(s.kind == KIND for s in api.col.sources):
        raise ApiError("not_found", "Add Ad break countdown first (+ → Plugins → Ad break countdown).")
    now = time.time()
    with SCHEDULE.lock:
        next_in = round(SCHEDULE.next_ad - now) if SCHEDULE.next_ad > now else None
        playing = SCHEDULE.last_ad and SCHEDULE.duration and now < SCHEDULE.last_ad + SCHEDULE.duration
        left = round(SCHEDULE.last_ad + SCHEDULE.duration - now) if playing else None
        return {"nextAdIn": next_in, "adLeft": left, "duration": round(SCHEDULE.duration) or None}


register_action("adbreak.get", "When the next Twitch ad break comes and how long one playing has left (seconds).",
                {}, _get)
