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


def wait_answering(job: Callable, *args, poll_ms: int = 10):
    """job(*args) on a shared worker, the window still drawing and answering while it runs; then its result
    (or its error, raised here). For the few steps that must finish before the next one, like asking Twitch
    for the stream key while going live: a web request on the window's thread froze it 461 ms on the PC
    (2026-11-16). Clicks and keys wait until it's done, so nothing starts twice. Off the window's thread, or
    without Qt, it simply runs job here."""
    import threading

    try:
        from PySide6.QtCore import QCoreApplication, QEventLoop
    except ImportError:
        return job(*args)
    app = QCoreApplication.instance()
    if app is None or threading.current_thread() is not threading.main_thread():
        return job(*args)
    from concurrent.futures import wait

    future = pool().submit(job, *args)
    # Poll rather than a nested QEventLoop: on Windows that loop barely woke for the window's timers while it
    # waited (2 of ~20 beats in 200 ms on the PC, 2026-11-17). Each turn hands over every event that's due
    # (clicks and keys wait, so nothing starts twice), then sleeps on the job, not the GIL, for poll_ms.
    flags = QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
    while not future.done():
        app.processEvents(flags)
        wait([future], timeout=poll_ms / 1000)
    return future.result()
