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

    from PySide6.QtWidgets import QApplication, QWidget

    for _ in range(3):
        QApplication.sendPostedEvents(None, 0)  # deleteLater happens here
        QApplication.processEvents()
        gc.collect()
    from collections import Counter

    # Widgets only: a label makes its rich-text helpers (plain QObjects) the first time it needs them, whenever
    # that is; a kept pop-up or menu is widgets.
    return Counter(type(o).__name__ for o in w.findChildren(QWidget)), len(QApplication.topLevelWidgets())


def test_menus_and_pop_ups_let_go_when_closed(tmp_path, monkeypatch):
    from PySide6.QtCore import QPoint

    col, w = _window(tmp_path, monkeypatch)
    try:
        opens = [
            lambda: w._library_plus("scenes"),
            lambda: w._sound_strip_menu(None, QPoint(10, 10)),
            lambda: w._open_settings(),
            lambda: w._open_filters("bg", "video"),
        ]
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
