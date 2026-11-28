"""Emotes the platforms don't send: 7TV, BetterTTV and FrankerFaceZ (user 2026-11-27).

Most Twitch chats use one of these three browser add-ons: a word like "catJAM" is a moving picture for everyone
who has the add-on, and plain text for everyone else, SprigCast's chat included. Their emote lists are public
and need no sign-in, so the chat hub (chat/hub.py) looks up each chat's list here and marks those words as emotes
on every message, for the Chat panel and the Chat source alike.

- Twitch: 7TV, BetterTTV and FrankerFaceZ, each one's emotes for everyone plus the channel's own.
- Kick and YouTube: 7TV (the only one of the three that works there).

The lists are fetched on a helper thread, once per chat, and again after AGAIN_S if one couldn't be read.
Plain Python (no Qt): the parsing is checked in tests/test_extra_emotes.py.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from sprigcast.chat import ChatMessage

log = logging.getLogger(__name__)

AGAIN_S = 600.0  # a list that couldn't be read is asked for again this much later
FRESH_S = 3 * 3600.0  # ... and one that could, this much later (a channel adds emotes now and then)
TIMEOUT_S = 10
MOST = 4000  # emotes kept for one chat (the biggest 7TV sets hold about 1,000)
SEVENTV_GLOBAL = "https://7tv.io/v3/emote-sets/global"
SEVENTV_CHANNEL = "https://7tv.io/v3/users/{platform}/{room}"
BTTV_GLOBAL = "https://api.betterttv.net/3/cached/emotes/global"
BTTV_CHANNEL = "https://api.betterttv.net/3/cached/users/twitch/{room}"
FFZ_GLOBAL = "https://api.frankerfacez.com/v1/set/global"
FFZ_CHANNEL = "https://api.frankerfacez.com/v1/room/id/{room}"
HOSTS = ("https://cdn.7tv.app/", "https://cdn.betterttv.net/", "https://cdn.frankerfacez.com/")


@dataclass(frozen=True)
class Emote:
    key: str  # "7tv-<id>", "bttv-<id>" or "ffz-<id>": what a message's emotes and images call it
    url: str
    ratio: float = 1.0  # width / height (many 7TV emotes are wide)


def _ratio(width, height) -> float:
    try:
        return max(0.5, min(4.0, float(width) / float(height)))
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0


def _safe(key: str) -> bool:
    return 0 < len(key) <= 64 and all(c.isalnum() or c in "-_" for c in key)


def parse_7tv(data) -> dict[str, Emote]:
    """A 7TV emote set (or a user with one) -> name -> Emote."""
    if isinstance(data, dict) and isinstance(data.get("emote_set"), dict):
        data = data["emote_set"]
    found: dict[str, Emote] = {}
    for entry in (data.get("emotes") if isinstance(data, dict) else None) or []:
        try:
            name, inner = str(entry["name"]), entry["data"]
            host = inner["host"]
            base = str(host["url"])
            files = [f for f in host.get("files") or [] if str(f.get("format", "")).upper() == "WEBP"]
            two = next((f for f in files if str(f.get("name")) == "2x.webp"), files[0] if files else {})
        except (KeyError, TypeError, IndexError):
            continue
        key = f"7tv-{entry.get('id') or inner.get('id')}"
        url = ("https:" + base if base.startswith("//") else base) + "/2x.webp"
        if name and _safe(key) and url.startswith(HOSTS[0]):
            found[name] = Emote(key, url, _ratio(two.get("width"), two.get("height")))
    return found


def parse_bttv(data) -> dict[str, Emote]:
    """BetterTTV's list for everyone (a bare list) or a channel's (channelEmotes and sharedEmotes)."""
    if isinstance(data, dict):
        data = [*(data.get("channelEmotes") or []), *(data.get("sharedEmotes") or [])]
    found: dict[str, Emote] = {}
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict) or entry.get("modifier"):
            continue  # "w!", "h!" and the like change the emote before them: left as text
        key, name = f"bttv-{entry.get('id')}", str(entry.get("code") or "")
        if name and _safe(key):
            found[name] = Emote(key, f"{HOSTS[1]}emote/{entry.get('id')}/2x",
                                _ratio(entry.get("width") or 1, entry.get("height") or 1))
    return found


def parse_ffz(data, only: list | None = None) -> dict[str, Emote]:
    """FrankerFaceZ sets -> name -> Emote. `only`: the set ids that count (its global answer lists other sets
    too: effects that change another emote); None takes default_sets, or a room's own set."""
    if not isinstance(data, dict) or not isinstance(data.get("sets"), dict):
        return {}
    if only is None:
        only = data.get("default_sets")
    if only is None and isinstance(data.get("room"), dict):
        only = [data["room"].get("set")]
    found: dict[str, Emote] = {}
    for set_id, one in data["sets"].items():
        if only is not None and str(set_id) not in {str(v) for v in only}:
            continue
        for entry in (one.get("emoticons") if isinstance(one, dict) else None) or []:
            if not isinstance(entry, dict) or entry.get("modifier"):
                continue
            moving = entry.get("animated") if isinstance(entry.get("animated"), dict) else {}
            urls = entry.get("urls") if isinstance(entry.get("urls"), dict) else {}
            url = str(moving.get("2") or moving.get("1") or urls.get("2") or urls.get("1") or "")
            if url.startswith("//"):
                url = "https:" + url
            key, name = f"ffz-{entry.get('id')}", str(entry.get("name") or "")
            if name and _safe(key) and url.startswith(HOSTS[2]):
                found[name] = Emote(key, url, _ratio(entry.get("width"), entry.get("height")))
    return found


def lists_for(platform: str, room: str) -> list[tuple[str, Callable, str]]:
    """(what, parser, address) for one chat, the least important first: a channel's own emote wins over one for
    everyone with the same name, and 7TV over BetterTTV over FrankerFaceZ (the order the add-ons use)."""
    room_ok = room.isascii() and room.replace("-", "").replace("_", "").isalnum() and len(room) <= 64
    lists: list[tuple[str, Callable, str]] = []
    if platform == "twitch":
        lists += [("FrankerFaceZ", parse_ffz, FFZ_GLOBAL), ("BetterTTV", parse_bttv, BTTV_GLOBAL)]
    lists.append(("7TV", parse_7tv, SEVENTV_GLOBAL))
    if room and room_ok:
        if platform == "twitch":
            lists += [("FrankerFaceZ channel", parse_ffz, FFZ_CHANNEL.format(room=room)),
                      ("BetterTTV channel", parse_bttv, BTTV_CHANNEL.format(room=room))]
        if platform in ("twitch", "kick", "youtube"):
            lists.append(("7TV channel", parse_7tv, SEVENTV_CHANNEL.format(platform=platform, room=room)))
    return lists


def fetch_json(url: str):
    """The address's JSON; None when that chat has no list there (404); raises OSError when it can't be read."""
    request = urllib.request.Request(url, headers={"User-Agent": "SprigCast", "Accept": "application/json"})  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:  # noqa: S310 (the https addresses above)
            return json.loads(reply.read(8_000_000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    except ValueError as exc:
        raise OSError(f"not JSON: {exc}") from exc


def mark(message: ChatMessage, emotes: dict[str, Emote]) -> int:
    """Mark the words of `message` that are emotes (whole words, capitals as written, outside the emotes the
    platform already marked). Returns how many."""
    if not emotes or message.notice or not message.text:
        return 0
    text = message.text
    taken = sorted((s, e) for s, e, _k in message.emotes)
    added, at, n = [], 0, len(text)
    while at < n:
        if text[at] == " ":
            at += 1
            continue
        end = text.find(" ", at)
        end = n if end < 0 else end
        emote = emotes.get(text[at:end])
        if emote is not None and not any(s <= end - 1 and e >= at for s, e in taken):
            added.append((at, end - 1, emote.key))
            message.images[emote.key] = emote.url
            if emote.ratio != 1.0:
                message.ratios[emote.key] = emote.ratio
        at = end + 1
    if added:
        message.emotes = sorted([*message.emotes, *added])
    return len(added)


class ExtraEmotes:
    """Each chat's extra emotes: `want(platform, room)` has them looked up, `apply(message)` marks them."""

    def __init__(self, fetch: Callable = fetch_json, clock: Callable[[], float] = time.monotonic):
        self._fetch, self._clock = fetch, clock
        self._lock = threading.Lock()
        self._sets: dict[tuple[str, str], dict[str, Emote]] = {}  # (platform, room) -> name -> Emote
        self._rooms: dict[str, str] = {}  # platform -> the room its messages are marked from
        self._again: dict[tuple[str, str], float] = {}
        self._loading: set[tuple[str, str]] = set()
        self.enabled = True

    def want(self, platform: str, room: str = "", wait: bool = False) -> None:
        """This platform's chat is `room` (its id there; "" while not known: the emotes for everyone)."""
        chat = (platform, room)
        with self._lock:
            self._rooms[platform] = room
            if not self.enabled or chat in self._loading or self._clock() < self._again.get(chat, 0.0):
                return
            self._loading.add(chat)
        if wait:
            self._load(chat)
        else:
            threading.Thread(target=self._load, args=(chat,), name="chat-extra-emotes", daemon=True).start()

    def _load(self, chat: tuple[str, str]) -> None:
        found: dict[str, Emote] = {}
        failed = []
        for what, parse, url in lists_for(*chat):
            try:
                data = self._fetch(url)
                if data is not None:
                    found.update(parse(data))
            except Exception as exc:  # noqa: BLE001 (unreachable, or an answer in another shape: the rest still count)
                failed.append(f"{what} ({type(exc).__name__})")
        if len(found) > MOST:
            found = dict(list(found.items())[-MOST:])
        with self._lock:
            if found or chat not in self._sets:
                self._sets[chat] = found
            self._again[chat] = self._clock() + (AGAIN_S if failed else FRESH_S)
            self._loading.discard(chat)
        # (in the logbook: counts only, no channel names)
        log.info("chat %s: %d extra emotes (7TV, BetterTTV, FrankerFaceZ)%s", chat[0], len(found),
                 f"; couldn't read: {', '.join(failed)}" if failed else "")

    def emotes(self, platform: str) -> dict[str, Emote]:
        with self._lock:
            room = self._rooms.get(platform, "")
            return self._sets.get((platform, room)) or self._sets.get((platform, "")) or {}

    def apply(self, message: ChatMessage) -> int:
        if not self.enabled:
            return 0
        return mark(message, self.emotes(message.platform))


EXTRA = ExtraEmotes()
