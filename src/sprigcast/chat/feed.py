"""The latest chat lines, shared between the UI thread (where the readers run) and the render
thread (where chat sources draw them). Plain Python with a lock: no Qt, no copying of big things.

The chat hub (chat/hub.py) pushes every message here; a chat source asks for `snapshot()` only
when `version` has moved, so a quiet chat costs nothing.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from sprigcast.chat import ChatMessage

KEEP = 60  # more than any chat box shows


@dataclass(frozen=True)
class Line:
    message: ChatMessage
    at: float  # time.monotonic() when it arrived, for "hide after" fading


class Feed:
    def __init__(self, keep: int = KEEP):
        self._lock = threading.Lock()
        self._lines: deque[Line] = deque(maxlen=keep)
        self.version = 0  # moves on every change; reading an int needs no lock

    def push(self, message: ChatMessage) -> None:
        with self._lock:
            self._lines.append(Line(message, time.monotonic()))
            self.version += 1

    def remove(self, *, platform: str = "", message_id: str = "", user_id: str = "", everything: bool = False) -> None:
        with self._lock:
            kept = [
                line for line in self._lines
                if not ((not platform or line.message.platform == platform)
                        and (everything or (message_id and line.message.id == message_id)
                             or (user_id and line.message.user_id == user_id)))
            ]
            if len(kept) != len(self._lines):
                self._lines = deque(kept, maxlen=self._lines.maxlen)
                self.version += 1

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self.version += 1

    def snapshot(self) -> tuple[int, list[Line]]:
        with self._lock:
            return self.version, list(self._lines)


FEED = Feed()
