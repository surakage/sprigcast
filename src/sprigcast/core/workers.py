"""One small pool of worker threads for short jobs the UI shouldn't wait on.

Screenshots, clip saves, file tidying and the like each used to start a thread of their own. On a long
show that kept the thread count swinging (140-260 a minute apart on 2026-11-15), and every thread costs
a stack and handles. Jobs here share up to MAX_WORKERS threads, started on first use and kept.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

log = logging.getLogger(__name__)

MAX_WORKERS = 4
_pool: ThreadPoolExecutor | None = None
_pictures: ThreadPoolExecutor | None = None


def pool() -> ThreadPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="sprigcast-work")
    return _pool


def run(job: Callable, *args, name: str = "") -> Future:
    """Run job(*args) on a shared worker. Errors are logged (and kept on the returned Future)."""
    def logged():
        try:
            return job(*args)
        except Exception:
            log.exception("background job %s failed", name or getattr(job, "__name__", "?"))
            raise
    return pool().submit(logged)


def picture(job: Callable, *args, name: str = "") -> Future:
    """Run job(*args) on the one pictures lane: drawing a card or decoding a picture for the render thread.

    These jobs hold Python's lock for much of their time (painting text, converting pixels). On the shared
    pool, a bloom coming back sent its texts, clocks, pictures and Now playing to four workers at once, and the
    render thread waited its turn behind all of them: its switch frame grew to 300+ ms on the PC (2026-11-18,
    "telling sources shown or hidden" 71 ms for a few cheap calls). One at a time, it waits behind one at most.
    """
    global _pictures
    if _pictures is None:
        _pictures = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sprigcast-pictures")

    def logged():
        try:
            return job(*args)
        except Exception:
            log.exception("picture job %s failed", name or getattr(job, "__name__", "?"))
            raise
    return _pictures.submit(logged)


def wait_answering(job: Callable, *args, poll_ms: int = 10):
    """job(*args) on a shared worker, the window still drawing and answering while it runs; then its result
    (or its error, raised here). For the few steps that must finish before the next one, like asking Twitch
    for the stream key while going live: a web request on the window's thread froze it 461 ms on the PC
    (2026-11-16). Clicks and keys wait until it's done, so nothing starts twice. Off the window's thread, or
    without Qt, it simply runs job here."""
    import threading

    try:
        from PySide6.QtCore import QCoreApplication
    except ImportError:
        return job(*args)
    app = QCoreApplication.instance()
    if app is None or threading.current_thread() is not threading.main_thread():
        return job(*args)
    global _answering
    future = pool().submit(job, *args)
    _answering += 1
    try:
        _wait(app, future, poll_ms)
    finally:
        _answering -= 1
        if not _answering and _after:
            later, _after[:] = list(_after), []
            from PySide6.QtCore import QTimer

            for step in later:
                QTimer.singleShot(0, step)
    return future.result()


_answering = 0  # wait_answering()s in progress on the window's thread
_after: list[Callable] = []  # steps held back until they're done (when_free)


def answering() -> bool:
    """True while the window's thread is inside wait_answering()."""
    return _answering > 0


def when_free(step: Callable) -> bool:
    """Inside wait_answering(): hold `step` until the wait is over and return True. Otherwise False (do it now).

    A pop-up opened in the middle of a wait (Settings from the Sound card's gear, while StreamBridge's bloom
    screenshot was being made, PC 2026-11-20) held the waiting job's answer for as long as
    it stayed open, and ran its own event loop under the wait's."""
    if not answering():
        return False
    _after.append(step)
    return True


def _wait(app, future, poll_ms: int) -> None:
    from concurrent.futures import wait
    from PySide6.QtCore import QEventLoop

    # Poll rather than a nested QEventLoop: on Windows that loop barely woke for the window's timers while it
    # waited (2 of ~20 beats in 200 ms on the PC, 2026-11-17). Each turn hands over every event that's due
    # (clicks and keys wait, so nothing starts twice), then sleeps on the job, not the GIL, for poll_ms.
    flags = QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
    while not future.done():
        app.processEvents(flags)
        wait([future], timeout=poll_ms / 1000)
