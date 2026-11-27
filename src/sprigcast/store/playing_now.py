"""Playing now: the category (game) you're streaming in, in a small card, kept up to date by itself.

Add Playing now (+ → Plugins → Playing now): it gets a sprig of its own with the card at the top left of your bloom.
It reads your category from Twitch (or Kick: From) with the sign-in SprigCast already has for Stream details, about
once a minute and only while a Playing now card is in your garden, so a change in Stream details (or on the
platform's own site) shows by itself. Text: your own words instead ("Just chatting with you all"). With nothing to
show (no category, not signed in) the card hides.

The card is SprigCast's clock card with a controller, drawn only when the category changes.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np

from sprigcast.core import cards, painted
from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

PLUGIN = {"id": "playing_now", "name": "Playing now", "version": 1, "adds": "Source",
          "about": "The category (game) you're streaming in, from Twitch or Kick, kept up to date by itself.",
          "new": "New: your category on stream, with nothing to set up."}

KIND = "playing_now"
H = cards.H
W = 560
LOOK_EVERY_MS = 5000
READ_EVERY_S = 60.0
FROM = (("twitch", "Twitch"), ("kick", "Kick"))


def _twitch() -> str:
    from sprigcast.output import twitch_auth

    rows = twitch_auth.helix_get(lambda uid: f"/channels?broadcaster_id={uid}").get("data") or []
    return str((rows[0] if rows else {}).get("game_name") or "")


def _kick() -> str:
    from sprigcast.output import stream_details as sd

    def call(token: str) -> str:
        rows = sd._call("GET", f"{sd.KICK_API}/channels", token, who="Kick").get("data") or []
        row = rows[0] if rows and isinstance(rows[0], dict) else {}
        category = row.get("category") if isinstance(row.get("category"), dict) else {}
        return str(category.get("name") or "")

    return sd._with_token("kick", call)


READERS = {"twitch": _twitch, "kick": _kick}


class Category:
    def __init__(self):
        self.lock = threading.Lock()
        self.name = ""
        self.where = ""
        self.read_at = -1e9
        self.busy = False

    def want(self, where: str) -> None:
        with self.lock:
            if where != self.where:
                self.where, self.name, self.read_at = where, "", -1e9
            if self.busy or time.monotonic() - self.read_at < READ_EVERY_S:
                return
            self.busy = True
        threading.Thread(target=self._read, args=(where,), name="playing-now", daemon=True).start()

    def _read(self, where: str) -> None:
        try:
            name = READERS.get(where, _twitch)()
        except Exception as exc:  # noqa: BLE001 (signed out, offline: the card hides, tried again later)
            log.info("playing now: couldn't read the category: %s", type(exc).__name__)
            name = ""
        with self.lock:
            if where == self.where:
                self.name = name.strip()[:80]
            self.read_at, self.busy = time.monotonic(), False


CATEGORY = Category()


def look_now(api) -> None:
    if api is None:
        return
    found = [s for s in api.col.sources if s.kind == KIND and not str(s.settings.get("text", "")).strip()]
    if found:
        CATEGORY.want(str(found[0].settings.get("from", "twitch")))


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
    log.debug("playing now: no timer", exc_info=True)


def shown_text(settings: dict[str, Any]) -> str:
    own = str(settings.get("text", "") or "").strip()
    return own or CATEGORY.name


def paint_fitted(big: str, small: str, settings: dict[str, Any], detail: float = 1.0):
    look = cards.look_of(settings)
    shown = cards.Words(big, small, small_on_top=True, badge="gamepad")
    return cards.paint(cards.card_width([shown], look), shown, detail=detail, look=look)


@register(KIND)
class PlayingNowSource(Source):
    label = "Playing now"
    defaults = {"from": "twitch", "text": "", "label": "Playing now",
                "text_color": "#f2f7f3", "card_color": "#151c19e0", "accent_color": "#4fd88f", "font": "",
                "text_scale": 100, "corners": "leaf", "show_badge": True, "custom_look": ""}
    choices = {"from": FROM}
    sleeps = True
    keeps_picture = True
    sharp = True
    own_sprig = (W, H)
    own_sprig_corner = "top_left"
    own_sprig_height = 0.12
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
        big = shown_text(settings)
        if not big:
            key = ("hidden", self._w)
            if key == self._hidden_key:
                return None
            self._hidden_key = key
            self._card.drop()
            self._frame.release()
            return painted.clear_frame(self._w, H)
        self._hidden_key = None
        small = str(settings.get("label", "") or "").strip()
        key = (big, small, cards.look_of(settings).key, self.detail)
        frame = self._card.picture(key, paint_fitted, big, small, dict(settings), self.detail)
        if frame is not None:
            self._w = int(frame.shape[1])
        return frame
