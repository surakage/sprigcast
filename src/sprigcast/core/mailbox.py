"""Single-slot hand-off between threads: the reader only ever wants the newest item."""

from __future__ import annotations

import threading
from typing import Generic, TypeVar

T = TypeVar("T")


class Mailbox(Generic[T]):
    def __init__(self) -> None:
        self._item: T | None = None
        self._lock = threading.Lock()

    def put(self, item: T) -> None:
        """Store an item, replacing any item the reader has not taken yet."""
        with self._lock:
            self._item = item

    def take(self) -> T | None:
        """Return the newest item and empty the slot, or None if nothing new arrived."""
        with self._lock:
            item, self._item = self._item, None
        return item

    def peek(self) -> T | None:
        """Return the newest item without emptying the slot."""
        with self._lock:
            return self._item
