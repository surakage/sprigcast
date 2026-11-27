"""Words and links you don't want in chat: lines with them are left out of the Chat panel and the chat on
your stream. Lines from the channel's owner, its moderators and VIPs always show.

Like hidden accounts, this is only what SprigCast shows: nothing is deleted on the platform.
Kept in QSettings "chat/blocked_words" (a JSON list) and "chat/hide_links". Plain helpers, no Qt objects.
A word ending in * matches anything that starts with it ("spam*" catches "spammer").
"""

from __future__ import annotations

import json
import re

WORDS = "chat/blocked_words"
LINKS = "chat/hide_links"
TRUSTED = ("broadcaster", "moderator", "vip")
_LINK = re.compile(r"(https?://|www\.)\S+|\b[\w-]+\.(com|net|org|gg|tv|io|ly|me|co|xyz|ru|shop)\b", re.IGNORECASE)


def parse(text: str) -> list[str]:
    """Words from a box of text: one a line or separated by commas. Lowercase, order kept, no repeats."""
    out: list[str] = []
    for part in re.split(r"[\n,;]+", text):
        word = part.strip().lower()
        if word and word not in out:
            out.append(word[:60])
    return out


def pattern(words: list[str]) -> re.Pattern | None:
    """One pattern for all the words (whole words; a trailing * matches the rest of a word)."""
    parts = []
    for word in words:
        stem = word.rstrip("*")
        if not stem:
            continue
        body = re.escape(stem) + (r"\w*" if word.endswith("*") else "")
        parts.append(rf"(?<!\w){body}(?!\w)")
    return re.compile("|".join(parts), re.IGNORECASE) if parts else None


def has_link(text: str) -> bool:
    return bool(_LINK.search(text))


def blocks(message, words: re.Pattern | None, links: bool) -> bool:
    """Should this line be left out?"""
    if message.notice or any(b in TRUSTED for b in message.badges):
        return False
    return bool((words is not None and words.search(message.text)) or (links and has_link(message.text)))


def load(settings=None) -> tuple[list[str], bool]:
    if settings is None:
        from PySide6.QtCore import QSettings

        settings = QSettings()
    try:
        words = json.loads(str(settings.value(WORDS, "") or "[]"))
    except ValueError:
        words = []
    words = [str(w).lower() for w in words if isinstance(w, str) and w.strip()] if isinstance(words, list) else []
    return words, str(settings.value(LINKS, "false")).lower() in ("true", "1")


def save(words: list[str], links: bool, settings=None) -> None:
    if settings is None:
        from PySide6.QtCore import QSettings

        settings = QSettings()
    settings.setValue(WORDS, json.dumps(list(dict.fromkeys(words))))
    settings.setValue(LINKS, bool(links))
