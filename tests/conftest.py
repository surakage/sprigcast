import os
import sys

import pytest

# Tests draw with Qt's "offscreen" platform. On Windows it finds no fonts of its own (Qt no longer ships any) and
# draws every letter as the same box, so "hi chat" and "HI CHAT" came out alike on the PC (2026-11-17). Point it at
# Windows' fonts, as the real app's Windows platform uses, before anything starts Qt.
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))


@pytest.fixture(autouse=True)
def _frames_faster_than_real_time(monkeypatch):
    """Tests hand an encoder a second of frames in a few milliseconds, often before it has opened.

    In the app that queue is kept short while the encoder opens (Recorder.opening_queue); here every
    frame has to arrive, so let the whole queue fill as it does once the encoder runs.
    """
    from sprigcast.output import recorder

    monkeypatch.setattr(recorder.Recorder, "opening_queue", recorder.VIDEO_QUEUE)


@pytest.fixture(autouse=True)
def _sources_closed_after_each_test():
    """Removed sources close on a helper thread (core/retire.py): let it finish and end after each test, so
    no test sees another's closing thread (some count threads)."""
    yield
    from sprigcast.core import retire

    retire.RETIRER.settle()


def pytest_sessionfinish(session, exitstatus):
    """Close what the tests left open and let Qt delete it while Python still runs. Left to the interpreter's own
    shutdown, Qt's wrappers were torn down after the modules they need, and the run ended in a crash (a segfault
    in Shiboken's unregisterWrapper) after every test had passed."""
    import gc

    try:
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        gc.collect()
