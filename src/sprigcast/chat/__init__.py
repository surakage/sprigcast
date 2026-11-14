"""Chat from the places you stream to, as one list of plain messages.

Each platform has a reader that turns its own public chat into `ChatMessage`s: chat/twitch.py,
chat/youtube.py and chat/kick.py. Every reader needs only a channel name: no sign-in, no API key,
nothing tied to one person's account, so it works the same for anyone who installs SprigCast.
The Chat panel (app/chat_panel.py) only ever sees `ChatMessage`, and mixes every reader into one list.

A reader is a QObject with `platform`, `channel`, `start(name)`, `stop()` and the signals
`message(ChatMessage)`, `deleted(message id)`, `cleared(user id, "" = everyone)` and
`status(str)`: "connecting", "live", "waiting: <why>" (e.g. not live yet) or "offline: <why>".
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChatMessage:
    platform: str  # "twitch", later "youtube", "kick", ...
    user: str  # the name to show
    text: str
    color: str = ""  # the chatter's own name colour, "#rrggbb", if the platform has one
    id: str = ""  # the platform's message id, so a moderator's delete can remove it here too
    user_id: str = ""
    badges: tuple[str, ...] = ()  # "broadcaster", "moderator", "vip", "subscriber", ...
    emotes: list[tuple[int, int, str]] = field(default_factory=list)  # (start, end inclusive, emote id)
    images: dict[str, str] = field(default_factory=dict)  # emote id -> picture URL (platforms without a template)
    action: bool = False  # "/me waves"
    notice: bool = False  # subs, raids and other events the platform announces


PLATFORMS = {  # key -> (label, colour of its mark in the mixed list)
    "twitch": ("Twitch", "#a970ff"),
    "youtube": ("YouTube", "#ff5a52"),
    "kick": ("Kick", "#53fc18"),
}


def make_reader(platform: str, parent=None):
    """A reader for one platform (imports it only when it's used)."""
    if platform == "twitch":
        from sprigcast.chat.twitch import TwitchChat

        return TwitchChat(parent)
    if platform == "youtube":
        from sprigcast.chat.youtube import YouTubeChat

        return YouTubeChat(parent)
    if platform == "kick":
        from sprigcast.chat.kick import KickChat

        return KickChat(parent)
    raise KeyError(platform)


def name_for(platform: str, text: str) -> str | None:
    """What the person typed (a name, @handle or link) -> the name that platform's reader takes."""
    if platform == "twitch":
        from sprigcast.chat.twitch import channel_from_text

        return channel_from_text(text)
    if platform == "youtube":
        from sprigcast.chat.youtube import handle_from_text

        return handle_from_text(text)
    if platform == "kick":
        from sprigcast.chat.kick import slug_from_text

        return slug_from_text(text)
    return None
