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
