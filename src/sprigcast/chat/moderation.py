"""Chat back and moderate from the Chat panel: timeout, ban, delete a message, block.

Each platform's own official API, as the account you signed in with, so it only works where that account may do
it (your own chat, or one you moderate). Everything here makes web calls: run it on a worker.

* Twitch: output/twitch_auth.py (Helix). Everything.
* Kick: its public API, with the sign-in kept for Stream details. Sending chat, timeout and ban; Kick's API
  has no block, and deleting one message waits until its endpoint has been checked on a real channel.
* YouTube: the Data API with the YouTube sign-in (its `youtube` permission, already asked for): timeout, ban
  and delete, in a live chat that sign-in owns or moderates. Its chat is read without the API, so a line is
  found again in the API's list (same author and words) to delete it.

A sign-in made before these were asked for can't do them: NeedsAllow says so, and the panel offers the sign-in.
Nothing here writes names or messages to the log.
"""

from __future__ import annotations

import logging
import threading
import urllib.parse

log = logging.getLogger(__name__)

POWERS = {  # platform -> what SprigCast can do there on a chat line
    "twitch": ("timeout", "ban", "delete", "block"),
    "kick": ("timeout", "ban"),
    "youtube": ("timeout", "ban", "delete"),
}
CAN_SAY = ("twitch", "kick")
TIMEOUTS = ((60, "1 minute"), (600, "10 minutes"), (3600, "1 hour"), (86400, "1 day"))
KICK_SAY_SCOPE = "chat:write"
KICK_BAN_SCOPE = "moderation:ban"
KICK_TEXT_MAX = 500
KICK_LONGEST_MINUTES = 10080  # a week

_kick_ids: dict[str, int] = {}  # channel name -> broadcaster user id (it never changes)
_kick_lock = threading.Lock()


class ModerationError(Exception):
    """Worded for the person."""


class NeedsAllow(ModerationError):
    """The sign-in doesn't allow this yet: signing in again once does."""

    def __init__(self, platform: str, text: str):
        super().__init__(text)
        self.platform = platform


def length_words(seconds: int) -> str:
    return next((words for length, words in TIMEOUTS if length == seconds), f"{max(1, seconds // 60)} minutes")


# Kick --------------------------------------------------------------------------------------------------


def _kick(scope: str, call):
    """call(token) with the Kick sign-in, when it allows `scope`."""
    from sprigcast.output import stream_details as sd

    try:
        if not sd.allowed("kick", scope):
            raise NeedsAllow("kick", "Sign in to Kick again once to allow this (Settings → Stream).")
        return sd._with_token("kick", call)
    except NeedsAllow:
        raise
    except sd.NeedsSignIn as exc:
        if "said no" in str(exc):  # allowed by the sign-in, refused for this chat
            raise ModerationError("Kick said no. You may not be a moderator in this chat.") from exc
        raise NeedsAllow("kick", str(exc)) from exc
    except sd.DetailsError as exc:
        raise ModerationError(str(exc)) from exc


def _kick_broadcaster(token: str, channel: str) -> int:
    from sprigcast.output import stream_details as sd

    channel = channel.lower()
    with _kick_lock:
        known = _kick_ids.get(channel)
    if known:
        return known
    found = sd._call("GET", f"{sd.KICK_API}/channels?slug={urllib.parse.quote(channel)}", token, who="Kick")
    rows = found.get("data") or []
    try:
        user_id = int(rows[0]["broadcaster_user_id"])
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise sd.DetailsError(f"No Kick channel called {channel}.") from exc
    with _kick_lock:
        _kick_ids[channel] = user_id
    return user_id


def _kick_say(channel: str, text: str) -> None:
    from sprigcast.output import stream_details as sd

    def call(token: str) -> None:
        body = {"broadcaster_user_id": _kick_broadcaster(token, channel), "content": text[:KICK_TEXT_MAX],
                "type": "user"}
        reply = sd._call("POST", f"{sd.KICK_API}/chat", token, body, who="Kick")
        sent = reply.get("data") if isinstance(reply.get("data"), dict) else {}
        if sent.get("is_sent") is False:
            raise sd.DetailsError("Kick didn't post it.")

    _kick(KICK_SAY_SCOPE, call)


def _kick_ban(channel: str, user_id: str, seconds: int) -> None:
    from sprigcast.output import stream_details as sd

    try:
        who = int(user_id)
    except ValueError as exc:
        raise ModerationError("Kick didn't say who wrote that.") from exc

    def call(token: str) -> None:
        body: dict = {"broadcaster_user_id": _kick_broadcaster(token, channel), "user_id": who}
        if seconds:  # Kick counts a timeout in minutes; no duration is a ban
            body["duration"] = max(1, min(round(seconds / 60), KICK_LONGEST_MINUTES))
        sd._call("POST", f"{sd.KICK_API}/moderation/bans", token, body, who="Kick")

    _kick(KICK_BAN_SCOPE, call)


# YouTube -----------------------------------------------------------------------------------------------

_yt_chats: dict[str, str] = {}  # video id -> its live chat's id (it never changes)


def _youtube(call):
    """call(token) with the YouTube sign-in, worded errors."""
    from sprigcast.output import stream_details as sd

    try:
        return sd._with_token("youtube", call)
    except sd.DetailsError as exc:
        raise ModerationError(str(exc)) from exc


def _youtube_chat_id(token: str, video: str) -> str:
    from sprigcast.output import stream_details as sd

    if video not in _yt_chats:
        found = sd._yt(token, "GET", f"videos?part=liveStreamingDetails&id={urllib.parse.quote(video)}")
        items = found.get("items") or [{}]
        chat = str((items[0].get("liveStreamingDetails") or {}).get("activeLiveChatId") or "")
        if not chat:
            raise ModerationError("That YouTube stream has no live chat right now.")
        _yt_chats[video] = chat
    return _yt_chats[video]


def _youtube_ban(video: str, user_id: str, seconds: int) -> None:
    from sprigcast.output import stream_details as sd

    if not user_id:
        raise ModerationError("YouTube didn't say who wrote that.")

    def call(token: str) -> None:
        snippet: dict = {"liveChatId": _youtube_chat_id(token, video), "type": "temporary" if seconds else "permanent",
                         "bannedUserDetails": {"channelId": user_id}}
        if seconds:
            snippet["banDurationSeconds"] = int(seconds)
        sd._yt(token, "POST", "liveChat/bans?part=snippet", {"snippet": snippet})

    _youtube(call)


def youtube_message_id(items: list, message_id: str, user_id: str, text: str) -> str:
    """The API's id for a chat line read from the page: the same id if the API has it, else the newest line by
    the same author with the same words. "" when it isn't there (too old, or already gone)."""
    for item in items:
        if item.get("id") == message_id:
            return message_id
    for item in reversed(items):
        snippet, author = item.get("snippet") or {}, item.get("authorDetails") or {}
        words = snippet.get("displayMessage") or (snippet.get("textMessageDetails") or {}).get("messageText") or ""
        if author.get("channelId") == user_id and words.strip() == text.strip():
            return str(item.get("id") or "")
    return ""


def _youtube_delete(video: str, message_id: str, user_id: str, text: str) -> None:
    from sprigcast.output import stream_details as sd

    def call(token: str) -> None:
        chat = _youtube_chat_id(token, video)
        found = sd._yt(token, "GET", f"liveChat/messages?liveChatId={urllib.parse.quote(chat)}"
                                     "&part=snippet,authorDetails&maxResults=2000")
        wanted = youtube_message_id(found.get("items") or [], message_id, user_id, text)
        if not wanted:
            raise ModerationError("YouTube no longer has that message.")
        sd._yt(token, "DELETE", f"liveChat/messages?id={urllib.parse.quote(wanted)}")

    _youtube(call)


# Any platform ------------------------------------------------------------------------------------------


def say(platform: str, channel: str, text: str) -> None:
    """Say `text` in `channel`'s chat as the signed-in account. Raises ModerationError worded for the person."""
    if platform == "kick":
        _kick_say(channel, text)
        return
    from sprigcast.output import twitch_auth

    try:
        twitch_auth.send_chat(channel, text)
    except twitch_auth.TwitchAuthError as exc:
        raise ModerationError(str(exc)) from exc


def act(platform: str, channel: str, action: str, *, user_id: str = "", message_id: str = "",
        seconds: int = 0, text: str = "") -> None:
    """One of POWERS[platform] on a chat line's sender or the line itself. Raises ModerationError.
    YouTube: `channel` is the live video's id, and `text` the line's words (to find it for a delete)."""
    if action not in POWERS.get(platform, ()):
        raise ModerationError("SprigCast can't do that on this platform yet.")
    if platform == "youtube":
        if not channel:
            raise ModerationError("SprigCast hasn't found the YouTube stream's chat yet.")
        if action == "delete":
            _youtube_delete(channel, message_id, user_id, text)
        else:
            _youtube_ban(channel, user_id, seconds if action == "timeout" else 0)
        log.info("youtube chat: %s done", action)  # in the logbook, no names
        return
    if platform == "kick":
        _kick_ban(channel, user_id, seconds if action == "timeout" else 0)
        log.info("kick chat: %s done", action)  # in the logbook, no names
        return
    from sprigcast.output import twitch_auth

    try:
        twitch_auth.moderate(channel, action, user_id=user_id, message_id=message_id, seconds=seconds)
    except twitch_auth.NeedsReconnect as exc:
        raise NeedsAllow("twitch", str(exc)) from exc
    except twitch_auth.TwitchAuthError as exc:
        raise ModerationError(str(exc)) from exc


# Who wrote it ------------------------------------------------------------------------------------------


def account_made(platform: str, user_id: str) -> str:
    """When a chatter's account was made ("2023-04-01T…"), "" when the platform doesn't say or you aren't signed
    in there. Twitch: Helix users (no extra permission). YouTube: the channel's start (1 quota unit). Worker."""
    if not user_id:
        return ""
    try:
        if platform == "twitch":
            from sprigcast.output import twitch_auth

            found = twitch_auth.helix_get(lambda _me: f"/users?id={urllib.parse.quote(user_id)}")
            return str(((found.get("data") or [{}])[0]).get("created_at") or "")
        if platform == "youtube":
            from sprigcast.output import stream_details as sd

            found = sd._with_token("youtube", lambda token: sd._yt(
                token, "GET", f"channels?part=snippet&id={urllib.parse.quote(user_id)}"))
            return str((((found.get("items") or [{}])[0]).get("snippet") or {}).get("publishedAt") or "")
    except Exception as exc:  # noqa: BLE001  the card just leaves the line out
        log.info("chat: account age not read (%s)", type(exc).__name__)
    return ""


def age_words(made: str, now: float | None = None) -> str:
    """"3 years ago", "5 months ago", "12 days ago", "Today" from an ISO time; "" if it can't be read."""
    import datetime
    import time

    try:
        when = datetime.datetime.fromisoformat(made.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return ""
    days = int(((now if now is not None else time.time()) - when) // 86400)
    if days < 1:
        return "Today"
    for size, word in ((365, "year"), (30, "month"), (1, "day")):
        if days >= size:
            count = days // size
            return f"{count} {word}{'s' if count != 1 else ''} ago"
    return "Today"
