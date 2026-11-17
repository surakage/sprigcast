"""Web requests and slow reads stay off the window's thread (the PC, 2026-11-16: a secure web read froze the
window 461 ms while going live, the Wave Link check 846 ms)."""

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_waiting_for_a_web_request_keeps_the_window_answering(qapp):
    from PySide6.QtCore import QTimer

    from sprigcast.core.workers import wait_answering

    beats, where = [], []
    beat = QTimer()
    beat.timeout.connect(lambda: beats.append(time.perf_counter()))
    beat.start(10)

    def slow(x):
        where.append(threading.current_thread() is threading.main_thread())
        time.sleep(0.2)
        return x * 2

    assert wait_answering(slow, 21) == 42
    beat.stop()
    assert where == [False] and len(beats) >= 5  # ran on a worker while the window's timers kept firing

    def fails():
        raise ValueError("offline")

    with pytest.raises(ValueError, match="offline"):
        wait_answering(fails)


def test_off_the_window_thread_it_just_runs(qapp):
    from sprigcast.core.workers import wait_answering

    got = []
    worker = threading.Thread(target=lambda: got.append(wait_answering(threading.current_thread)))
    worker.start()
    worker.join()
    assert got == [worker]
