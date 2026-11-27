"""Socials: your handles one at a time, each with its platform's icon, in a small card on stream.

Add Socials (+ → Plugins → Socials): it gets a sprig of its own with the card at the bottom left of your bloom.

Socials: one line each, the platform, a colon, then what to show:
    Twitch: @Suraruisuh
    YouTube: @Suraruisuh
    Website: slothbloom.com
Platforms with an icon: Facebook, GitHub, Instagram, Kick, Patreon, PlayStation, Reddit, Snapchat, Steam, TikTok,
Twitch, X (or Twitter), Xbox and YouTube; anything else gets the globe (a website). The small words under the handle
are the platform's name, or Label words of your own after a second colon ("Discord: sloths.gg: Join us").

Every: seconds each one shows (8). Brand colors: the card's rim takes the platform's color.

The icons are vectors (sprigcast/store/assets/socials), sharp at any size. Brand shapes from Simple Icons (CC0);
each logo is its owner's trademark, shown here to point at your own accounts. The card is SprigCast's clock card
(core/cards.py), drawn only when it changes to the next handle. Off screen it does nothing.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import math
import time
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.core import cards, painted
from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

PLUGIN = {"id": "socials", "name": "Socials", "version": 1, "adds": "Source",
          "about": "Your handles one at a time, each with its platform's icon, in a small card on stream.",
          "new": "New: your socials, one at a time, with official platform icons."}

KIND = "socials"
H = cards.H
W = 640
ASSETS = Path(__file__).resolve().parent / "assets" / "socials"

PLATFORMS = {  # key -> (name shown, icon, brand color)
    "facebook": ("Facebook", "facebook", "#1877f2"), "github": ("GitHub", "github", "#c9d1d9"),
    "instagram": ("Instagram", "instagram", "#e1306c"), "kick": ("Kick", "kick", "#53fc18"),
    "patreon": ("Patreon", "patreon", "#f96854"), "playstation": ("PlayStation", "playstation", "#0070d1"),
    "psn": ("PlayStation", "playstation", "#0070d1"), "reddit": ("Reddit", "reddit", "#ff4500"),
    "snapchat": ("Snapchat", "snapchat", "#fffc00"), "steam": ("Steam", "steam", "#66c0f4"),
    "tiktok": ("TikTok", "tiktok", "#25f4ee"), "twitch": ("Twitch", "twitch", "#a970ff"),
    "x": ("X", "x", "#e7e9ea"), "twitter": ("X", "x", "#e7e9ea"), "xbox": ("Xbox", "xbox", "#107c10"),
    "youtube": ("YouTube", "youtube", "#ff5a52"),
}
WEBSITE = ("Website", "website", "#4fd88f")
DEFAULT_LIST = "Twitch: @yourname\nYouTube: @yourname\nWebsite: yoursite.com"


def _assets_dir() -> Path:
    if (ASSETS / "website.svg").exists():
        return ASSETS
    from sprigcast import plugin_store  # installed in the plugins folder: the art stays with SprigCast

    return plugin_store.STORE_DIR / "assets" / "socials"


def entries(text: str) -> list[dict[str, str]]:
    """The lines of Socials: [{"handle", "label", "icon", "color"}]. Blank and handle-less lines are left out."""
    out = []
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        platform, rest = line.split(":", 1)
        if ":" in rest and not rest.strip().lower().startswith(("http", "//")):
            handle, label = rest.split(":", 1)
        else:
            handle, label = rest, ""
        handle = handle.strip()
        if not handle:
            continue
        name, icon, color = PLATFORMS.get(platform.strip().lower().replace(" ", ""), (platform.strip() or WEBSITE[0],
                                                                                  *WEBSITE[1:]))
        out.append({"handle": handle[:60], "label": (label.strip() or name)[:60], "icon": icon, "color": color})
    return out


def every_s(settings: dict[str, Any]) -> float:
    try:
        value = float(settings.get("every", 8))
    except (TypeError, ValueError):
        return 8.0
    return max(2.0, min(120.0, value)) if math.isfinite(value) else 8.0


def which(count: int, settings: dict[str, Any], now: float) -> int:
    return int(now // every_s(settings)) % count if count else 0


def words_for(entry: dict[str, str]) -> cards.Words:
    return cards.Words(entry["handle"], entry["label"], art=str(_assets_dir() / f"{entry['icon']}.svg"))


def paint_fitted(shown: list[dict[str, str]], index: int, settings: dict[str, Any], detail: float = 1.0):
    """The card for shown[index], as wide as the widest handle (so the box stays put as they change)."""
    entry = shown[index]
    if settings.get("brand_colors", True):
        settings = {**settings, "accent_color": entry["color"]}
    look = cards.look_of(settings)
    width = cards.card_width([words_for(e) for e in shown], look)
    return cards.paint(width, words_for(entry), detail=detail, look=look)


@register(KIND)
class SocialsSource(Source):
    label = "Socials"
    defaults = {"socials": DEFAULT_LIST, "every": 8, "brand_colors": True,
                "text_color": "#f2f7f3", "card_color": "#151c19e0", "accent_color": "#4fd88f", "font": "",
                "text_scale": 100, "corners": "leaf", "show_badge": True, "custom_look": ""}
    sleeps = True
    keeps_picture = True
    sharp = True
    own_sprig = (W, H)
    own_sprig_corner = "bottom_left"
    own_sprig_height = 0.12
    channel_order = painted.Frame.channel_order

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame = painted.Frame()
        self._card = painted.Aside(self._frame)
        self._list = entries(self.settings.get("socials", DEFAULT_LIST))
        self._empty = False
        self._w = W
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return self._w, H

    def update(self, settings: dict[str, Any]) -> None:
        super().update(settings)
        self._list = entries(self.settings.get("socials", DEFAULT_LIST))
        self._empty = False
        self._card.forget()

    def sleep(self) -> None:
        self._frame.release()

    def video_frame(self) -> np.ndarray | None:
        settings = self.settings
        if not self._list:
            self.status = "add a line to Socials, like Twitch: @yourname"
            if self._empty:
                return None
            self._empty = True
            return painted.clear_frame(self._w, H)
        self.status = "ok"
        index = which(len(self._list), settings, time.time())
        key = (tuple(tuple(e.values()) for e in self._list), index, bool(settings.get("brand_colors", True)),
               cards.look_of(settings).key, self.detail)
        frame = self._card.picture(key, paint_fitted, list(self._list), index, dict(settings), self.detail)
        if frame is not None:
            self._w = int(frame.shape[1])
        return frame
