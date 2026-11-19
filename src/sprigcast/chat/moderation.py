"""Chat back and moderate from the Chat panel: timeout, ban, delete a message, block.

Each platform's own official API, as the account you signed in with, so it only works where that account may do
it (your own chat, or one you moderate). Everything here makes web calls: run it on a worker.

* Twitch: output/twitch_auth.py (Helix). Everything.
* Kick: its public API, with the sign-in kept for Stream details. Sending chat, timeout and ban; Kick's API
  has no block, and deleting one message waits until its endpoint has been checked on a real channel.
* YouTube: not yet (it needs a wider Google permission, asked for only after Google's review is through).

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
        seconds: int = 0) -> None:
    """One of POWERS[platform] on a chat line's sender or the line itself. Raises ModerationError."""
    if action not in POWERS.get(platform, ()):
        raise ModerationError("SprigCast can't do that on this platform yet.")
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
