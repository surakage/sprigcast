"""The safe mode question starts normally on its own unless touched (it sat unanswered 30 minutes after a crash,
2026-11-20, keeping SprigCast down)."""

import logging
import os

import pytest

pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _card():
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.message import MessageCard

    return next((w for w in QApplication.topLevelWidgets() if isinstance(w, MessageCard) and w.isVisible()), None)


def test_no_answer_starts_normally_after_the_count(caplog):
    _app()
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QLabel

    from sprigcast.app import safe_mode

    seen = []
    QTimer.singleShot(300, lambda: seen.extend(label.text() for label in _card().findChildren(QLabel)))
    with caplog.at_level(logging.INFO, logger="sprigcast.app.safe_mode"):
        assert safe_mode.ask(seconds=2) is False
    assert "Starting normally in 2 s" in seen
    assert "safe mode question: no answer, starting normally" in caplog.text
    assert safe_mode.unattended()  # nobody there: the crash card isn't put up over the window (main.py)
    assert safe_mode.AUTO_START_S == 15


def test_a_click_stops_the_count_and_it_waits_for_the_answer(caplog):
    _app()
    from PySide6.QtCore import QPoint, Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLabel, QPushButton

    from sprigcast.app import safe_mode

    texts = []

    def click_card():
        card = _card()
        QTest.mouseClick(card, Qt.MouseButton.LeftButton, pos=QPoint(10, 10))

    def answer_later():
        card = _card()
        texts.extend(label.text() for label in card.findChildren(QLabel))
        safe = next(b for b in card.findChildren(QPushButton) if b.text() == "Safe mode")
        card._shown_at = 0  # armed (presses in its first moment are ignored)
        safe.click()

    QTimer.singleShot(200, click_card)
    QTimer.singleShot(1600, answer_later)  # after the 1 s count would have run out
    with caplog.at_level(logging.INFO, logger="sprigcast.app.safe_mode"):
        assert safe_mode.ask(seconds=1) is True
    assert "Waiting for your choice" in texts
    assert "answered after stopping the count, safe mode" in caplog.text
    assert not safe_mode.unattended()
