"""Closing the window doesn't wait for the render thread (the PC: 'the window didn't answer for 348-392 ms'
on close, engine.stop joining it)."""

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


def test_closing_hides_at_once_and_stops_the_engine_after(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core import pacing

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    w = MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")
    w.updater.install_on_quit = lambda: None
    w.show()
    engine = w.engine
    release = threading.Event()
    joined = engine._thread.join
    monkeypatch.setattr(engine._thread, "join", lambda timeout=None: (release.wait(5), joined(timeout)))
    pacing.ui_beat()
    began = time.perf_counter()
    w.close()
    assert time.perf_counter() - began < 2.5  # the render thread is held 5 s: didn't wait for the render thread
    assert not w.isVisible() and pacing._ui["beat"] is None
    assert engine._stopping is not None and engine._stopping.is_alive()
    release.set()
    engine.finish_stopping()
    assert not engine._stopping.is_alive() and not engine._thread.is_alive()



def test_the_log_says_why_sprigcast_closed():
    """StreamBridge 2026-11-20: closes for installs and restart tests looked like SprigCast ending by itself."""
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.main_window import close_reason

    app = QApplication.instance() or QApplication([])
    assert "window was closed" in close_reason(True, app)
    assert "closed itself" in close_reason(False, app)
    app.setProperty("sprigcast_session_end", True)
    try:
        assert "installer" in close_reason(True, app)
        app.setProperty("sprigcast_restart", True)
        assert "restarts" in close_reason(False, app)
    finally:
        app.setProperty("sprigcast_session_end", None)
        app.setProperty("sprigcast_restart", None)
