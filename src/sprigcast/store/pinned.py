"""Pinned message: a chat message you pick, on stream until you take it down.

Add Pinned message (+ → Plugins → Pinned message): it gets a sprig of its own with the card at the top of your
bloom. Then right-click any line in the Chat panel and pick Pin on stream: the card shows the message, with who
said it above. Unpin it with a right-click on the card (or in the Chat panel), a hotkey, or after Unpin after
minutes (0 keeps it up). With nothing pinned the card hides.

Apps and the Stream Deck:
    {"do": "pin.set", "text": "Welcome in!", "user": "Suraruisuh"}   pin your own words
    {"do": "pin.clear"}                                              take it down
    {"do": "pin.get"}                                                {"text", "user"} or nulls

The card is SprigCast's clock card (core/cards.py) with a pin. A long message gets smaller to fit, then is cut.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

from sprigcast.control.native import ApiError, register_action
from sprigcast.core import cards, painted
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import register_chat_menu, register_hotkeys, register_item_menu

log = logging.getLogger(__name__)

PLUGIN = {"id": "pinned", "name": "Pinned message", "version": 1, "adds": "Source",
          "about": "A chat message you pick (right-click it in the Chat panel), on stream until you unpin it.",
          "new": "New: pin a chat message on stream."}

KIND = "pinned"
H = cards.H
W = 900
MOST_LETTERS = 200

PIN: dict[str, Any] = {"text": "", "user": "", "at": 0.0}  # swapped whole


def pin(text: str, user: str = "") -> None:
    globals()["PIN"] = {"text": " ".join(str(text or "").split())[:MOST_LETTERS],
                        "user": str(user or "").strip()[:40], "at": time.monotonic()}


def unpin() -> None:
    globals()["PIN"] = {"text": "", "user": "", "at": 0.0}


def showing(settings: dict[str, Any], now: float) -> dict[str, Any] | None:
    current = PIN
    if not current["text"]:
        return None
    try:
        minutes = float(settings.get("unpin_after", 0) or 0)
    except (TypeError, ValueError):
        minutes = 0.0
    if minutes > 0 and now - current["at"] >= minutes * 60:
        return None
    return current


def paint_fitted(big: str, small: str, settings: dict[str, Any], detail: float = 1.0):
    look = cards.look_of(settings)
    shown = cards.Words(big, small, small_on_top=True, badge="pin", shrink=0.5)
    return cards.paint(cards.card_width([shown], look), shown, detail=detail, look=look)


@register(KIND)
class PinnedSource(Source):
    label = "Pinned message"
    defaults = {"unpin_after": 0, "text_color": "#f2f7f3", "card_color": "#151c19e0", "accent_color": "#4fd88f",
                "font": "", "text_scale": 100, "corners": "leaf", "show_badge": True, "custom_look": ""}
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
        current = showing(settings, time.monotonic())
        if current is None:
            key = ("hidden", self._w)
            if key == self._hidden_key:
                return None
            self._hidden_key = key
            self._card.drop()
            self._frame.release()
            return painted.clear_frame(self._w, H)
        self._hidden_key = None
        key = (current["text"], current["user"], cards.look_of(settings).key, self.detail)
        frame = self._card.picture(key, paint_fitted, current["text"], current["user"], dict(settings), self.detail)
        if frame is not None:
            self._w = int(frame.shape[1])
        return frame


def _answer():
    current = PIN
    return {"text": current["text"] or None, "user": current["user"] or None}


def _set(api, data):
    text = str(data.get("text") or "").strip()
    if not text:
        raise ApiError("bad_request", "text must say what to pin.")
    pin(text, str(data.get("user") or ""))
    return _answer()


def _clear(api, data):
    unpin()
    return _answer()


register_action("pin.set", "Pin a message on stream (Pinned message).",
                {"text": "the message", "user": "?who said it"}, _set)
register_action("pin.clear", "Take the pinned message down.", {}, _clear)
register_action("pin.get", "The pinned message, if any.", {}, lambda api, data: _answer())


def _chat_menu(message, add):
    if getattr(message, "notice", False) or not getattr(message, "text", ""):
        return
    add("Pin on stream", lambda: pin(message.text, message.user))
    if PIN["text"]:
        add("Unpin from stream", unpin)


register_chat_menu(_chat_menu)


def _menu(api, source, add):
    if source is not None and source.kind == KIND and PIN["text"]:
        add("Unpin", unpin)


register_item_menu(_menu)


def _hotkeys(collection):
    return [("pinned:clear", "Pinned message: unpin")] if any(s.kind == KIND for s in collection.sources) else []


def _run_hotkey(api, hid):
    if hid == "pinned:clear":
        unpin()
    return None


register_hotkeys(_hotkeys, _run_hotkey)
