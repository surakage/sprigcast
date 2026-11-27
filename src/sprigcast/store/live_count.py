"""Live count: how many are watching right now, added up across the platforms you're live on, in a small card.

Add Live count (+ → Plugins → Live count): it gets a sprig of its own with the card in the bottom right corner of
your bloom, in every shape. Move or resize it like anything else.

Where the numbers come from:
    YouTube, Twitch and Kick   read by SprigCast itself while you're live there, with the sign-ins it already has
                               for Stream details (Settings → Stream). At most once a minute each, and only while
                               a Live count is in your garden. Nothing new to sign in to or approve.
    Any other platform         sent in by an app (StreamBridge, Streamer.bot, a web link) with livecount.set:
                               {"do": "livecount.set", "platform": "Facebook", "viewers": 12}
                               A number not sent again for 3 minutes stops counting (that app went quiet).
                               TikTok joins the platforms SprigCast reads itself once its app is approved.

Settings: Label (the small words under the number, "watching" to start), Each platform (the small words list each
platform's number instead), YouTube, Twitch, Kick and Other apps (which ones count), Hide when not live, Blink, and
the Style tab's colors, font, text size and corners.

Apps can read it too: {"do": "livecount.get"} answers {"total", "platforms": {name: viewers}, "live"}.

It's SprigCast's clock card, with an eye badge that's green while you're live and blinks now and
then (Blink switches that off). With nobody watching (0) the eye stays shut. It's light: the
numbers come from a cache the window fills every few seconds (never a web call on the render thread), and the
card is drawn again only when the words on it change. Off screen it does nothing at all.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import math
import random
import time
from typing import Any

import numpy as np

from sprigcast.control.native import ApiError, register_action
from sprigcast.core import cards, painted
from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

PLUGIN = {"id": "live_count", "name": "Live count", "version": 1, "adds": "Source",
          "about": "How many are watching right now, across YouTube, Twitch, Kick and more, in a small card.",
          "new": "New: YouTube, Twitch and Kick by themselves, and any other platform sent in by an app."}

KIND = "live_count"
H = cards.H
W = 560
LOOK_EVERY_MS = 5000  # how often the window refreshes the numbers the card reads (from SprigCast's own cache)
OTHER_KEEP_S = 180.0  # a platform an app sends stops counting this long after its last number
MOST = 99_999_999
NAMES = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick"}  # the platforms SprigCast reads itself


# The numbers --------------------------------------------------------------------------------------

# What the window last read: platform -> {"live", "viewers"}. Swapped whole, so the render thread reads it safely.
_read: dict[str, dict[str, Any]] = {}
# What apps sent: name -> (viewers, time.monotonic() it came).
_others: dict[str, tuple[int, float]] = {}


def others(now: float | None = None) -> dict[str, int]:
    now = time.monotonic() if now is None else now
    return {name: n for name, (n, at) in list(_others.items()) if now - at < OTHER_KEEP_S}


def counted(settings: dict[str, Any], read: dict[str, dict[str, Any]] | None = None,
            sent: dict[str, int] | None = None) -> dict[str, Any]:
    """What the card shows: total, each platform's number (in order), live (any counted platform live) and
    waiting (live but no number yet)."""
    read = _read if read is None else read
    sent = others() if sent is None else sent
    each: list[tuple[str, int]] = []
    live = waiting = False
    for key, name in NAMES.items():
        if not settings.get(key, True):
            continue
        got = read.get(key) or {}
        if not got.get("live"):
            continue
        live = True
        if got.get("viewers") is None:
            waiting = True
            continue
        each.append((name, int(got["viewers"])))
    if settings.get("other_apps", True):
        for name, n in sorted(sent.items(), key=lambda kv: kv[0].lower()):
            if name.lower() in NAMES:
                continue  # SprigCast reads that one itself
            live = True
            each.append((name, n))
    return {"total": sum(n for _name, n in each), "each": each, "live": live, "waiting": waiting and not each}


def number_text(n: int) -> str:
    return f"{n:,}"


def words(settings: dict[str, Any], count: dict[str, Any]) -> tuple[str, str]:
    """The big number and the small words under it."""
    big = "–" if count["waiting"] else number_text(count["total"])
    if settings.get("each_platform", False) and count["each"]:
        small = "  ·  ".join(f"{name} {number_text(n)}" for name, n in count["each"])
    else:
        small = str(settings.get("label", "watching") or "").strip()
    return big, small


def card_words(big: str, small: str, live: bool, openness: float = 1.0) -> cards.Words:
    return cards.Words(big, small, badge="viewers" if live else "viewers_quiet", openness=openness)


# The eye blinks now and then while live: a few pictures over a fifth of a second, every BLINK_EVERY seconds or so.
BLINK_EVERY = (3.0, 9.0)
BLINK = ((0.05, 0.5), (0.10, 0.0), (0.16, 0.5))  # (seconds into the blink, how open) until it's open again
BLINK_LONG = 0.22
TWICE = 0.2  # now and then a quick second blink


def openness(since: float) -> float:
    """How open the eye is this far into a blink."""
    for until, open_ in BLINK:
        if since < until:
            return open_
    return 1.0


def paint_fitted(big: str, small: str, settings: dict[str, Any], live: bool, detail: float = 1.0,
                 open_: float = 1.0):
    """The card at the width its words need (worked out on the pictures lane, never the render thread). The
    number is measured as its widest digits, so it doesn't twitch as it changes by one."""
    look = cards.look_of(settings)
    width = cards.card_width([cards.Words(cards.even(big), small)], look)
    return cards.paint(width, card_words(big, small, live, open_), detail=detail, look=look)


# The window's side: the numbers, every few seconds, from SprigCast's own cache --------------------


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


def look_now(api) -> None:
    """Ask SprigCast's viewers.counts (a cache: it never waits on the web) while a Live count is in the garden;
    without one, nothing is asked, so nothing is read from the platforms."""
    global _read
    if api is None or not any(s.kind == KIND for s in api.col.sources):
        if _read:
            _read = {}
        return
    try:
        answer = api.viewers_counts({})
    except Exception:  # noqa: BLE001
        log.debug("live count: couldn't read the counts", exc_info=True)
        return
    _read = dict((answer or {}).get("platforms") or {})


def _make_timer():
    from PySide6.QtCore import QTimer

    timer = QTimer()
    timer.setInterval(LOOK_EVERY_MS)
    timer.timeout.connect(lambda: look_now(_window_api()))
    timer.start()
    return timer


_TIMER = None
try:  # made now, on the UI thread (plugins load there)
    from PySide6.QtWidgets import QApplication as _App

    if _App.instance() is not None:
        _TIMER = _make_timer()
except Exception:  # pragma: no cover - no Qt: the card shows what apps send
    log.debug("live count: no timer", exc_info=True)


# The card -----------------------------------------------------------------------------------------


@register(KIND)
class LiveCountSource(Source):
    label = "Live count"
    defaults = {"label": "watching", "each_platform": False, "youtube": True, "twitch": True, "kick": True,
                "other_apps": True, "hide_when_offline": True, "blink": True,
                "text_color": "#f2f7f3", "card_color": "#151c19e0", "accent_color": "#4fd88f", "font": "",
                "text_scale": 100, "corners": "leaf", "show_badge": True, "custom_look": ""}
    sleeps = True  # off screen it does nothing
    keeps_picture = True  # back on screen, drawn again only if the words changed meanwhile
    sharp = True  # drawn at the size it's shown
    own_sprig = (W, H)
    own_sprig_corner = "bottom_right"
    own_sprig_height = 0.14
    channel_order = painted.Frame.channel_order

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame = painted.Frame()
        self._card = painted.Aside(self._frame)  # painted on the pictures lane, not the render thread
        self._hidden_key: tuple | None = None
        self._blink_at = time.monotonic() + random.uniform(*BLINK_EVERY)
        self._w = W
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return self._w, H

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._hidden_key = None
        self._card.forget()

    def wake(self) -> None:
        pass

    def sleep(self) -> None:
        self._frame.release()

    def video_frame(self) -> np.ndarray | None:
        settings = self.settings
        count = counted(settings)
        if not count["live"] and settings.get("hide_when_offline", True):
            key = ("hidden", self._w)
            if key == self._hidden_key:
                return None
            self._hidden_key = key
            self._card.drop()
            self._frame.release()
            return painted.clear_frame(self._w, H)
        self._hidden_key = None
        big, small = words(settings, count)
        open_ = 1.0
        if not count["waiting"] and count["total"] == 0:
            open_ = 0.0  # nobody watching: the eye stays shut
        elif count["live"] and settings.get("blink", True):
            now = time.monotonic()
            since = now - self._blink_at
            if since >= BLINK_LONG:  # done: the next one some seconds from now (or a quick second one)
                again = 0.15 if random.random() < TWICE and since < BLINK_LONG + 0.05 else random.uniform(*BLINK_EVERY)
                self._blink_at = now + again
            elif since >= 0:
                open_ = openness(since)
        key = (big, small, count["live"], open_, cards.look_of(settings).key, self.detail)
        frame = self._card.picture(key, paint_fitted, big, small, dict(settings), count["live"], self.detail, open_)
        if frame is not None:
            self._w = int(frame.shape[1])
        return frame


# Apps: other platforms in, the count out ----------------------------------------------------------


def _set(api, data):
    name = str(data.get("platform") or "").strip()[:24]
    if not name:
        raise ApiError("bad_request", "platform must name the platform (e.g. Facebook).")
    value = data.get("viewers")
    if isinstance(value, str):
        try:
            value = float(value.strip())  # web links send text
        except ValueError:
            pass
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ApiError("bad_request", "viewers must be a number.")
    if value < 0:  # an app saying it's no longer live there
        _others.pop(name, None)
    else:
        _others[name] = (int(min(value, MOST)), time.monotonic())
    return _get(api, {})


def _get(api, data):
    count = counted({}, sent=others())
    return {"total": count["total"], "platforms": dict(count["each"]), "live": count["live"]}


register_action("livecount.set", "Add a platform SprigCast doesn't read itself to the Live count (Facebook...). "
                "Send it again at least every 3 minutes; viewers below 0 takes it off.",
                {"platform": "the platform's name, e.g. Facebook", "viewers": "how many watch there now"}, _set)
register_action("livecount.get", "The Live count: the total, each platform's number, and whether you're live.",
                {}, _get)
