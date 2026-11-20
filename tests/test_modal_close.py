"""Closing the main window while a pop-up is open (PC, 2026-11-20: the × was ignored for 92 s)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.asked = 0

    def request_close(self):
        self.asked += 1
        self.close()


def test_the_pop_up_is_cancelled_then_the_window_closes(app):
    from sprigcast.app.modal import close_main_after

    window = Window()
    window.show()
    dialog = QDialog(window)
    answers = []
    QTimer.singleShot(0, lambda: close_main_after(dialog, window))
    answers.append(dialog.exec())
    for _ in range(5):
        app.processEvents()
    assert answers == [QDialog.DialogCode.Rejected] and window.asked == 1 and not window.isVisible()


def test_the_watch_is_off_away_from_windows(app):
    from sprigcast.app.modal import _CloseWatch

    assert _CloseWatch.start(QDialog()) is None  # nothing runs per message here (only Windows drops the ×)


def test_closing_waits_for_nested_pop_ups_opened_through_modal(app):
    """PC test, 2026-11-20: with Filters open the first close was lost. Every pop-up is cancelled, inner first,
    and the window is closed only after the outer one's exec() has returned."""
    from sprigcast.app import modal as m

    window = Window()
    window.show()
    outer = QDialog(window)
    seen = []

    def open_inner():
        inner = QDialog(outer)
        QTimer.singleShot(0, lambda: m.close_main_after(outer, window))
        seen.append(("inner", m.modal(inner)))
        seen.append(("asked while open", window.asked))

    QTimer.singleShot(0, open_inner)
    seen.append(("outer", m.modal(outer)))
    seen.append(("asked after outer", window.asked))
    for _ in range(20):
        app.processEvents()
    assert seen == [("inner", 0), ("asked while open", 0), ("outer", 0), ("asked after outer", 0)]
    assert window.asked == 1 and not window.isVisible() and m._closing is None and m._depth == 0
