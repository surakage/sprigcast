"""SPRIGCAST_CRASH_TEST: crash SprigCast on purpose, for testing the crash guard on a real PC (never in normal use).

SPRIGCAST_CRASH_TEST=<seconds> reads address 0 on a thread of its own that many seconds after start;
SPRIGCAST_CRASH_TEST=<seconds>,main does it on the window's thread. Nothing catches it: no ctypes guard, no
native_bridge guard, so it reaches SprigCast.exe's crash guard (launcher.c) exactly as a real access violation
does. Expected: crash.log names it, a .dmp is written to the Logs folder, the run ends within 15 s, and the
next start says it closed unexpectedly. Unset (or not a number), nothing happens.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

ENV = "SPRIGCAST_CRASH_TEST"
THREAD_NAME = "crash-test"


def parse(value: str | None) -> tuple[float, bool] | None:
    """(seconds, on the window's thread) from the setting, or None."""
    if not value:
        return None
    when, _, where = value.partition(",")
    try:
        seconds = float(when)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds, where.strip().lower() == "main"


def crash() -> None:
    """An unguarded read of address 0 (Python's own test helper for exactly this)."""
    faulthandler._read_null()  # type: ignore[attr-defined]


def schedule(on_main: Callable[[int, Callable[[], None]], None], value: str | None = None,
             crash_now: Callable[[], None] = crash) -> str | None:
    """Arm the crash if SPRIGCAST_CRASH_TEST asks for it. on_main(ms, fn) runs fn on the window's thread later
    (QTimer.singleShot). Returns where it was armed ("thread" or "main"), or None."""
    asked = parse(os.environ.get(ENV) if value is None else value)
    if asked is None:
        return None
    seconds, main = asked
    log.warning("crash test: SprigCast will crash on purpose in %.0f s on the %s", seconds,
                "window's thread" if main else "crash-test thread")
    if main:
        on_main(int(seconds * 1000), crash_now)
        return "main"
    timer = threading.Timer(seconds, crash_now)
    timer.name = THREAD_NAME
    timer.daemon = True
    timer.start()
    return "thread"
