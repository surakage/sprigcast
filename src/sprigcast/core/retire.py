"""Closing sources off the render thread.

A source's stop() may wait: a camera, a sound device or a video file joins its own thread (up to 2-3 s when a
device hangs). Called on the render thread, as it was when a source was removed or its plugin went away, that
wait froze the picture and the recording. Removed sources go to one helper thread here instead, which closes
them in the order they came and ends by itself once it has nothing to do for IDLE_S.

When everything closes (the app quits), stop_all() closes them side by side with one shared time limit, so ten
sources take as long as the slowest, not the sum.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

log = logging.getLogger(__name__)

IDLE_S = 10.0  # the helper thread ends after this long with nothing to close (started again when needed)
_END = object()


def _stop(source) -> None:
    began = time.perf_counter()
    try:
        source.stop()
    except Exception:  # a plugin's source: log it, carry on with the rest
        log.exception("source %s (%s) didn't close cleanly", getattr(source, "id", "?"), getattr(source, "kind", "?"))
        return
    spent = time.perf_counter() - began
    if spent >= 0.25:
        log.info("source %s (%s) took %.0f ms to close", getattr(source, "id", "?"), getattr(source, "kind", "?"),
                 1000 * spent)


class Retirer:
    """One helper thread that stops sources handed to it, in order."""

    def __init__(self, idle_s: float = IDLE_S):
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._idle_s = idle_s

    def retire(self, source) -> None:
        """Stop `source` on the helper thread. Returns at once."""
        with self._lock:
            self._queue.put(source)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="sprigcast-retire", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                source = self._queue.get(timeout=self._idle_s)
                if source is _END:
                    self._queue.task_done()
                    return
            except queue.Empty:
                with self._lock:  # decided under the lock: retire() never hands work to a thread that's leaving
                    if self._queue.empty():
                        self._thread = None
                        return
                continue
            try:
                _stop(source)
            finally:
                self._queue.task_done()

    def wait(self, timeout: float) -> bool:
        """Wait until everything handed over so far is closed. False if `timeout` passed first."""
        deadline = time.monotonic() + timeout
        done = self._queue.all_tasks_done
        with done:
            while self._queue.unfinished_tasks:
                left = deadline - time.monotonic()
                if left <= 0 or not done.wait(left):
                    return not self._queue.unfinished_tasks
        return True

    def settle(self, timeout: float = 5.0) -> bool:
        """Close everything handed over, then let the helper thread end now (tests count threads)."""
        done = self.wait(timeout)
        with self._lock:
            thread, self._thread = self._thread, None
            if thread is not None and thread.is_alive():
                self._queue.put(_END)
        if thread is not None:
            thread.join(timeout)
        return done

    @property
    def busy(self) -> bool:
        return bool(self._queue.unfinished_tasks)


RETIRER = Retirer()


def stop_all(sources, timeout: float = 4.0) -> list[str]:
    """Stop every source at once, each on a thread of its own, and wait at most `timeout` for all of them.
    Returns the ids of the ones still closing when the time ran out (they finish on their own)."""
    threads = []
    for source in sources:
        thread = threading.Thread(target=_stop, args=(source,), name=f"close-{getattr(source, 'id', '?')}",
                                  daemon=True)
        thread.start()
        threads.append((source, thread))
    deadline = time.monotonic() + timeout
    for _source, thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    late = [str(getattr(s, "id", "?")) for s, t in threads if t.is_alive()]
    if late:
        log.warning("sources still closing after %.0f s: %s", timeout, ", ".join(late))
    return late
