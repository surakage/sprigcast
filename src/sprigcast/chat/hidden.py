"""Accounts you don't want to see in chat: bots, and anyone you hide.

A hidden account's lines never reach the Chat panel or a chat source on your stream. This is only about
what SprigCast shows: it doesn't block, time out or ban anyone on the platform itself. Names are matched
without capitals and on every platform (a bot is usually called the same everywhere).

Kept in QSettings "chat/hidden" as a JSON list. Plain helpers, no Qt objects.
"""

from __future__ import annotations

import json
import re

SETTING = "chat/hidden"
# The usual chat bots, offered with one click (nothing is hidden until you choose it).
COMMON_BOTS = ("nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot", "botrix",
               "kickbot", "soundalerts", "sery_bot", "pokemoncommunitygame", "streamer.bot")
_NAME = re.compile(r"^[\w.\-·]{1,50}$")


def clean(name: str) -> str | None:
    """What someone typed (or a chatter's shown name) -> the name as it's kept, or None if it isn't one."""
    name = name.strip().lstrip("@").lower()
    return name if _NAME.match(name) else None


def parse(text: str) -> list[str]:
    """Names from a box of text: one a line, or separated by commas or spaces. Order kept, no repeats."""
    out: list[str] = []
    for part in re.split(r"[\s,;]+", text):
        name = clean(part) if part else None
        if name and name not in out:
            out.append(name)
    return out


def load(settings=None) -> list[str]:
    if settings is None:
        from PySide6.QtCore import QSettings

        settings = QSettings()
    try:
        names = json.loads(str(settings.value(SETTING, "") or "[]"))
    except ValueError:
        return []
    return [n for n in (clean(str(n)) for n in names if isinstance(n, str)) if n] if isinstance(names, list) else []


def save(names: list[str], settings=None) -> None:
    if settings is None:
        from PySide6.QtCore import QSettings

        settings = QSettings()
    settings.setValue(SETTING, json.dumps(list(dict.fromkeys(names))))
