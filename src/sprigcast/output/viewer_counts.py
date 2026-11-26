"""How many watch on each platform SprigCast is live on, for the Control API's viewers.counts.

StreamBridge's Live count (user, 2026-11-26: "use sprigcast to connect twitch and kick"). Like youtube.viewers
(output/youtube_viewers.py, which this uses for YouTube), the answer always comes from what was last read: asking
never waits on the web. A platform SprigCast is live on is read again on a worker at most every REFRESH_S while
someone keeps asking; one it isn't live on is never read. Each uses the sign-in SprigCast already keeps for Stream
details, with no new permission:
- Twitch: Helix GET /streams?user_id= (any user token may read it).
- Kick: GET /channels (the signed-in channel; its stream's viewer_count), the channel:read it already has.
Nothing personal goes to the log.
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

REFRESH_S = 60.0
FAILED_AGAIN_S = 120.0
PLATFORMS = ("youtube", "twitch", "kick")


class _Count:
    """One platform's last read, read again on a worker when it's older than REFRESH_S."""

    def __init__(self, name: str, read: Callable[[], int | None]):
        self.name, self.read = name, read
        self.lock = threading.Lock()
        self.answer: dict[str, Any] = {"live": False}
        self.at = -1e9
        self.busy = False
        self.live = False

    def get(self, live: bool) -> dict[str, Any]:
        with self.lock:
            if not live:
                self.answer, self.at, self.live = {"live": False}, -1e9, False
                return dict(self.answer)
            if not self.live:
                self.live, self.at = True, -1e9
                self.answer = {"live": True, "viewers": None}
            if not self.busy and time.monotonic() - self.at >= REFRESH_S:
                self.busy = True
                threading.Thread(target=self._read, name=f"viewers-{self.name}", daemon=True).start()
            return dict(self.answer)

    def _read(self) -> None:
        try:
            count = self.read()
            answer: dict[str, Any] = {"live": True, "viewers": count}
            wait = REFRESH_S
        except Exception as exc:  # noqa: BLE001 (signed out, offline: the old answer kept, tried again later)
            log.info("viewer counts: couldn't read %s: %s", self.name, exc)
            signed_out = type(exc).__name__ == "NeedsSignIn"
            answer = {"live": True, "viewers": None, "signedIn": False} if signed_out else None
            wait = FAILED_AGAIN_S
        with self.lock:
            if self.live and answer is not None:
                self.answer = answer
            self.at = time.monotonic() + wait - REFRESH_S
            self.busy = False

    def forget(self) -> None:
        with self.lock:
            self.answer, self.at, self.live = {"live": False}, -1e9, False


def _twitch() -> int | None:
    from sprigcast.output import stream_details as sd
    from sprigcast.output import twitch_auth

    cid = twitch_auth.client_id()
    tokens = twitch_auth._load()
    if not tokens:
        raise sd.NeedsSignIn("not signed in to Twitch")

    def ask(t: dict) -> int | None:
        streams = sd._helix("GET", f"/streams?user_id={t['user_id']}", t["access_token"], cid).get("data") or []
        return int(streams[0].get("viewer_count") or 0) if streams else None  # None: Twitch doesn't see it live yet

    try:
        if time.time() >= float(tokens.get("expires_at", 0)):
            tokens = twitch_auth._refresh(tokens, cid)
        try:
            return ask(tokens)
        except PermissionError:
            return ask(twitch_auth._refresh(tokens, cid))
    except (twitch_auth.TwitchAuthError, PermissionError) as exc:
        raise sd.NeedsSignIn("sign in to Twitch again") from exc


def _kick() -> int | None:
    from sprigcast.output import stream_details as sd

    def call(token: str) -> int | None:
        channels = sd._call("GET", f"{sd.KICK_API}/channels", token, who="Kick").get("data") or []
        channel = channels[0] if channels and isinstance(channels[0], dict) else {}
        stream = channel.get("stream") if isinstance(channel.get("stream"), dict) else {}
        if not stream.get("is_live"):
            return None  # Kick doesn't see it live yet
        return int(stream.get("viewer_count") or 0)

    return sd._with_token("kick", call)


_counts = {"twitch": _Count("twitch", _twitch), "kick": _Count("kick", _kick)}


def counts(live_on: dict[str, list[tuple[str, str | None]]]) -> dict[str, Any]:
    """live_on: platform -> its places sending now ((canvas, stream key ref)); a platform not in it isn't live.
    {"platforms": {"youtube": {...}, "twitch": {...}, "kick": {...}}, "updatedAt": ISO time of this answer}.
    Each platform: {"live": false}, or {"live": true, "viewers": n or null (no number yet)}, with
    "signedIn": false when SprigCast has no sign-in for it."""
    from sprigcast.output import youtube_viewers

    yt = youtube_viewers.viewers(live_on.get("youtube") or [])
    platforms: dict[str, Any] = {"youtube": {k: yt[k] for k in ("live", "viewers", "broadcasts") if k in yt}}
    for name, count in _counts.items():
        platforms[name] = count.get(bool(live_on.get(name)))
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {"platforms": platforms, "updatedAt": now}


def forget(service: str) -> None:
    if service in _counts:
        _counts[service].forget()
