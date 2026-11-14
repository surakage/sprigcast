"""Chat from the places you stream to, as one list of plain messages.

Each platform has a reader that turns its own feed into `ChatMessage`s (chat/twitch.py today).
The Chat panel (app/chat_panel.py) only ever sees `ChatMessage`, so more platforms can be mixed
into the same list later without touching the panel.
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
    action: bool = False  # "/me waves"
    notice: bool = False  # subs, raids and other events the platform announces
