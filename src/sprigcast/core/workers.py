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
