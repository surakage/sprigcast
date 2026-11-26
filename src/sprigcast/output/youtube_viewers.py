"""How many are watching on YouTube while SprigCast is live there, for the Control API's youtube.viewers.

StreamBridge's Live count asked for it (2026-11-26): Streamer.bot sent no YouTube count all evening and follows
only one of the two YouTube streams. The answer always comes from what was last read: asking never waits on the
web. While someone keeps asking and SprigCast is live on YouTube, it is read again on a worker at most every
REFRESH_S: the channel's active streams (liveBroadcasts, 1 unit of YouTube's daily quota) and their viewers
(videos part=liveStreamingDetails, 1 unit). Nobody asking: nothing is read. Only the youtube permission the
sign-in already has is used. Nothing personal goes to the log (no titles, no ids).
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

REFRESH_S = 60.0
FAILED_AGAIN_S = 120.0  # after a failed read, the next try waits this long (the quota stays safe)

_lock = threading.Lock()
_answer: dict[str, Any] = {"live": False}
_read_at = -1e9  # monotonic time of the last read (or failed read: then pushed later)
_reading = False
_watched: tuple = ()  # the YouTube places last asked about: a change reads again at once
_stream_ids: dict[str, str] = {}  # stream key ref -> YouTube's stream id (a key's stream never changes)


def viewers(places: list[tuple[str, str | None]]) -> dict[str, Any]:
    """places: (canvas, stream key ref) of each YouTube place sending now. Returns the last read:
    {"live": True, "viewers": total, "broadcasts": [{"canvas", "viewers"}], "updatedAt": ISO time} while live
    on YouTube ("viewers" is None until the first read is in), else {"live": False}."""
    global _answer, _read_at, _reading, _watched
    places = sorted(places, key=lambda p: p[0])
    with _lock:
        if not places:
            _answer, _read_at, _watched = {"live": False}, -1e9, ()
            return dict(_answer)
        if tuple(places) != _watched:  # live now, or a place joined or left: read at once
            _watched, _read_at = tuple(places), -1e9
            _answer = {"live": True, "viewers": None, "broadcasts": [], "updatedAt": None}
        if not _reading and time.monotonic() - _read_at >= REFRESH_S:
            _reading = True
            threading.Thread(target=_read, args=(places,), name="youtube-viewers", daemon=True).start()
        return dict(_answer)


def _read(places: list[tuple[str, str | None]]) -> None:
    global _answer, _read_at, _reading
    try:
        answer = _fetch(places)
    except Exception as exc:  # noqa: BLE001 (signed out, offline, quota: tried again later, the old answer kept)
        log.info("youtube viewers: couldn't read them: %s", exc)
        with _lock:
            _read_at = time.monotonic() + FAILED_AGAIN_S - REFRESH_S
            _reading = False
            if _watched == tuple(places) and type(exc).__name__ == "NeedsSignIn":
                _answer = {"live": False}  # not signed in to YouTube: nothing to say
        return
    with _lock:
        if _watched == tuple(places):  # still live on the same places
            _answer = answer
        _read_at = time.monotonic()
        _reading = False


def _fetch(places: list[tuple[str, str | None]]) -> dict[str, Any]:
    from sprigcast.output import stream_details as sd
    from sprigcast.output.keys import get_key

    def call(token: str) -> dict[str, Any]:
        unknown = [ref for _canvas, ref in places if ref and ref not in _stream_ids]
        if unknown:
            keys = [get_key(ref) for ref in unknown]
            for ref, stream in zip(unknown, sd._youtube_streams_for_keys(token, *keys)):
                if stream and stream.get("id"):
                    _stream_ids[ref] = stream["id"]
        active = sd._yt(token, "GET", "liveBroadcasts?part=id,contentDetails&broadcastStatus=active"
                                      "&broadcastType=all&maxResults=25").get("items") or []
        by_stream = {(b.get("contentDetails") or {}).get("boundStreamId"): b.get("id") for b in active if b.get("id")}
        # Each place's broadcast. YouTube's Dual stream puts the vertical key on the horizontal one's broadcast:
        # its stream is bound to none, so it counts once, with the horizontal one.
        wanted = [(canvas, by_stream.get(_stream_ids.get(ref or ""))) for canvas, ref in places]
        if not any(b for _c, b in wanted) and len(active) == 1:
            wanted = [(places[0][0], active[0]["id"])]  # one live stream and keys YouTube didn't list: that one
        ids = list(dict.fromkeys(b for _c, b in wanted if b))
        counts: dict[str, int] = {}
        if ids:
            videos = sd._yt(token, "GET", "videos?part=liveStreamingDetails&id=" + ",".join(ids)).get("items") or []
            for video in videos:
                raw = (video.get("liveStreamingDetails") or {}).get("concurrentViewers")
                if video.get("id") and raw is not None:
                    try:
                        counts[video["id"]] = int(raw)
                    except (TypeError, ValueError):
                        pass
        seen: set[str] = set()
        broadcasts = []
        for canvas, broadcast in wanted:
            if broadcast and broadcast not in seen:
                seen.add(broadcast)
                broadcasts.append({"canvas": canvas, "viewers": counts.get(broadcast, 0)})
        now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return {"live": True, "viewers": sum(b["viewers"] for b in broadcasts), "broadcasts": broadcasts,
                "updatedAt": now}

    return sd._with_token("youtube", call)


def forget() -> None:
    """Signed out or in again: start over."""
    global _answer, _read_at, _watched
    with _lock:
        _answer, _read_at, _watched = {"live": False}, -1e9, ()
        _stream_ids.clear()
