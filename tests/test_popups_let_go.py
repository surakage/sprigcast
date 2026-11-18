"""Opening and closing pop-ups, menus and Settings many times leaves nothing behind (OBS deletes its properties
and settings windows on close the same way). Each kept menu held a window handle for the rest of the session."""

import os

import pytest

pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from test_add_menu import _window  # noqa: E402


def _closing_whatever_pops_up():
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    def close():
        popup = QApplication.activePopupWidget()
        if popup is not None:
            popup.close()
            return
        modal = QApplication.activeModalWidget()
        if modal is not None:
            modal.reject() if hasattr(modal, "reject") else modal.close()
            return
        QTimer.singleShot(20, close)  # not up yet

    QTimer.singleShot(20, close)


def _count(w):
    import gc

    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication, QWidget

    for _ in range(3):
        QApplication.sendPostedEvents(None, 0)
        # deleteLater: what's waiting to be deleted is let go, not kept. Plain processEvents only deletes those
        # when the event loop level allows it, which made this fail now and then in a full run.
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        gc.collect()
    from collections import Counter

    # Widgets only: a label makes its rich-text helpers (plain QObjects) the first time it needs them, whenever
    # that is; a kept pop-up or menu is widgets.
    return Counter(type(o).__name__ for o in w.findChildren(QWidget)), len(QApplication.topLevelWidgets())


def test_menus_and_pop_ups_let_go_when_closed(tmp_path, monkeypatch):
    from PySide6.QtCore import QPoint

    from sprigcast import plugin_store

    # Left by an earlier test, these make the window say "plugin updated" 2.5 s in, in the middle of the count.
    monkeypatch.setattr(plugin_store, "updated_at_start", [])
    monkeypatch.setattr(plugin_store, "waiting", {})
    col, w = _window(tmp_path, monkeypatch)
    try:
        opens = [
            lambda: w._library_plus("scenes"),
            lambda: w._sound_strip_menu(None, QPoint(10, 10)),
            lambda: w._open_settings(),
            lambda: w._open_filters("bg", "video"),
        ]
        # The window's one note is made the first time something says something ("Your PC is busy" when the
        # test machine is loaded) and kept for the next: made here so that moment can't fall in the count.
        w._saved_note("Warm-up", ms=1)
        for _ in range(2):  # twice each first: anything made once and kept (caches, a shared menu) settles
            for open_ in opens:
                _closing_whatever_pops_up()
                open_()
        before = _count(w)
        for _ in range(3):
            for open_ in opens:
                _closing_whatever_pops_up()
                open_()
        after = _count(w)
        kept = after[0] - before[0]
        assert not kept and after[1] <= before[1], f"kept {dict(kept)}, {after[1] - before[1]} windows"
    finally:
        w.close()


def test_the_settings_warm_up_leaves_nothing_behind(tmp_path, monkeypatch):
    """Once after start, Settings' Stream page is made off screen and let go (Qt's style rules and fonts ready:
    the first real open takes about half as long). Nothing of it stays, and it never runs while live."""
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.settings_window import SettingsWindow

    import time

    col, w = _window(tmp_path, monkeypatch)
    app = QApplication.instance()

    def settle():
        end = time.monotonic() + 2
        while time.monotonic() < end:
            app.processEvents()
            if not any(isinstance(x, SettingsWindow) and x.isVisible() for x in QApplication.topLevelWidgets()):
                app.processEvents()  # its deleteLater
                break

    monkeypatch.setattr(type(w), "WARM_SETTINGS_MS", 300)
    try:
        w.show()
        w.warm_settings()  # made in parts, like a real open, then let go
        settle()
        before = _count(w)
        w.warm_settings()
        settle()
        assert _count(w) == before
        assert not any(isinstance(x, SettingsWindow) and x.isVisible() for x in QApplication.topLevelWidgets())
        w.streaming = object()
        made = []
        monkeypatch.setattr("sprigcast.app.main_window.SettingsWindow", lambda *a, **k: made.append(1))
        w.warm_settings()
        assert not made
    finally:
        w.streaming = None
        w.close()
