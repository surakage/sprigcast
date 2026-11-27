"""Name tag: a lower third with your name and a short line, that slides in when its bloom comes on screen.

Add Name tag (+ → Plugins → Name tag): it gets a sprig of its own with the card at the bottom left of your bloom.
Name ("Your name") big, Line ("Cozy games · SlothBloom") small under it. Each time its bloom comes on screen it
slides in from the left, stays Show for seconds (8), and slides out; Show for 0 keeps it up.

Show it again with a hotkey (Tools → Hotkeys → Plugins), a right-click on it, the Stream Deck or an app:
{"do": "nametag.show"}.

The card is SprigCast's clock card (core/cards.py) with a tag. It's drawn once; the slide moves that picture
(a few frames, on the pictures lane), so it costs next to nothing. Off screen it does nothing.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import math
import time
import weakref
from typing import Any

import numpy as np

from sprigcast.control.native import ApiError, register_action
from sprigcast.core import cards, painted
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import register_hotkeys, register_item_menu

log = logging.getLogger(__name__)

PLUGIN = {"id": "name_tag", "name": "Name tag", "version": 1, "adds": "Source",
          "about": "A lower third with your name and a short line, sliding in when its bloom comes on.",
          "new": "New: a name tag that slides in."}

KIND = "name_tag"
H = cards.H
W = 640
SLIDE_S = 0.45
_ALL: "weakref.WeakSet" = weakref.WeakSet()  # the name tags made, for nametag.show
STEPS = 12  # pictures in a slide


def show_for_s(settings: dict[str, Any]) -> float:
    try:
        value = float(settings.get("show_for", 8))
    except (TypeError, ValueError):
        return 8.0
    return max(0.0, min(600.0, value)) if math.isfinite(value) else 8.0


def step_at(since: float, settings: dict[str, Any]) -> int:
    """How far in the card is, 0 (out) to STEPS (in), this long after it was asked to show."""
    stay = show_for_s(settings)
    if since < 0:
        return 0
    if since < SLIDE_S:
        return max(1, round(STEPS * since / SLIDE_S))
    if stay <= 0 or since < SLIDE_S + stay:
        return STEPS
    out = since - SLIDE_S - stay
    return max(0, STEPS - round(STEPS * out / SLIDE_S)) if out < SLIDE_S else 0


def paint_fitted(name: str, line: str, settings: dict[str, Any], step: int, detail: float = 1.0):
    from PySide6.QtGui import QPainter

    look = cards.look_of(settings)
    shown = cards.Words(name, line, badge="tag")
    width = cards.card_width([shown], look)
    card = cards.paint(width, shown, detail=detail, look=look)
    if step >= STEPS:
        return card
    image, p = painted.canvas(width, H, detail)
    eased = 1 - (1 - step / STEPS) ** 3  # eases out as it arrives
    p.setOpacity(step / STEPS)
    p.resetTransform()
    p.drawImage(round(-card.width() * (1 - eased)), 0, card)
    p.end()
    return image


@register(KIND)
class NameTagSource(Source):
    label = "Name tag"
    defaults = {"name": "Your name", "line": "", "show_for": 8,
                "text_color": "#f2f7f3", "card_color": "#151c19e0", "accent_color": "#4fd88f", "font": "",
                "text_scale": 100, "corners": "leaf", "show_badge": True, "custom_look": ""}
    sleeps = True
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
        self._shown_at = time.monotonic()
        self._hidden_key: tuple | None = None
        self._w = W
        self.status = "ok"
        _ALL.add(self)

    @property
    def size(self) -> tuple[int, int]:
        return self._w, H

    def show_again(self) -> None:
        self._shown_at = time.monotonic()

    def wake(self) -> None:
        self.show_again()

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._hidden_key = None
        self._card.forget()

    def sleep(self) -> None:
        self._frame.release()

    def video_frame(self) -> np.ndarray | None:
        settings = self.settings
        step = step_at(time.monotonic() - self._shown_at, settings)
        if step == 0:
            key = ("hidden", self._w)
            if key == self._hidden_key:
                return None
            self._hidden_key = key
            self._card.drop()
            self._frame.release()
            return painted.clear_frame(self._w, H)
        self._hidden_key = None
        name = str(settings.get("name", "") or "").strip() or "Your name"
        line = str(settings.get("line", "") or "").strip()
        key = (name, line, step, cards.look_of(settings).key, self.detail)
        frame = self._card.picture(key, paint_fitted, name, line, dict(settings), step, self.detail)
        if frame is not None:
            self._w = int(frame.shape[1])
        return frame


def _show(api, data):
    if not any(s.kind == KIND for s in api.col.sources):
        raise ApiError("not_found", "Add Name tag first (+ → Plugins → Name tag).")
    for tag in list(_ALL):
        tag.show_again()
    return {"shown": True}


register_action("nametag.show", "Slide the name tag in again.", {}, _show)


def _hotkeys(collection):
    return [("nametag:show", "Name tag: show")] if any(s.kind == KIND for s in collection.sources) else []


def _run_hotkey(api, hid):
    if hid == "nametag:show":
        try:
            _show(api, {})
        except ApiError as exc:
            return exc.message
    return None


register_hotkeys(_hotkeys, _run_hotkey)


def _menu(api, source, add):
    if source is not None and source.kind == KIND:
        add("Show again", lambda: _show(api, {}))


register_item_menu(_menu)
