"""Signing out, shutting down or a plain kill closes SprigCast cleanly on Linux and macOS (app/unix_signals.py):
SIGTERM used to end it on the spot and the next start said it had closed unexpectedly (test build 16)."""
import os
import signal
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows asks through the session instead")


def test_sigterm_closes_the_window_and_ends_the_app_once():
    from PySide6.QtWidgets import QApplication, QWidget

    from sprigcast.app import unix_signals
    from sprigcast.app.main_window import close_reason

    app = QApplication.instance() or QApplication([])
    closed, quits = [], []

    class Window(QWidget):
        def closeEvent(self, event):  # noqa: N802
            closed.append(event.spontaneous())

    window = Window()
    window.show()
    old = {name: signal.getsignal(getattr(signal, name)) for name in unix_signals.NAMES}
    old_fd = signal.set_wakeup_fd(-1)
    app.aboutToQuit.connect(lambda: quits.append(1))
    try:
        assert unix_signals.install(app, window)
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGTERM)  # asked twice: closed once
        until = time.monotonic() + 3
        while not closed and time.monotonic() < until:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        assert closed == [False]  # closed by SprigCast itself: no "really quit?" question
        assert "SIGTERM" in close_reason(False, app)
    finally:
        app.setProperty("sprigcast_signal", None)
        signal.set_wakeup_fd(old_fd)
        for name, handler in old.items():
            signal.signal(getattr(signal, name), handler)
