"""One set of chat readers for the whole app.

The Chat panel and chat sources on your stream both want the same chats, so the readers live here
instead of in either. The hub reads while anything needs it (`want(reason, True)`: the panel is on
screen, or a bloom has a chat source) and stops when nothing does. Every message goes out on
`message` (for the panel) and into the shared FEED (for chat sources on the render thread).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from sprigcast.chat import PLATFORMS, ChatMessage, hidden, make_reader, word_filter
from sprigcast.chat.feed import FEED
from sprigcast.core import privacy
from sprigcast.core.quiet import Every

log = logging.getLogger(__name__)

_CHAT_LOG = Every(1800)


class ChatHub(QObject):
    message = Signal(object)  # ChatMessage
    deleted = Signal(str, str)  # platform, message id
    cleared = Signal(str, str)  # platform, user id ("" = everyone)
    changed = Signal()  # which chats are read, or one's state, changed
    hid = Signal(str)  # an account was hidden: its lines leave (the name, without capitals)

    def __init__(self, names: Callable[[], dict[str, str]], parent: QObject | None = None):
        super().__init__(parent)
        self.names = names  # platform -> channel name to read, decided by the Chat panel's settings
        self.readers: dict[str, object] = {}
        self.states: dict[str, str] = {}
        self.reasons: set[str] = set()
        self.hidden: frozenset[str] = frozenset(hidden.load())  # accounts whose lines are never shown
        self.set_word_filter(*word_filter.load())

    def want(self, reason: str, on: bool) -> None:
        before = bool(self.reasons)
        (self.reasons.add if on else self.reasons.discard)(reason)
        if bool(self.reasons) != before:
            self.refresh()

    @property
    def reading(self) -> bool:
        return bool(self.reasons)

    def refresh(self) -> None:
        """Start, switch or stop readers to match the names (or stop them all when nothing wants chat)."""
        wanted = self.names() if self.reasons else {}
        for platform in list(self.readers):
            if platform not in wanted:
                reader = self.readers.pop(platform)
                reader.stop()
                log.info("chat %s: stopped", platform)  # in the logbook, no names
                self.states.pop(platform, None)
        for platform, name in wanted.items():
            if platform not in PLATFORMS:
                continue
            reader = self.readers.get(platform)
            if reader is None:
                reader = self.readers[platform] = make_reader(platform, self)
                reader.message.connect(self._message)
                reader.deleted.connect(lambda mid, p=platform: self._deleted(p, mid))
                reader.cleared.connect(lambda uid, p=platform: self._cleared(p, uid))
                reader.status.connect(lambda state, p=platform: self._state(p, state))
            if reader.channel != name or platform not in self.states:
                self.states[platform] = "connecting"
            privacy.remember_name(name)  # never in the log
            reader.start(name)
        self.changed.emit()

    def check_now(self) -> None:
        """You went live: readers still waiting for a stream to start look again now."""
        for reader in self.readers.values():
            check = getattr(reader, "check_now", None)
            if check is not None:
                check()

    def stop(self) -> None:
        self.reasons.clear()
        self.refresh()

    def set_hidden(self, names: list[str]) -> None:
        """Replace the hidden accounts (saved). Lines already shown from newly hidden ones leave."""
        new = [n for n in names if n not in self.hidden]
        hidden.save(names)
        self.hidden = frozenset(names)
        for name in new:
            FEED.remove(user=name)
            self.hid.emit(name)

    def hide(self, name: str) -> bool:
        name = hidden.clean(name) or ""
        if not name or name in self.hidden:
            return False
        self.set_hidden([*hidden.load(), name])
        log.info("chat: an account was hidden (%d hidden)", len(self.hidden))  # in the logbook, no names
        return True

    def set_word_filter(self, words: list[str], links: bool, save: bool = False) -> None:
        """Leave out lines with these words, or with links (from anyone but the owner, mods and VIPs)."""
        if save:
            word_filter.save(words, links)
            log.info("chat: word filter set (%d words, links %s)", len(words), "hidden" if links else "shown")
        self.blocked_words, self.hide_links = list(words), bool(links)
        self._words = word_filter.pattern(self.blocked_words)

    def _message(self, message: ChatMessage) -> None:
        if self.hidden and not message.notice and message.user.lower() in self.hidden:
            return
        if (self._words is not None or self.hide_links) and word_filter.blocks(message, self._words, self.hide_links):
            return
        if not message.at:
            message.at = time.time()
        FEED.push(message)
        self.message.emit(message)

    def _deleted(self, platform: str, message_id: str) -> None:
        FEED.remove(platform=platform, message_id=message_id)
        self.deleted.emit(platform, message_id)

    def _cleared(self, platform: str, user_id: str) -> None:
        FEED.remove(platform=platform, user_id=user_id, everything=not user_id)
        self.cleared.emit(platform, user_id)

    def _state(self, platform: str, state: str) -> None:
        if platform not in self.readers:
            return
        if self.states.get(platform) != state:
            # Waiting for a channel to go live cycles "connecting" / "not live yet" all day (519 lines
            # on 2026-11-15): those are written every 30 minutes; going live, offline or an error every time.
            routine = state == "connecting" or state.startswith("waiting")
            if not routine or _CHAT_LOG.allow((platform, state)):
                log.info("chat %s: %s%s", platform, state,
                         _CHAT_LOG.skipped((platform, state)) if routine else "")  # in the logbook, no names
        self.states[platform] = state
        self.changed.emit()
