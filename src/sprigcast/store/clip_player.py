"""Random clip player: your own Twitch clips, played one after another by SprigCast itself (no browser window,
no black box), with a small title card.

Add Random clip player (+ → Plugins → Random clip player) to the blooms it should play on, your Break and Ending
blooms say: it plays while its bloom is on screen and you're live (Only while live), a clip, a short gap (Gap),
the next. Shuffled, no repeats until every clip has played. Its sound goes to the mixer like any video.

Add Clip title too (+ → Plugins → Clip title) for the card that names each clip and who clipped it, for a few
seconds as it starts (Show for).

Where the clips come from: the newest Clips (20) on your Twitch channel, Shortest to Longest seconds (5 to 60),
read with the Twitch sign-in SprigCast already has. Twitch hands over a clip's video only to the channel's owner
and with its channel:manage:clips permission: if you connected Twitch before SprigCast asked for it, connect
Twitch once more in Settings → Stream (its status says so). Each clip is fetched on a worker just before it plays
and kept a while in SprigCast's clip folder (the newest 30, none older than a day), so nothing waits on the web.

Apps: {"do": "clips.next"} skips to the next clip, {"do": "clips.get"} says what's playing ({"title", "by"}).

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.control.native import ApiError, register_action
from sprigcast.core import cards, painted
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import output_since
from sprigcast.sources.media import MediaSource

log = logging.getLogger(__name__)

PLUGIN = {"id": "clip_player", "name": "Random clip player", "version": 1, "adds": "Source",
          "about": "Your own Twitch clips, played one after another by SprigCast, with a title card.",
          "new": "New: plays your Twitch clips itself, no browser window."}

KIND = "clip_player"
TITLE_KIND = "clip_title"
H = cards.H
W = 640
LIST_EVERY_S = 15 * 60.0
FAILED_AGAIN_S = 120.0
MOST_BYTES = 60 * 1024 * 1024
KEEP_FILES = 30
KEEP_S = 24 * 3600.0
TIMEOUT = 20
HOSTS = (".twitch.tv", ".twitchcdn.net", ".ttvnw.net", ".jtvnw.net", ".cloudfront.net")


def _number(settings: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    try:
        value = float(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value)) if value == value else default


def cache_dir() -> Path:
    try:
        from PySide6.QtCore import QStandardPaths

        base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation))
    except Exception:  # noqa: BLE001
        base = Path.home() / ".sprigcast"
    return base / "Clip player"


def allowed(url: str) -> bool:
    """Only Twitch's own video hosts, over https."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h[1:] or host.endswith(h) for h in HOSTS)


# The clips: listed, shuffled, fetched one ahead on a worker ------------------------------------------


class Pool:
    def __init__(self):
        self.lock = threading.Lock()
        self.clips: list[dict] = []  # {"id", "title", "by", "seconds"}
        self.bag: list[str] = []
        self.listed_at = -1e9
        self.ready: dict | None = None  # {"path", "title", "by"}
        self.busy = False
        self.problem = ""
        self.wish: tuple = (20, 5.0, 60.0)

    def pick(self) -> dict | None:
        """The next clip from the shuffled bag (a new shuffle once it's empty), or None."""
        with self.lock:
            if not self.clips:
                return None
            ids = {c["id"] for c in self.clips}
            self.bag = [i for i in self.bag if i in ids]
            if not self.bag:
                self.bag = list(ids)
                random.shuffle(self.bag)
            wanted = self.bag.pop()
            return next(c for c in self.clips if c["id"] == wanted)

    def take(self) -> dict | None:
        with self.lock:
            ready, self.ready = self.ready, None
            return ready

    def want(self, settings: dict[str, Any]) -> None:
        """Fetch the next clip on a worker if none is ready (and the list, when it's old)."""
        wish = (int(_number(settings, "count", 20, 1, 100)), _number(settings, "shortest", 5, 1, 60),
                _number(settings, "longest", 60, 5, 120))
        with self.lock:
            if self.busy or self.ready is not None:
                return
            if wish != self.wish:
                self.wish, self.listed_at = wish, -1e9
            self.busy = True
        threading.Thread(target=self._work, name="clip-player", daemon=True).start()

    def _work(self) -> None:
        from sprigcast.output import twitch_auth

        try:
            if time.monotonic() - self.listed_at >= LIST_EVERY_S:
                self._list(twitch_auth)
            clip = self.pick()
            if clip is None:
                raise LookupError("No clips on your Twitch channel fit Shortest and Longest yet.")
            path = self._fetch(twitch_auth, clip)
            with self.lock:
                self.ready, self.problem = {"path": str(path), "title": clip["title"], "by": clip["by"]}, ""
        except Exception as exc:  # noqa: BLE001 (signed out, offline, no clips: tried again later)
            log.info("clip player: couldn't get a clip ready: %s", type(exc).__name__)
            with self.lock:
                self.problem = str(exc) if isinstance(exc, (twitch_auth.TwitchAuthError, LookupError)) \
                    else "Couldn't reach Twitch."
                self.listed_at = min(self.listed_at, time.monotonic() - LIST_EVERY_S + FAILED_AGAIN_S)
            time.sleep(FAILED_AGAIN_S / 4)  # a pause before the next try
        finally:
            with self.lock:
                self.busy = False

    def _list(self, twitch_auth) -> None:
        count, shortest, longest = self.wish
        data = twitch_auth.helix_get(lambda uid: f"/clips?broadcaster_id={uid}&first={count}")
        clips = []
        for row in data.get("data") or []:
            try:
                seconds = float(row.get("duration") or 0)
            except (TypeError, ValueError):
                continue
            if row.get("id") and shortest <= seconds <= longest:
                clips.append({"id": str(row["id"]), "title": str(row.get("title") or "").strip(),
                              "by": str(row.get("creator_name") or "").strip(), "seconds": seconds,
                              "at": str(row.get("created_at") or "")})
        clips.sort(key=lambda c: c["at"], reverse=True)
        with self.lock:
            self.clips, self.listed_at = clips[:count], time.monotonic()

    def _fetch(self, twitch_auth, clip: dict) -> Path:
        folder = cache_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (hashlib.sha256(clip["id"].encode()).hexdigest()[:24] + ".mp4")
        if path.exists() and path.stat().st_size > 0:
            path.touch()
            return path
        quoted = urllib.parse.quote(clip["id"])
        data = twitch_auth.helix_get(
            lambda uid: f"/clips/downloads?editor_id={uid}&broadcaster_id={uid}&clip_id={quoted}",
            twitch_auth.CLIPS_SCOPE)
        rows = data.get("data") or []
        url = str((rows[0] if rows else {}).get("landscape_download_url") or "")
        if not allowed(url):
            raise LookupError("Twitch didn't hand over that clip's video.")
        part = path.with_suffix(".part")
        with urllib.request.urlopen(urllib.request.Request(url), timeout=TIMEOUT) as response, open(part, "wb") as out:
            got = 0
            while chunk := response.read(256 * 1024):
                got += len(chunk)
                if got > MOST_BYTES:
                    raise LookupError("That clip is too big to keep.")
                out.write(chunk)
        part.replace(path)
        tidy(folder)
        return path


def tidy(folder: Path, now: float | None = None) -> None:
    """Keep the newest KEEP_FILES clips, none older than KEEP_S."""
    now = time.time() if now is None else now
    try:
        files = sorted(folder.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for i, f in enumerate(files):
        try:
            if i >= KEEP_FILES or now - f.stat().st_mtime > KEEP_S:
                f.unlink()
        except OSError:
            pass
    for f in folder.glob("*.part"):
        try:
            if now - f.stat().st_mtime > 3600:
                f.unlink()
        except OSError:
            pass


POOL = Pool()
NOW: dict[str, Any] = {"title": "", "by": "", "since": -1e9}  # the clip playing (swapped whole)


# The player: a video file whose file changes clip by clip ---------------------------------------------


@register(KIND)
class ClipPlayerSource(MediaSource):
    label = "Random clip player"
    defaults = {**MediaSource.defaults, "path": "", "loop": False, "restart_on_show": False, "clear_on_end": True,
                "count": 20, "shortest": 5, "longest": 60, "gap": 5, "only_live": True}

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, {**settings, "path": "", "loop": False, "clear_on_end": True})
        self._clip_path = ""
        self._ended_at: float | None = None
        self._skip = False

    def update(self, settings: dict[str, Any]) -> None:
        # The garden keeps no file: the clip playing stays when its other settings change.
        super().update({**settings, "path": self._clip_path, "loop": False, "clear_on_end": True})

    def skip(self) -> None:
        self._skip = True

    def video_frame(self) -> np.ndarray | None:
        settings = self.settings
        live = output_since("stream") is not None or not settings.get("only_live", True)
        now = time.monotonic()
        playing = bool(self._clip_path) and self.recent_state() in ("playing", "paused") and not self._skip
        if not playing:
            if self._ended_at is None:
                self._ended_at = now if self._clip_path else -1e9
            if live:
                POOL.want(settings)
                gap = 0.0 if self._skip else _number(settings, "gap", 5, 0, 120)
                if now - self._ended_at >= gap:
                    ready = POOL.take()
                    if ready is not None:
                        self._skip, self._ended_at = False, None
                        self._clip_path = ready["path"]
                        globals()["NOW"] = {"title": ready["title"], "by": ready["by"], "since": now}
                        super().update({**self.settings, "path": self._clip_path})
        self.status = POOL.problem or ("ok" if live else "waits until you're live")
        return super().video_frame()


# The title card ------------------------------------------------------------------------------------------


def paint_fitted(big: str, small: str, settings: dict[str, Any], detail: float = 1.0):
    look = cards.look_of(settings)
    shown = cards.Words(big, small, badge="clip")
    return cards.paint(cards.card_width([shown], look), shown, detail=detail, look=look)


def title_words(now: dict[str, Any]) -> tuple[str, str]:
    return now["title"] or "A clip", f"Clipped by {now['by']}" if now["by"] else ""


@register(TITLE_KIND)
class ClipTitleSource(Source):
    label = "Clip title"
    defaults = {"show_for": 4.5, "text_color": "#f2f7f3", "card_color": "#151c19e0", "accent_color": "#a970ff",
                "font": "", "text_scale": 100, "corners": "leaf", "show_badge": True, "custom_look": ""}
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
        settings, now = self.settings, NOW
        if time.monotonic() - now["since"] >= _number(settings, "show_for", 4.5, 1, 60):
            key = ("hidden", self._w)
            if key == self._hidden_key:
                return None
            self._hidden_key = key
            self._card.drop()
            self._frame.release()
            return painted.clear_frame(self._w, H)
        self._hidden_key = None
        big, small = title_words(now)
        key = (big, small, now["since"], cards.look_of(settings).key, self.detail)
        frame = self._card.picture(key, paint_fitted, big, small, dict(settings), self.detail)
        if frame is not None:
            self._w = int(frame.shape[1])
        return frame


# Apps -----------------------------------------------------------------------------------------------------


def _players(api) -> list:
    if not any(s.kind == KIND for s in api.col.sources):
        raise ApiError("not_found", "Add Random clip player first (+ → Plugins → Random clip player).")
    engine = getattr(api.host, "engine", None)
    running = getattr(engine, "_sources", {}) if engine is not None else {}
    return [p for p in (running.get(s.id) for s in api.col.sources if s.kind == KIND) if isinstance(p, ClipPlayerSource)]


def _next(api, data):
    for player in _players(api):
        player.skip()
    return _get(api, {})


def _get(api, data):
    if not any(s.kind == KIND for s in api.col.sources):
        raise ApiError("not_found", "Add Random clip player first (+ → Plugins → Random clip player).")
    now = NOW
    return {"title": now["title"] or None, "by": now["by"] or None, "problem": POOL.problem or None}


register_action("clips.next", "Skip to the next clip.", {}, _next)
register_action("clips.get", "The clip playing: its title and who clipped it.", {}, _get)

