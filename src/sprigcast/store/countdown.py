"""Countdown: a countdown on stream, for a "Starting soon" bloom or a break, on SprigCast's clock card.

Add Countdown (+ → Plugins → Countdown): it gets a sprig of its own with the card in the top left corner of
your bloom, in every shape. Move or resize it like anything else.

It counts down Minutes, or to a time of day (Until, e.g. "8:30 PM" or "20:30"). With Restart when shown on
(the default) it starts over each time it comes on screen, so a "Starting soon" bloom always shows the whole
countdown. A vine along the bottom of the card shrinks as the time runs out (a countdown of Minutes).

Start, pause, reset and add time with hotkeys (Tools → Hotkeys → Plugins), a right-click on it, the Stream Deck
(Run action) or from an app:
    {"do": "countdown.start"}                 start or carry on ("minutes": 10 starts a fresh 10-minute one)
    {"do": "countdown.pause"}
    {"do": "countdown.toggle"}                start or pause, for one button
    {"do": "countdown.reset"}                 back to the start, stopped
    {"do": "countdown.add", "seconds": 60}    more time; negative takes some off
    {"do": "countdown.get"}                   where it is: {"seconds", "running", "done", "text"}
Apps hear "countdown.changed" after each of those, and "countdown.done" when it reaches 0 on screen.

Settings: Minutes, Until, Label (small words above the time), Done text (shown when it ends), Restart when shown,
Vine, Hide when stopped, Text colour and Card colour (a colour ending in 00 is see-through).

It's light: off screen it does nothing at all, and on screen the card is drawn again only when the time on it
changes, once a second, on the pictures lane (not the render thread). Its box is the card, as wide as the
whole countdown needs, so it stays put while it counts and a selection hugs it. Where it is is kept with your
blooms, so it carries on after a restart.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import Any

import numpy as np

from sprigcast.control.native import ApiError, commit_settings, register_action, send_event
from sprigcast.core import cards, painted
from sprigcast.core.cards import clock_text, until_time
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import register_hotkeys, register_item_menu

log = logging.getLogger(__name__)

PLUGIN = {"id": "countdown", "name": "Countdown", "version": 2, "adds": "Source",
          "about": "A countdown on a SprigCast card, for a Starting soon bloom or a break.",
          "new": "A Style tab: your own colours, font, text size and corners, and a custom look box."}

KIND = "countdown_timer"  # "countdown" is the plugins folder's example
H = cards.H
W = 396  # how wide the card starts (its box follows the length: card_width)
MOST_MINUTES = 24 * 60
RESTART_WAIT = 0.5  # seconds a countdown coming on screen waits to be started over before it draws anyway


# What it reads -------------------------------------------------------------------------------------


def _float(settings: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = float(settings.get(key, default) or 0.0)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def minutes_of(settings: dict[str, Any]) -> float:
    return max(0.0, min(MOST_MINUTES, _float(settings, "minutes", 5.0)))


def to_a_time(settings: dict[str, Any]) -> bool:
    """Counting to a time of day (Until set): it runs by itself, so start and pause don't apply."""
    return bool(str(settings.get("until", "") or "").strip())


def running(settings: dict[str, Any]) -> bool:
    return _float(settings, "run_since") > 0


def counted(settings: dict[str, Any], now: float) -> float:
    """Seconds it has run (now is time.time())."""
    since = _float(settings, "run_since")
    return _float(settings, "banked") + (max(0.0, now - since) if since > 0 else 0.0)


def length(settings: dict[str, Any]) -> float:
    """The whole countdown in seconds (Minutes, and time added or taken off)."""
    return max(0.0, minutes_of(settings) * 60 - min(0.0, _float(settings, "banked")))


def reading(settings: dict[str, Any], now: float | None = None, now_dt: datetime | None = None) -> dict[str, Any]:
    """Where it is: seconds (shown), counting (moving now), done (at 0), text, vine (share left) and problem."""
    now = time.time() if now is None else now
    out = {"seconds": 0.0, "counting": False, "done": False, "problem": "", "vine": None}
    until = str(settings.get("until", "") or "").strip()
    if until:
        at = now_dt or datetime.fromtimestamp(now)
        target = until_time(until, at)
        if target is None:
            out["problem"] = f'Until "{until}" isn\'t a time (try 8:30 PM or 20:30).'
            left = minutes_of(settings) * 60
        else:
            left = (target - at).total_seconds()
            out["counting"] = left > 0
            out["done"] = left <= 0
    else:
        left = minutes_of(settings) * 60 - counted(settings, now)
        out["done"] = left <= 0 and (running(settings) or _float(settings, "banked") > 0)
        out["counting"] = running(settings) and left > 0
        whole = length(settings)
        out["vine"] = max(0.0, min(1.0, math.ceil(left - 1e-6) / whole)) if whole > 0 else 0.0
    out["seconds"] = max(0.0, float(math.ceil(left - 1e-6)))  # 0:01 stays up until it's really 0
    done_text = str(settings.get("done_text", "") or "").strip()
    out["text"] = done_text if out["done"] and done_text else clock_text(out["seconds"])
    return out


def hidden(settings: dict[str, Any], read: dict[str, Any]) -> bool:
    """Hide when stopped: not started yet, or done with no Done text."""
    if not settings.get("hide_when_stopped", False):
        return False
    if read["done"]:
        return not str(settings.get("done_text", "") or "").strip()
    if read["counting"]:
        return False
    return read["seconds"] == 0 or (not running(settings) and _float(settings, "banked") == 0)


# Start, pause, reset: each gives the settings to keep ---------------------------------------------


def started(settings: dict[str, Any], now: float) -> dict[str, Any]:
    if minutes_of(settings) * 60 - counted(settings, now) <= 0:
        return {**settings, "banked": 0.0, "run_since": now}  # a finished countdown starts over
    if running(settings):
        return dict(settings)
    return {**settings, "run_since": now}


def paused(settings: dict[str, Any], now: float) -> dict[str, Any]:
    if not running(settings):
        return dict(settings)
    return {**settings, "banked": counted(settings, now), "run_since": 0.0}


def toggled(settings: dict[str, Any], now: float) -> dict[str, Any]:
    if running(settings) and not reading(settings, now)["done"]:
        return paused(settings, now)
    return started(settings, now)


def reset(settings: dict[str, Any], now: float = 0.0) -> dict[str, Any]:
    return {**settings, "banked": 0.0, "run_since": 0.0}


def restarted(settings: dict[str, Any], now: float) -> dict[str, Any]:
    return {**settings, "banked": 0.0, "run_since": now}


def added(settings: dict[str, Any], seconds: float, now: float) -> dict[str, Any]:
    """seconds more left (negative: less)."""
    return {**settings, "banked": _float(settings, "banked") - seconds}


# Drawing ------------------------------------------------------------------------------------------


def longest(settings: dict[str, Any]) -> str:
    """The longest time the card shows, digits as 0: its box is this wide, so it stays put as it counts."""
    if to_a_time(settings):
        return "00:00"
    return cards.even(clock_text(length(settings)))


def card_words(text: str, label: str, counting: bool, vine: float | None) -> cards.Words:
    return cards.Words(text, label, small_on_top=True, badge="sprig" if counting else "sprig_quiet", vine=vine)


def card_width(longest_text: str, text: str, label: str, vine: bool, look=None) -> int:
    """The box: the longest time it shows, or what it shows now if that's longer (a Done text may shrink to
    60% to fit)."""
    v = 1.0 if vine else None
    shown = [cards.Words(longest_text, label, vine=v)]
    if any(c.isdigit() for c in text):
        shown.append(cards.Words(cards.even(text), label, vine=v))
    else:
        shown.append(cards.Words(text, label, vine=v, shrink=0.6))
    return cards.card_width(shown, look)


def paint(text: str, label: str, settings: dict[str, Any], counting: bool, vine: float | None,
          detail: float = 1.0, width: int = W):
    """The card as a QImage (premultiplied), width x H: the label above the time, the vine under it."""
    return cards.paint(width, card_words(text, label, counting, vine), detail=detail, look=cards.look_of(settings))


# Telling the app (the source runs on the render thread; these run on the UI thread) ---------------


def _window_api():
    """The SprigCast API of the main window, or None (tests, or while it's starting)."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    for widget in app.topLevelWidgets() if app is not None else []:
        if isinstance(getattr(type(widget), "api", None), property) and getattr(widget, "control", None) is not None:
            try:
                return widget.api
            except Exception:
                return None
    return None


def _make_ui():
    from PySide6.QtCore import QObject, Signal, Slot

    class Ui(QObject):
        """Lives on the UI thread; the render thread asks through its signals, which Qt queues over."""

        restart = Signal(str)
        finished = Signal(str)

        def __init__(self):
            super().__init__()
            self.restart.connect(self.on_restart)
            self.finished.connect(self.on_finished)

        @Slot(str)
        def on_restart(self, source_id: str) -> None:
            api = _window_api()
            source = next((s for s in api.col.sources if s.id == source_id), None) if api is not None else None
            if source is not None and source.kind == KIND:
                change(api, [source], restarted)

        @Slot(str)
        def on_finished(self, source_id: str) -> None:
            api = _window_api()
            source = next((s for s in api.col.sources if s.id == source_id), None) if api is not None else None
            if source is None:
                return
            log.info("countdown %s reached 0", source.name)
            try:
                send_event(api, "countdown.done", {"source": source.name})
            except Exception:
                log.debug("couldn't tell apps the countdown ended", exc_info=True)

    return Ui()


_UI = None


def _ui():
    global _UI
    if _UI is None:
        _UI = _make_ui()
    return _UI


try:  # made now, on the UI thread (plugins load there), so it belongs to it
    from PySide6.QtWidgets import QApplication as _App

    if _App.instance() is not None:
        _ui()
except Exception:  # pragma: no cover - no Qt: it still draws, it just can't restart itself
    log.debug("countdown: no UI thread helper", exc_info=True)


@register(KIND)
class CountdownSource(Source):
    label = "Countdown"
    defaults = {"minutes": 5.0, "until": "", "label": "Starting in", "done_text": "Starting now",
                "restart_on_show": True, "vine": True, "hide_when_stopped": False,
                "text_color": "#f2f7f3", "card_color": "#151c19e8", "accent_color": "#4fd88f", "font": "", "text_scale": 100,
                "corners": "leaf", "show_badge": True, "custom_look": ""}
    placeholders = {"until": "8:30 PM"}
    sleeps = True  # off screen it does nothing; back on screen it draws the time as it is then
    keeps_picture = True  # (only if the time changed meanwhile: its texture stays while it sleeps)
    sharp = True  # drawn at the size it's shown
    own_sprig = (W, H)
    own_sprig_corner = "top_left"
    card_box = (W, "left")  # main_window keeps the card where it was when its box grows or shrinks
    own_sprig_height = 0.14  # starts readable: the card about a seventh of the canvas tall
    channel_order = painted.Frame.channel_order

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame = painted.Frame()
        self._shown: tuple | None = None  # the clear card's key while it's hidden
        self._card = painted.Aside(self._frame)  # painted on the pictures lane, not the render thread
        self._was_done: bool | None = None  # last frame's state, to hear it reach 0
        self._asked_at = 0.0  # when it asked the UI thread to start it over (drawn once that's done)
        self._w = W  # the card on screen (a new width counts once its picture lands)
        self._widths: tuple[tuple, int] | None = None
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return self._w, H

    def start(self) -> None:
        self._came_on()

    def wake(self) -> None:
        self._came_on()  # the card is drawn again only if what it shows changed (keeps_picture)

    def _came_on(self) -> None:
        self._was_done = None
        if self.settings.get("restart_on_show", True) and not to_a_time(self.settings) and _UI is not None:
            self._asked_at = time.monotonic()
            _UI.restart.emit(self.id)  # the UI thread starts it over and saves that

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._shown = None  # draw it again with the new look
        self._card.forget()
        self._asked_at = 0.0

    def sleep(self) -> None:
        self._frame.release()  # the texture has the picture

    def _width_for(self, text: str, label: str, vine: bool) -> int:
        """card_width, worked out again only when the digits or words change (not every second)."""
        look = cards.look_of(self.settings)
        what = (longest(self.settings), cards.even(text), label, vine, look.key)
        if self._widths is None or self._widths[0] != what:
            self._widths = (what, card_width(*what[:4], look))
        return self._widths[1]

    def video_frame(self) -> np.ndarray | None:
        if self._asked_at and time.monotonic() - self._asked_at < RESTART_WAIT:
            return None  # starting over: wait a moment for it rather than flash the old time
        self._asked_at = 0.0
        settings = self.settings
        read = reading(settings)
        self.status = read["problem"] or "ok"
        if self._was_done is False and read["done"] and _UI is not None:
            _UI.finished.emit(self.id)
        self._was_done = read["done"]
        label = str(settings.get("label", "") or "").strip()
        if read["done"] and read["text"] != clock_text(0):
            label = ""  # the Done text says it all ("Starting in" over "Starting now" read twice)
        vine = read["vine"] if settings.get("vine", True) and not read["done"] else None
        gone = hidden(settings, read)
        width = self._width_for(read["text"], label, read["vine"] is not None and bool(settings.get("vine", True)))
        key = (width, gone, read["text"], label, read["counting"], vine, cards.look_of(settings).key, self.detail)
        if gone:
            if key == self._shown:
                return None  # nothing changed: nothing drawn, nothing uploaded
            self._shown = key
            self._card.drop()
            self._frame.release()
            return painted.clear_frame(self._w, H)
        self._shown = None
        frame = self._card.picture(key, paint, read["text"], label, dict(settings), read["counting"], vine,
                                   self.detail, width)
        if frame is not None:
            self._w = self._card.landed[0]
        return frame


# The API, hotkeys and right-click ----------------------------------------------------------------


def _countdowns(api, data: dict | None = None, *, control: bool = True) -> list:
    """The countdowns this is for: the one named (source), or every one (that start and pause work on)."""
    if data and data.get("source") is not None:
        source = api._source(data, "source")
        if source.kind != KIND:
            raise ApiError("bad_request", f"{source.name} isn't a countdown.")
        if control and to_a_time(source.settings):
            raise ApiError("bad_request", _why_not(source))
        return [source]
    found = [s for s in api.col.sources if s.kind == KIND]
    if not found:
        raise ApiError("not_found", "Add a Countdown first (+ → Plugins → Countdown).")
    if not control:
        return found
    usable = [s for s in found if not to_a_time(s.settings)]
    if not usable:
        raise ApiError("bad_request", _why_not(found[0]))
    return usable


def _why_not(source) -> str:
    return f"{source.name} counts down to {source.settings.get('until')} by itself; clear Until to start and pause it."


def _answer(source) -> dict[str, Any]:
    read = reading(source.settings)
    return {"source": source.name, "seconds": round(read["seconds"], 1), "running": read["counting"],
            "done": read["done"], "text": read["text"]}


def change(api, sources, how) -> dict:
    """Give each countdown how(settings, now), save it (it's kept with your blooms) and redraw. Answers the first."""
    now = time.time()
    for source in sources:
        source.settings = how(dict(source.settings), now)
    commit_settings(api.host, sources, "countdown")
    try:
        send_event(api, "countdown.changed", _answer(sources[0]))
    except Exception:
        log.debug("couldn't tell apps about the countdown", exc_info=True)
    return _answer(sources[0])


def _seconds(data, key: str) -> float:
    value = data.get(key)
    if isinstance(value, str):
        try:
            value = float(value.strip())  # web links send text
        except ValueError:
            pass
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ApiError("bad_request", f"{key} must be a number.")
    return float(value)


def _start(api, data):
    found = _countdowns(api, data)
    if data.get("minutes") is not None:
        minutes = _seconds(data, "minutes")
        if not 0 < minutes <= MOST_MINUTES:
            raise ApiError("bad_request", f"minutes must be above 0 and at most {MOST_MINUTES}.")
        return change(api, found, lambda s, now: {**restarted(s, now), "minutes": minutes})
    return change(api, found, started)


def _pause(api, data):
    return change(api, _countdowns(api, data), paused)


def _toggle(api, data):
    return change(api, _countdowns(api, data), toggled)


def _reset(api, data):
    return change(api, _countdowns(api, data), reset)


def _add(api, data):
    seconds = _seconds(data, "seconds")
    return change(api, _countdowns(api, data), lambda s, now: added(s, seconds, now))


def _get(api, data):
    return _answer(_countdowns(api, data, control=False)[0])


_WHICH = {"source": "?which Countdown (default: every one)"}
register_action("countdown.start", "Start the countdown, or carry on after a pause. minutes starts a fresh one.",
                {"minutes": "?a countdown of this many minutes, from the start", **_WHICH}, _start)
register_action("countdown.pause", "Pause the countdown.", dict(_WHICH), _pause)
register_action("countdown.toggle", "Start the countdown, or pause it if it's going.", dict(_WHICH), _toggle)
register_action("countdown.reset", "The countdown back to the start, stopped.", dict(_WHICH), _reset)
register_action("countdown.add", "Add seconds to the countdown; negative takes time off.",
                {"seconds": "how many seconds", **_WHICH}, _add)
register_action("countdown.get", "Where the countdown is, without changing it.",
                {"source": "?which Countdown (default: the first)"}, _get)


HOTKEYS = {
    "countdown:toggle": ("Countdown: start or pause", toggled),
    "countdown:reset": ("Countdown: reset", reset),
    "countdown:add": ("Countdown: add a minute", lambda s, now: added(s, 60, now)),
}


def _hotkeys(collection):
    if not any(s.kind == KIND for s in collection.sources):
        return []
    return [(hid, label) for hid, (label, _fn) in HOTKEYS.items()]


def _run_hotkey(api, hid):
    entry = HOTKEYS.get(hid)
    if entry is None:
        return None
    try:
        change(api, _countdowns(api), entry[1])
    except ApiError as exc:
        log.info("countdown hotkey: %s", exc)
        return exc.message
    return None


register_hotkeys(_hotkeys, _run_hotkey)


def _menu(api, source, add):
    if source is None or source.kind != KIND or to_a_time(source.settings):
        return
    going = reading(source.settings)["counting"]
    add("Pause" if going else "Start", lambda: change(api, [source], toggled))
    add("Reset", lambda: change(api, [source], reset))
    add("Add a minute", lambda: change(api, [source], lambda s, now: added(s, 60, now)))


register_item_menu(_menu)
