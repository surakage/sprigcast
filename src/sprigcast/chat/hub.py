"""One set of chat readers for the whole app.

The Chat panel and chat sources on your stream both want the same chats, so the readers live here
instead of in either. The hub reads while anything needs it (`want(reason, True)`: the panel is on
screen, or a bloom has a chat source) and stops when nothing does. Every message goes out on
`message` (for the panel) and into the shared FEED (for chat sources on the render thread).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from sprigcast.chat import PLATFORMS, ChatMessage, make_reader
from sprigcast.chat.feed import FEED

log = logging.getLogger(__name__)


class ChatHub(QObject):
    message = Signal(object)  # ChatMessage
    deleted = Signal(str, str)  # platform, message id
    cleared = Signal(str, str)  # platform, user id ("" = everyone)
    changed = Signal()  # which chats are read, or one's state, changed

    def __init__(self, names: Callable[[], dict[str, str]], parent: QObject | None = None):
        super().__init__(parent)
        self.names = names  # platform -> channel name to read, decided by the Chat panel's settings
        self.readers: dict[str, object] = {}
        self.states: dict[str, str] = {}
        self.reasons: set[str] = set()

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
                self.readers.pop(platform).stop()
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

    def _message(self, message: ChatMessage) -> None:
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
            log.info("chat %s %s: %s", platform, self.readers[platform].channel, state)  # in the logbook
        self.states[platform] = state
        self.changed.emit()
