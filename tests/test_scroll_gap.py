import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication, QLabel, QScrollArea  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_every_scroll_bar_keeps_a_gap_from_what_it_scrolls(app):
    """The user saw the Live Sound bar touching the Mute buttons (2026-11-18): every bar, app-wide, keeps
    SCROLL_GAP between itself and the content, from one shared rule."""
    from pathlib import Path

    from sprigcast.app import palette, theme

    assert theme.SCROLL_BARS in theme.STYLESHEET and theme.SCROLL_BARS in palette.STYLE
    assert theme.SCROLL_GAP >= 8
    # No widget brings its own scroll bar rule (it would undo the gap there).
    src = Path(theme.__file__).parent
    own = [p.name for p in src.rglob("*.py") if p.name != "theme.py" and "QScrollBar:" in p.read_text("utf-8")]
    assert own == []
    area = QScrollArea()
    area.setStyleSheet(theme.SCROLL_BARS)
    label = QLabel("\n".join("row" for _ in range(80)))
    area.setWidget(label)
    area.resize(200, 120)
    area.show()
    app.processEvents()
    bar = area.verticalScrollBar()
    assert bar.isVisible() and bar.width() >= 6 + theme.SCROLL_GAP
