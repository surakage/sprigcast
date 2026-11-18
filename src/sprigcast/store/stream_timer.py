"""Stream timer: how long you've been live, a countdown, or a stopwatch, in a small card on stream.

Add Stream timer (+ → Plugins → Stream timer): it gets a sprig of its own with the card in the top left
corner of your bloom, in every shape. Move or resize it like anything else.

Mode:
    Time live   counts up from when you went live (or started recording: Count from). Runs by itself.
    Countdown   counts down Minutes, or to a time of day (Until, e.g. "8:30 PM" or "20:30"). With Restart
                when shown on, it starts over each time it comes on screen (a "Starting soon" bloom).
    Stopwatch   counts up when you start it, pauses and resets.

Start, pause and reset with hotkeys (Tools → Hotkeys → Plugins), a right-click on it, the Stream Deck
(Run action) or from an app:
    {"do": "timer.start"}                 start or carry on ("minutes": 10 starts a fresh 10-minute countdown)
    {"do": "timer.pause"}
    {"do": "timer.toggle"}                start or pause, for one button
    {"do": "timer.reset"}                 back to the start, stopped
    {"do": "timer.add", "seconds": 60}    more time on a countdown (or on a stopwatch); negative takes some off
    {"do": "timer.get"}                   where it is: {"mode", "seconds", "running", "text"}
Apps hear "timer.done" when a countdown reaches 0 on screen.

Settings: Label (small words above the time, e.g. "Starting in"), Done text (shown when a countdown ends,
e.g. "Starting now"), Hide when stopped, Text colour and Card colour (a colour ending in 00 is see-through).

For a countdown on its own (a "Starting soon" bloom), the Countdown plugin is made for it. Countdown stays here
so timers set up before it keep working.

It's SprigCast's clock card (core/cards.py): a leaf-cut card with a sprig that's green while the time moves.
Its box is the card, so a selection hugs it.

It's light: off screen it does nothing at all, and on screen the card is drawn again only when the time
on it changes, once a second. Where a stopwatch or countdown is is kept with your blooms, so it carries on
after a restart.

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
from sprigcast.plugin_hooks import output_since, register_hotkeys, register_item_menu

log = logging.getLogger(__name__)

PLUGIN = {"id": "stream_timer", "name": "Stream timer", "version": 3, "adds": "Source",
          "about": "How long you've been live, a countdown or a stopwatch, in a small card on stream.",
          "new": "A new SprigCast look, and its box hugs the card."}

KIND = "stream_timer"
H = cards.H
W = 416  # how wide the card starts (its box follows the words: timer_width)
OLD_W = 640  # the box before version 3, with the card at its left
MOST_MINUTES = 24 * 60
RESTART_WAIT = 0.5  # seconds a countdown coming on screen waits to be started over before it draws anyway

MODES = (("uptime", "Time live"), ("countdown", "Countdown"), ("stopwatch", "Stopwatch"))
COUNT_FROM = (("stream", "Going live"), ("record", "Recording"), ("either", "Live or recording"))


# What the timer reads ------------------------------------------------------------------------------


def _float(settings: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        value = float(settings.get(key, default) or 0.0)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def minutes_of(settings: dict[str, Any]) -> float:
    return max(0.0, min(MOST_MINUTES, _float(settings, "minutes", 5.0)))


def mode_of(settings: dict[str, Any]) -> str:
    mode = settings.get("mode", "uptime")
    return mode if mode in dict(MODES) else "uptime"


def to_a_time(settings: dict[str, Any]) -> bool:
    """A countdown to a time of day (Until set): it runs by itself, so start and pause don't apply."""
    return mode_of(settings) == "countdown" and bool(str(settings.get("until", "") or "").strip())


def controllable(settings: dict[str, Any]) -> bool:
    """Start, pause and reset work on it: a stopwatch, or a countdown of Minutes."""
    mode = mode_of(settings)
    return mode == "stopwatch" or (mode == "countdown" and not to_a_time(settings))


def counted(settings: dict[str, Any], now: float) -> float:
    """Seconds a stopwatch or countdown has run (now is time.time())."""
    since = _float(settings, "run_since")
    return _float(settings, "banked") + (max(0.0, now - since) if since > 0 else 0.0)


def running(settings: dict[str, Any]) -> bool:
    return _float(settings, "run_since") > 0


def live_since(settings: dict[str, Any]) -> float | None:
    """When the time live counts from (time.monotonic()), or None while off air."""
    which = settings.get("count_from", "stream")
    found = [output_since(w) for w in (("stream", "record") if which == "either" else (which,))]
    found = [t for t in found if t is not None]
    return min(found) if found else None


def reading(settings: dict[str, Any], now: float | None = None, now_mono: float | None = None,
            now_dt: datetime | None = None) -> dict[str, Any]:
    """Where the timer is: seconds (shown), counting (moving now), done (a countdown at 0), text and problem."""
    now = time.time() if now is None else now
    mode = mode_of(settings)
    out = {"mode": mode, "seconds": 0.0, "counting": False, "done": False, "problem": ""}
    if mode == "uptime":
        since = live_since(settings)
        if since is not None:
            out.update(seconds=max(0.0, (time.monotonic() if now_mono is None else now_mono) - since), counting=True)
    elif mode == "stopwatch":
        out.update(seconds=max(0.0, counted(settings, now)), counting=running(settings))
    else:
        until = str(settings.get("until", "") or "").strip()
        if until:
            target = until_time(until, now_dt or datetime.fromtimestamp(now))
            if target is None:
                out["problem"] = f'Until "{until}" isn\'t a time (try 8:30 PM or 20:30).'
                left = minutes_of(settings) * 60
            else:
                left = (target - (now_dt or datetime.fromtimestamp(now))).total_seconds()
                out["counting"] = left > 0
                out["done"] = left <= 0
        else:
            left = minutes_of(settings) * 60 - counted(settings, now)
            out["done"] = left <= 0 and (running(settings) or _float(settings, "banked") > 0)
            out["counting"] = running(settings) and left > 0
        out["seconds"] = max(0.0, float(math.ceil(left - 1e-6)))  # 0:01 stays up until it's really 0
    done_text = str(settings.get("done_text", "") or "").strip()
    out["text"] = done_text if out["done"] and done_text else clock_text(out["seconds"])
    return out


def hidden(settings: dict[str, Any], read: dict[str, Any]) -> bool:
    """Hide when stopped: off air, a stopwatch not started, a countdown not started (or done with no Done text)."""
    if not settings.get("hide_when_stopped", False):
        return False
    if read["done"]:
        return not str(settings.get("done_text", "") or "").strip()
    if read["counting"]:
        return False
    return read["mode"] == "uptime" or read["seconds"] == 0 or (
        read["mode"] == "countdown" and not running(settings) and _float(settings, "banked") == 0)


# Start, pause, reset: each gives the settings to keep ---------------------------------------------


def started(settings: dict[str, Any], now: float) -> dict[str, Any]:
    if running(settings):
        if mode_of(settings) == "countdown" and minutes_of(settings) * 60 - counted(settings, now) <= 0:
            return {**settings, "banked": 0.0, "run_since": now}  # a finished countdown starts over
        return dict(settings)
    if mode_of(settings) == "countdown" and minutes_of(settings) * 60 - counted(settings, now) <= 0:
        return {**settings, "banked": 0.0, "run_since": now}
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
    """More time: a countdown has seconds more left, a stopwatch shows seconds more (never below 0)."""
    banked = _float(settings, "banked")
    if mode_of(settings) == "countdown":
        banked -= seconds
    else:
        banked = max(-counted({**settings, "banked": 0.0}, now), banked + seconds)
    return {**settings, "banked": banked}


# Drawing ------------------------------------------------------------------------------------------


def card_words(text: str, label: str, counting: bool) -> cards.Words:
    return cards.Words(text, label, small_on_top=True, badge="sprig" if counting else "sprig_quiet")


def longest(settings: dict[str, Any]) -> str:
    """The longest time the card will show with these settings, digits as 0: its box is at least this wide, so
    it stays put while the seconds tick (a countdown: its whole length). Time live and a stopwatch grow a digit
    as they go (at 10:00 and 1:00:00)."""
    mode = mode_of(settings)
    if mode in ("uptime", "stopwatch"):
        return "0:00"
    if mode == "countdown" and not to_a_time(settings):
        total = minutes_of(settings) * 60 - _float(settings, "banked")
        return cards.even(clock_text(max(total, 0.0)))
    return "00:00"


def timer_width(longest_text: str, text: str, label: str) -> int:
    """The box for the card: the longest time it will show, or what it shows now if that's longer (a Done
    text is allowed to shrink to fit, down to 60%)."""
    shown = [cards.Words(longest_text, label)]
    if any(c.isdigit() for c in text):
        shown.append(cards.Words(cards.even(text), label))
    else:
        shown.append(cards.Words(text, label, shrink=0.6))
    return cards.card_width(shown)


def paint(text: str, label: str, settings: dict[str, Any], counting: bool = True, detail: float = 1.0,
          width: int = W):
    """The card as a QImage (premultiplied), width x H: the label small above the time."""
    return cards.paint(width, card_words(text, label, counting), settings.get("text_color"),
                       settings.get("card_color"), detail)


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
            log.info("stream timer %s: the countdown reached 0", source.name)
            try:
                send_event(api, "timer.done", {"source": source.name})
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
except Exception:  # pragma: no cover - no Qt: the timer still draws, it just can't restart itself
    log.debug("stream timer: no UI thread helper", exc_info=True)


@register(KIND)
class StreamTimerSource(Source):
    label = "Stream timer"
    defaults = {"mode": "uptime", "count_from": "stream", "minutes": 5.0, "until": "", "label": "",
                "done_text": "", "restart_on_show": True, "hide_when_stopped": False,
                "text_color": "#f2f7f3", "card_color": "#151c19e0"}
    choices = {"mode": MODES, "count_from": COUNT_FROM}
    sleeps = True  # off screen it does nothing; back on screen it draws the time as it is then
    keeps_picture = True  # (only if the time changed meanwhile: its texture stays while it sleeps)
    sharp = True  # drawn at the size it's shown
    own_sprig = (W, H)
    own_sprig_corner = "top_left"
    card_box = (OLD_W, "left")  # the box it had before (main_window keeps the card where it was)
    own_sprig_height = 0.14  # starts readable: the card about a seventh of the canvas tall
    channel_order = painted.Frame.channel_order

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame = painted.Frame()
        self._shown: tuple | None = None  # the clear card's key while it's hidden (the painted ones: self._card)
        self._card = painted.Aside(self._frame)  # painted on the pictures lane, not the render thread
        self._was_done: bool | None = None  # last frame's countdown state, to hear it reach 0
        self._asked_at = 0.0  # when it asked the UI thread to start it over (drawn once that's done)
        self._w = W  # the card on screen (a new width counts once its picture lands)
        self._widths: tuple[tuple, int] | None = None  # (what it was worked out from, the width)
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return self._w, H

    def _width_for(self, text: str, label: str) -> int:
        """timer_width, worked out again only when the digits or words change (not every second)."""
        what = (longest(self.settings), cards.even(text), label)
        if self._widths is None or self._widths[0] != what:
            self._widths = (what, timer_width(what[0], text, label))
        return self._widths[1]

    def start(self) -> None:
        self._came_on()

    def wake(self) -> None:
        self._came_on()  # the card is drawn again only if what it shows changed (keeps_picture)

    def _came_on(self) -> None:
        self._was_done = None
        if (self.settings.get("restart_on_show", True) and controllable(self.settings)
                and mode_of(self.settings) == "countdown" and _UI is not None):
            self._asked_at = time.monotonic()
            _UI.restart.emit(self.id)  # the UI thread starts it over and saves that

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._shown = None  # draw it again with the new look
        self._card.forget()
        self._asked_at = 0.0

    def sleep(self) -> None:
        self._frame.release()  # the texture has the picture

    def video_frame(self) -> np.ndarray | None:
        if self._asked_at and time.monotonic() - self._asked_at < RESTART_WAIT:
            return None  # starting over: wait a moment for it rather than flash the old time
        self._asked_at = 0.0
        settings = self.settings
        read = reading(settings)
        self.status = read["problem"] or "ok"
        if read["mode"] == "countdown":
            if self._was_done is False and read["done"] and _UI is not None:
                _UI.finished.emit(self.id)
            self._was_done = read["done"]
        label = str(settings.get("label", "") or "").strip()
        if read["done"] and read["text"] != clock_text(0):
            label = ""  # the Done text says it all ("Starting in" over "Starting now" read twice)
        gone = hidden(settings, read)
        width = self._width_for(read["text"], label)
        key = (width, gone, read["text"], label, read["counting"], settings.get("text_color"),
               settings.get("card_color"), self.detail)
        if gone:
            if key == self._shown:
                return None  # nothing changed: nothing drawn, nothing uploaded
            self._shown = key
            self._card.drop()
            self._frame.release()
            return painted.clear_frame(self._w, H)
        self._shown = None
        frame = self._card.picture(key, paint, read["text"], label, dict(settings), read["counting"], self.detail,
                                   width)
        if frame is not None:
            self._w = self._card.landed[0]
        return frame


# The API, hotkeys and right-click ----------------------------------------------------------------


def _timers(api, data: dict | None = None, *, control: bool = True) -> list:
    """The timers this is for: the one named (source), or every one (that start and pause work on)."""
    if data and data.get("source") is not None:
        source = api._source(data, "source")
        if source.kind != KIND:
            raise ApiError("bad_request", f"{source.name} isn't a stream timer.")
        if control and not controllable(source.settings):
            raise ApiError("bad_request", _why_not(source))
        return [source]
    found = [s for s in api.col.sources if s.kind == KIND]
    if not found:
        raise ApiError("not_found", "Add a Stream timer first (+ → Plugins → Stream timer).")
    if not control:
        return found
    usable = [s for s in found if controllable(s.settings)]
    if not usable:
        raise ApiError("bad_request", _why_not(found[0]))
    return usable


def _why_not(source) -> str:
    if mode_of(source.settings) == "uptime":
        return f"{source.name} counts time live by itself; set its Mode to Countdown or Stopwatch to start and pause it."
    return f"{source.name} counts down to {source.settings.get('until')} by itself; clear Until to start and pause it."


def _answer(source) -> dict[str, Any]:
    read = reading(source.settings)
    return {"source": source.name, "mode": read["mode"], "seconds": round(read["seconds"], 1),
            "running": read["counting"], "done": read["done"], "text": read["text"]}


def change(api, sources, how) -> dict:
    """Give each timer how(settings, now), save it (it's kept with your blooms) and redraw. Answers the first."""
    now = time.time()
    for source in sources:
        source.settings = how(dict(source.settings), now)
    commit_settings(api.host, sources, "timer")
    try:
        send_event(api, "timer.changed", _answer(sources[0]))
    except Exception:
        log.debug("couldn't tell apps about the timer", exc_info=True)
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
    timers = _timers(api, data)
    if data.get("minutes") is not None:
        minutes = _seconds(data, "minutes")
        if not 0 < minutes <= MOST_MINUTES:
            raise ApiError("bad_request", f"minutes must be above 0 and at most {MOST_MINUTES}.")
        timers = [s for s in timers if mode_of(s.settings) == "countdown"] or timers
        return change(api, timers, lambda s, now: {**restarted(s, now), "minutes": minutes})
    return change(api, timers, started)


def _pause(api, data):
    return change(api, _timers(api, data), paused)


def _toggle(api, data):
    return change(api, _timers(api, data), toggled)


def _reset(api, data):
    return change(api, _timers(api, data), reset)


def _add(api, data):
    seconds = _seconds(data, "seconds")
    return change(api, _timers(api, data), lambda s, now: added(s, seconds, now))


def _get(api, data):
    return _answer(_timers(api, data, control=False)[0])


_WHICH = {"source": "?which Stream timer (default: every stopwatch and countdown)"}
register_action("timer.start", "Start the stream timer, or carry on after a pause. minutes starts a fresh countdown.",
                {"minutes": "?a countdown of this many minutes, from the start", **_WHICH}, _start)
register_action("timer.pause", "Pause the stream timer.", dict(_WHICH), _pause)
register_action("timer.toggle", "Start the stream timer, or pause it if it's going.", dict(_WHICH), _toggle)
register_action("timer.reset", "The stream timer back to the start, stopped.", dict(_WHICH), _reset)
register_action("timer.add", "Add seconds to a countdown (or a stopwatch); negative takes time off.",
                {"seconds": "how many seconds", **_WHICH}, _add)
register_action("timer.get", "Where the stream timer is, without changing it.",
                {"source": "?which Stream timer (default: the first)"}, _get)


HOTKEYS = {
    "timer:toggle": ("Stream timer: start or pause", toggled),
    "timer:reset": ("Stream timer: reset", reset),
    "timer:add": ("Stream timer: add a minute", lambda s, now: added(s, 60, now)),
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
        change(api, _timers(api), entry[1])
    except ApiError as exc:
        log.info("stream timer hotkey: %s", exc)
        return exc.message
    return None


register_hotkeys(_hotkeys, _run_hotkey)


def _menu(api, source, add):
    if source is None or source.kind != KIND or not controllable(source.settings):
        return
    going = reading(source.settings)["counting"]
    add("Pause" if going else "Start", lambda: change(api, [source], toggled))
    add("Reset", lambda: change(api, [source], reset))
    add("Add a minute", lambda: change(api, [source], lambda s, now: added(s, 60, now)))


register_item_menu(_menu)
