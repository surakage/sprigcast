"""Keep the log readable: things that happen over and over are written now and then, not every time.

The user's log for one day (2026-11-15) had 3,142 lines, most of them the same few: a screenshot every
minute, every web request a chat made, a chat going "connecting" / "not live yet" all day,
and Streamer.bot connecting and disconnecting. Failures are still written every time.
"""

from __future__ import annotations

import time


class Every:
    """allow(key) is True at most once every `seconds` per key; skipped() says how many were held back."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self.seconds = seconds
        self._clock = clock
        self._at: dict[object, float] = {}
        self._held: dict[object, int] = {}

    def allow(self, key: object = None) -> bool:
        now = self._clock()
        if now - self._at.get(key, float("-inf")) >= self.seconds:
            self._at[key] = now
            return True
        self._held[key] = self._held.get(key, 0) + 1
        return False

    def skipped(self, key: object = None) -> str:
        """" (and N more like it)" for the line being written now, then starts counting again."""
        n = self._held.pop(key, 0)
        return f" (and {n} more like it since)" if n else ""


def quiet_libraries() -> None:
    """Libraries that log every web request at INFO (httpx: hundreds of lines a day from one chat)."""
    import logging

    for name in ("httpx", "httpcore", "urllib3", "websockets", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
