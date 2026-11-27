"""Break & End Guard: breaks while you're live, and a heads-up before your stream limit.

Add Break & End Guard (+ → Plugins → Break & End Guard): it gets a sprig of its own with a card in the top left
corner of your bloom. Put it in your Break bloom too: while a break runs it shows "Back in 4:32" with a vine that
shrinks as the break runs out, and "Break in 2:10" in the minutes before one (Show "Break in").

How it goes, from when you go live (SprigCast knows that itself: no Streamer.bot, so it can't get stuck after a
stream):
    Every Every minutes (60) a break comes. With Start by itself on, a note in SprigCast says it starts in Warning
    minutes (5) and then it does: SprigCast puts your Break bloom on air for Break minutes (5) and then puts back
    what was on before (or the bloom in Back to). Off, the note asks: Take a break, or it asks again in 30 minutes.
    Stream limit minutes (240) after going live: End warning minutes (15) before it, a note offers One more hour.
    At the limit, with Switch to ending on, your Ending bloom goes on air; otherwise a note says it's time. It never
    stops the stream itself.
    A break that would run into the end warning is skipped. Going off air ends everything (a break too).

Break bloom and Ending bloom: a bloom's name; empty finds one with "break" or "brb" (or "end") in its name.
Every 0 turns breaks off, Stream limit 0 the limit.

From a hotkey (Tools → Hotkeys → Plugins), a right-click on it, the Stream Deck or an app:
    {"do": "break.start"}                     a break now ("minutes": 10 for a longer one)
    {"do": "break.end"}                       back now
    {"do": "break.later", "minutes": 30}      the next break later
    {"do": "end.later", "minutes": 60}        the limit later
    {"do": "break.get"}                       {"onBreak", "breakLeft", "nextBreakIn", "endIn"} (seconds or null)
Apps hear "break.started", "break.ended" and "stream.limit".

The notes are only in SprigCast, never on stream. The card is SprigCast's clock card (core/cards.py) with a cup.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

import numpy as np

from sprigcast.control.native import ApiError, register_action, send_event
from sprigcast.core import cards, painted
from sprigcast.core.cards import clock_text
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import output_since, register_hotkeys, register_item_menu

log = logging.getLogger(__name__)

PLUGIN = {"id": "break_guard", "name": "Break & End Guard", "version": 1, "adds": "Source",
          "about": "Breaks while you're live (your Break bloom, then back), and a heads-up before your stream limit.",
          "new": "New: breaks and a stream limit, with no Streamer.bot."}

KIND = "break_guard"
H = cards.H
W = 560
ASK_AGAIN_S = 30 * 60.0  # a break not taken is asked about again this much later
LOOK_EVERY_MS = 1000


def _minutes(settings: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    try:
        value = float(settings.get(key, default))
    except (TypeError, ValueError):
        return default * 60.0
    return (max(low, min(high, value)) if math.isfinite(value) else default) * 60.0


def every_s(s):
    return _minutes(s, "every", 60, 0, 360) if float(s.get("every", 60) or 0) > 0 else 0.0


def warn_s(s):
    return _minutes(s, "warn", 5, 1, 15)


def break_s(s):
    return _minutes(s, "break_minutes", 5, 1, 60)


def limit_s(s):
    return _minutes(s, "limit", 240, 30, 1440) if float(s.get("limit", 240) or 0) > 0 else 0.0


def end_warn_s(s):
    return _minutes(s, "end_warn", 15, 1, 60)


# The guard: what's due, as events the window acts on (plain logic, so it's tested without a window) -------


class Guard:
    """Times are time.monotonic(). tick() returns events: ("break_soon", secs), ("ask_break",), ("break_start",),
    ("break_end",), ("end_soon", secs), ("end",). The card reads break_until, break_len, next_break and warned."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stream: float | None = None
        self.next_break: float | None = None
        self.warned = False
        self.break_until: float | None = None
        self.break_len = 0.0
        self.end_at: float | None = None
        self.end_warned = False
        self.ended = False

    def tick(self, s: dict[str, Any], live_since: float | None, now: float) -> list[tuple]:
        out: list[tuple] = []
        with self.lock:
            if live_since != self.stream:
                if self.break_until is not None and live_since is None:
                    out.append(("break_over",))  # off air mid-break: nothing to put back on air
                self.stream = live_since
                self.break_until, self.warned, self.end_warned, self.ended = None, False, False, False
                every, limit = every_s(s), limit_s(s)
                self.next_break = live_since + every if live_since is not None and every else None
                self.end_at = live_since + limit if live_since is not None and limit else None
            if live_since is None:
                return out
            if self.break_until is not None and now >= self.break_until:
                self.break_until = None
                out.append(("break_end",))
            if self.next_break is not None and self.end_at is not None and (
                    self.next_break + break_s(s) > self.end_at - end_warn_s(s)):
                self.next_break = None  # it would run into the end: skipped
            if self.next_break is not None and self.break_until is None:
                if s.get("auto_break", True):
                    if not self.warned and now >= self.next_break - warn_s(s):
                        self.warned = True
                        out.append(("break_soon", max(0.0, self.next_break - now)))
                    if now >= self.next_break:
                        out.append(("break_start",))
                        self._start(s, now, break_s(s))
                elif now >= self.next_break:
                    self.next_break = now + ASK_AGAIN_S
                    out.append(("ask_break",))
            if self.end_at is not None:
                if not self.end_warned and now >= self.end_at - end_warn_s(s):
                    self.end_warned = True
                    out.append(("end_soon", max(0.0, self.end_at - now)))
                if not self.ended and now >= self.end_at:
                    self.ended = True
                    out.append(("end",))
        return out

    def _start(self, s, now: float, length: float) -> None:
        self.break_until, self.break_len, self.warned = now + length, length, False
        every = every_s(s)
        self.next_break = now + length + every if every else None

    def start(self, s, now: float, length: float | None = None) -> None:
        with self.lock:
            self._start(s, now, break_s(s) if length is None else length)

    def stop(self, now: float) -> bool:
        with self.lock:
            on = self.break_until is not None
            self.break_until = None
            return on

    def later(self, seconds: float, now: float, end: bool = False) -> None:
        with self.lock:
            if end:
                if self.end_at is not None:
                    self.end_at = max(self.end_at, now) + seconds
                    self.end_warned = self.ended = False
            elif self.next_break is not None:
                self.next_break, self.warned = max(self.next_break, now) + seconds, False

    def card(self, s: dict[str, Any], now: float) -> tuple[str, float | None] | None:
        """What the card shows: ("break", share left) during a break, ("soon", None) before one, or None."""
        with self.lock:
            if self.break_until is not None:
                return "break", max(0.0, self.break_until - now)
            if (self.warned and s.get("show_break_in", True) and self.next_break is not None
                    and s.get("auto_break", True)):
                return "soon", max(0.0, self.next_break - now)
            return None

    def answer(self, now: float) -> dict[str, Any]:
        with self.lock:
            def left(t):
                return None if t is None else max(0, round(t - now))
            return {"onBreak": self.break_until is not None, "breakLeft": left(self.break_until),
                    "nextBreakIn": left(self.next_break), "endIn": left(self.end_at)}


GUARD = Guard()


def card_words(s: dict[str, Any], what: str, left: float, length: float) -> cards.Words:
    shown = clock_text(math.ceil(left - 1e-6))
    if what == "break":
        big = f"{str(s.get('text', '') or 'Back in').strip()} {shown}"
        share = left / length if length > 0 else 0.0
        return cards.Words(big, str(s.get("label", "") or "").strip(), badge="cup", vine=max(0.0, min(1.0, share)))
    return cards.Words(f"Break in {shown}", "Quick break coming", badge="cup")


def paint_fitted(words: cards.Words, settings: dict[str, Any], detail: float = 1.0):
    look = cards.look_of(settings)
    widest = cards.Words(cards.even(words.big), words.small, vine=words.vine)
    return cards.paint(cards.card_width([widest], look), words, detail=detail, look=look)


@register(KIND)
class BreakGuardSource(Source):
    label = "Break & End Guard"
    defaults = {"every": 60, "warn": 5, "break_minutes": 5, "auto_break": True, "break_bloom": "", "back_to": "",
                "limit": 240, "end_warn": 15, "end_bloom": "", "auto_end": False, "show_break_in": True,
                "text": "Back in", "label": "Be right back",
                "text_color": "#f2f7f3", "card_color": "#151c19e0", "accent_color": "#4fd88f", "font": "",
                "text_scale": 100, "corners": "leaf", "show_badge": True, "custom_look": ""}
    sleeps = True
    keeps_picture = True
    sharp = True
    own_sprig = (W, H)
    own_sprig_corner = "top_left"
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
        shown = GUARD.card(settings, time.monotonic())
        if shown is None:
            key = ("hidden", self._w)
            if key == self._hidden_key:
                return None
            self._hidden_key = key
            self._card.drop()
            self._frame.release()
            return painted.clear_frame(self._w, H)
        self._hidden_key = None
        words = card_words(settings, shown[0], shown[1], GUARD.break_len)
        key = (words.big, words.small, round(words.vine or 0, 3), cards.look_of(settings).key, self.detail)
        frame = self._card.picture(key, paint_fitted, words, dict(settings), self.detail)
        if frame is not None:
            self._w = int(frame.shape[1])
        return frame


# The window's side: a look every second, while a guard is in the garden --------------------------------


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


def _settings(api) -> dict[str, Any] | None:
    found = [s for s in api.col.sources if s.kind == KIND]
    return {**BreakGuardSource.defaults, **found[0].settings} if found else None


def find_bloom(api, name: str, words: tuple[str, ...]):
    """The bloom called name, or (name empty) the first whose name has one of words in it; None if none."""
    name = str(name or "").strip()
    if name:
        try:
            return api._bloom({"bloom": name})
        except ApiError:
            return None
    for bloom in api._blooms():
        if any(w in bloom.name.lower() for w in words):
            return bloom
    return None


_back: dict[str, str] = {}  # canvas -> bloom on air before the break


def _note(api, title: str, detail: str = "", link: str = "", on_click=None, ms: int = 15000) -> None:
    say = getattr(api.host, "_saved_note", None)
    if say is None:
        return
    try:
        say(title, None, detail=detail, link=link, on_click=on_click, ms=ms, kind="warn")
    except Exception:  # noqa: BLE001
        log.debug("break guard: couldn't show the note", exc_info=True)


def _event(api, name: str, data: dict) -> None:
    try:
        send_event(api, name, data)
    except Exception:  # noqa: BLE001
        log.debug("break guard: couldn't tell apps %s", name, exc_info=True)


def begin_break(api, settings: dict[str, Any], minutes: float | None = None) -> None:
    now = time.monotonic()
    GUARD.start(settings, now, None if minutes is None else minutes * 60.0)
    bloom = find_bloom(api, settings.get("break_bloom", ""), ("break", "brb"))
    if bloom is None:
        _note(api, "Break started", "Name your Break bloom in Break & End Guard to switch to it.")
    else:
        _back.clear()
        _back.update(api.col.active_scenes)
        api.bloom_show({"bloom": bloom.id})
    log.info("break guard: a break started")
    _event(api, "break.started", GUARD.answer(now))


def finish_break(api, settings: dict[str, Any], put_back: bool = True) -> None:
    if put_back:
        back = find_bloom(api, settings.get("back_to", ""), ()) if str(settings.get("back_to", "")).strip() else None
        if back is not None:
            api.bloom_show({"bloom": back.id})
        elif _back:
            known = {s.id for s in api.col.scenes}
            for canvas, scene_id in _back.items():
                if scene_id in known:
                    api.col.active_scenes[canvas] = scene_id
            api.host.commit("program")
    _back.clear()
    log.info("break guard: the break ended")
    _event(api, "break.ended", GUARD.answer(time.monotonic()))


def look_now(api) -> None:
    if api is None:
        return
    settings = _settings(api)
    if settings is None:
        return
    for event in GUARD.tick(settings, output_since("stream"), time.monotonic()):
        what = event[0]
        try:
            if what == "break_soon":
                _note(api, f"A break starts in {round(event[1] / 60)} minutes",
                      "Your Break bloom goes on air, then SprigCast puts things back.", "Not now",
                      lambda: GUARD.later(ASK_AGAIN_S, time.monotonic()))
            elif what == "ask_break":
                _note(api, "Time for a break?", f"{round(break_s(settings) / 60)} minutes on your Break bloom.",
                      "Take a break", lambda: begin_break(api, _settings(api) or settings))
            elif what == "break_start":
                begin_break(api, settings, break_s(settings) / 60)
            elif what == "break_end":
                finish_break(api, settings)
            elif what == "break_over":
                finish_break(api, settings, put_back=False)
            elif what == "end_soon":
                _note(api, "Almost time to end", f"Your stream limit is in {round(event[1] / 60)} minutes.",
                      "One more hour", lambda: GUARD.later(3600, time.monotonic(), end=True), ms=30000)
            elif what == "end":
                _event(api, "stream.limit", {})
                bloom = find_bloom(api, settings.get("end_bloom", ""), ("end", "outro")) \
                    if settings.get("auto_end", False) else None
                if bloom is not None:
                    api.bloom_show({"bloom": bloom.id})
                _note(api, "Time to end the stream", "You reached your stream limit.", "One more hour",
                      lambda: GUARD.later(3600, time.monotonic(), end=True), ms=60000)
        except ApiError as exc:
            log.info("break guard: %s", exc.message)


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
except Exception:  # pragma: no cover
    log.debug("break guard: no timer", exc_info=True)


# Apps, hotkeys and right-click ----------------------------------------------------------------------


def _need(api) -> dict[str, Any]:
    settings = _settings(api)
    if settings is None:
        raise ApiError("not_found", "Add Break & End Guard first (+ → Plugins → Break & End Guard).")
    return settings


def _number(data, key: str, default: float) -> float:
    value = data.get(key, default)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ApiError("bad_request", f"{key} must be a number.") from None
    if not (math.isfinite(value) and 0 < value <= 1440):
        raise ApiError("bad_request", f"{key} must be above 0 and at most 1440.")
    return value


def _start(api, data):
    settings = _need(api)
    minutes = _number(data, "minutes", break_s(settings) / 60)
    if GUARD.break_until is None:
        begin_break(api, settings, minutes)
    return GUARD.answer(time.monotonic())


def _end(api, data):
    settings = _need(api)
    if GUARD.stop(time.monotonic()):
        finish_break(api, settings)
    return GUARD.answer(time.monotonic())


def _toggle(api, data):
    return _end(api, data) if GUARD.break_until is not None else _start(api, {})


def _later(api, data):
    _need(api)
    GUARD.later(_number(data, "minutes", 30) * 60, time.monotonic())
    return GUARD.answer(time.monotonic())


def _end_later(api, data):
    _need(api)
    GUARD.later(_number(data, "minutes", 60) * 60, time.monotonic(), end=True)
    return GUARD.answer(time.monotonic())


def _get(api, data):
    _need(api)
    return GUARD.answer(time.monotonic())


register_action("break.start", "Start a break now: your Break bloom goes on air.",
                {"minutes": "?how long (default: Break minutes)"}, _start)
register_action("break.end", "End the break now and put back what was on air.", {}, _end)
register_action("break.later", "Put the next break off.", {"minutes": "?how many minutes (default 30)"}, _later)
register_action("end.later", "Move the stream limit later.", {"minutes": "?how many minutes (default 60)"}, _end_later)
register_action("break.get", "Whether you're on a break, and when the next break and the limit come (seconds).",
                {}, _get)

HOTKEYS = {"break:toggle": ("Break: start or end", _toggle), "break:later": ("Break: 30 minutes later", _later),
           "break:end_later": ("Stream limit: one more hour", _end_later)}


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
    if GUARD.break_until is None:
        add("Start a break", lambda: _start(api, {}))
    else:
        add("End the break", lambda: _end(api, {}))


register_item_menu(_menu)
