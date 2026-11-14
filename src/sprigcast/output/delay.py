"""Stream delay: hold a stream's packets for N seconds before they go to the network.

Viewers see everything N seconds late, so stream snipers can't use the stream to find you. The
packets wait in memory (30 s at 6000 kbps is about 23 MB). Recordings and clips are never delayed.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

MAX_DELAY = 600  # seconds; Settings offers up to this


class DelayLine:
    """Hands each item to `release` `seconds` after it was put in, in order, on its own thread.

    `close()` drops whatever is still waiting and stops releasing at once, so a reconnecting stream
    never gets packets meant for its old connection. `finish()` is for ending a stream: it takes nothing
    new but keeps releasing until the delayed part has gone out, like OBS.
    """

    def __init__(self, seconds: float, release: Callable, name: str = "delay"):
        self.seconds = max(0.0, float(seconds))
        self._release = release
        self._items: deque = deque()
        self._lock = threading.Condition()
        self._closed = False
        self._finishing = False
        self._thread = threading.Thread(target=self._run, name=f"sprigcast-{name}", daemon=True)
        self._thread.start()

    def put(self, item) -> None:
        with self._lock:
            if self._closed or self._finishing:
                return
            self._items.append((time.monotonic() + self.seconds, item))
            self._lock.notify()

    def waiting(self) -> int:
        with self._lock:
            return len(self._items)

    def finish(self, timeout: float | None = None) -> bool:
        """Take nothing new and wait until everything waiting has been released on time. True when it
        all went out; on timeout the rest is dropped (close())."""
        with self._lock:
            self._finishing = True
            self._lock.notify()
            end = None if timeout is None else time.monotonic() + timeout
            while self._items and not self._closed:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    break
                self._lock.wait(left)
            drained = not self._items
        self.close()
        return drained

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._items.clear()
            self._lock.notify()

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._closed and (not self._items or self._items[0][0] > time.monotonic()):
                    wait = None if not self._items else self._items[0][0] - time.monotonic()
                    self._lock.wait(wait)
                if self._closed:
                    return
                _, item = self._items.popleft()
                # released while holding the lock, so close() can't race a release into a new connection
                self._release(item)
                if not self._items:
                    self._lock.notify_all()  # finish() is waiting for the last one
