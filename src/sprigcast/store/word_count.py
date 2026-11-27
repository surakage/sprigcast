"""Chat word counter: how many times chat said a word or emote this stream ("GG × 42"), like Death count.

Add Chat word counter (+ → Plugins → Chat word counter): it gets a sprig of its own with the card at the top right
of your bloom. Word: what to count, "GG" to start; several with commas ("GG, GGs") all count together. It counts
whole words in any case, across Twitch, YouTube and Kick (the Chat panel's chat), once per message (Once per
message off: every time it's said). A new stream starts the count at 0.

From a hotkey (Tools → Hotkeys → Plugins), a right-click on it, the Stream Deck or an app:
    {"do": "wordcount.get"}                 {"count", "word"}
    {"do": "wordcount.add", "amount": 1}    more (or fewer, negative)
    {"do": "wordcount.reset"}               back to 0

It reads the chat SprigCast already has (no new sign-in) once a second, only while a counter is in your garden.
The card is SprigCast's clock card, drawn only when the number changes.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

import numpy as np

from sprigcast.control.native import ApiError, register_action
from sprigcast.core import cards, painted
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import output_since, register_hotkeys, register_item_menu

log = logging.getLogger(__name__)

PLUGIN = {"id": "word_count", "name": "Chat word counter", "version": 1, "adds": "Source",
          "about": "How many times chat said a word or emote this stream (\"GG × 42\"), on Twitch, YouTube and Kick.",
          "new": "New: counts a word or emote in chat."}

KIND = "word_count"
H = cards.H
W = 520
LOOK_EVERY_MS = 1000


def words_of(settings: dict[str, Any]) -> list[str]:
    return [w.strip() for w in str(settings.get("word", "GG") or "").split(",") if w.strip()][:10]


def pattern(words: list[str]):
    if not words:
        return None
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![\w]){alts}(?![\w])", re.IGNORECASE)


def hits(text: str, settings: dict[str, Any]) -> int:
    found = pattern(words_of(settings))
    if found is None:
        return 0
    count = len(found.findall(text or ""))
    return min(count, 1) if settings.get("once_per_message", True) else count


class Counter:
    def __init__(self):
        self.lock = threading.Lock()
        self.count = 0
        self.stream: float | None = None
        self.seen_at: float | None = None  # chat lines that arrived after this (time.monotonic()) are new

    def take(self, settings: dict[str, Any], lines, live_since: float | None) -> None:
        """Count the chat lines that came since the last look. The first look only marks where chat is."""
        with self.lock:
            if live_since != self.stream:
                self.stream = live_since
                if live_since is not None:
                    self.count = 0  # a new stream
            newest = max((line.at for line in lines), default=float("-inf"))
            if self.seen_at is None:
                self.seen_at = newest  # chat from before the counter was added isn't counted
                return
            for line in lines:
                if line.at > self.seen_at and not line.message.notice:
                    self.count += hits(line.message.text, settings)
            self.seen_at = max(self.seen_at, newest)

    def add(self, amount: int) -> None:
        with self.lock:
            self.count = max(0, self.count + amount)

    def reset(self) -> None:
        with self.lock:
            self.count = 0


COUNTER = Counter()
_version = -1


def look_now(api) -> None:
    global _version
    if api is None:
        return
    found = [s for s in api.col.sources if s.kind == KIND]
    if not found:
        return
    from sprigcast.chat.feed import FEED

    if FEED.version == _version and COUNTER.stream == output_since("stream"):
        return
    _version, lines = FEED.snapshot()
    COUNTER.take({**WordCountSource.defaults, **found[0].settings}, lines, output_since("stream"))


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
    log.debug("word count: no timer", exc_info=True)


def big_text(settings: dict[str, Any], count: int) -> str:
    words = words_of(settings)
    return f"{words[0] if words else '?'}  ×  {count:,}"


def paint_fitted(big: str, small: str, settings: dict[str, Any], detail: float = 1.0):
    look = cards.look_of(settings)
    shown = cards.Words(big, small, badge="bubble")
    widest = cards.Words(cards.even(big) if any(c.isdigit() for c in big) else big, small)
    return cards.paint(cards.card_width([widest], look), shown, detail=detail, look=look)


@register(KIND)
class WordCountSource(Source):
    label = "Chat word counter"
    defaults = {"word": "GG", "once_per_message": True, "label": "said in chat this stream",
                "text_color": "#f2f7f3", "card_color": "#151c19e0", "accent_color": "#4fd88f", "font": "",
                "text_scale": 100, "corners": "leaf", "show_badge": True, "custom_look": ""}
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
        self._w = W
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return self._w, H

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._card.forget()

    def sleep(self) -> None:
        self._frame.release()

    def video_frame(self) -> np.ndarray | None:
        settings = self.settings
        big = big_text(settings, COUNTER.count)
        small = str(settings.get("label", "") or "").strip()
        key = (big, small, cards.look_of(settings).key, self.detail)
        frame = self._card.picture(key, paint_fitted, big, small, dict(settings), self.detail)
        if frame is not None:
            self._w = int(frame.shape[1])
        return frame


def _need(api) -> dict[str, Any]:
    found = [s for s in api.col.sources if s.kind == KIND]
    if not found:
        raise ApiError("not_found", "Add Chat word counter first (+ → Plugins → Chat word counter).")
    return {**WordCountSource.defaults, **found[0].settings}


def _answer(settings):
    words = words_of(settings)
    return {"count": COUNTER.count, "word": words[0] if words else ""}


def _get(api, data):
    return _answer(_need(api))


def _add(api, data):
    settings = _need(api)
    try:
        amount = int(float(data.get("amount", 1)))
    except (TypeError, ValueError):
        raise ApiError("bad_request", "amount must be a number.") from None
    COUNTER.add(amount)
    return _answer(settings)


def _reset(api, data):
    settings = _need(api)
    COUNTER.reset()
    return _answer(settings)


register_action("wordcount.get", "How many times chat said the word this stream.", {}, _get)
register_action("wordcount.add", "Add to the chat word count (negative takes some off).",
                {"amount": "?how many (default 1)"}, _add)
register_action("wordcount.reset", "The chat word count back to 0.", {}, _reset)

HOTKEYS = {"wordcount:add": ("Chat word counter: add one", _add), "wordcount:reset": ("Chat word counter: reset", _reset)}


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
    add("Add one", lambda: _add(api, {}))
    add("Reset", lambda: _reset(api, {}))


register_item_menu(_menu)
