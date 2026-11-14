"""Friendly empty states: a little sloth napping on a vine branch (art/sloth-napping.png), with a line saying what goes here.

Used wherever a list or panel has nothing in it yet (Inside, the Sound card, Chat, filters, the
inspector), so an empty spot reads as "ready for you" instead of broken.
"""

from __future__ import annotations

from collections.abc import Callable

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from sprigcast.app import theme

ART = Path(__file__).with_name("art") / "sloth-napping.png"
_art: QPixmap | None = None


def sloth_art() -> QPixmap:
    """The napping sloth picture (loaded once). Empty if the file is missing."""
    global _art
    if _art is None:
        _art = QPixmap(str(ART))
    return _art


class NappingSloth(QWidget):
    """A sloth asleep on a leafy vine branch, drawn to fit its box (aspect kept)."""

    BASE = QSize(150, 96)

    def __init__(self, scale: float = 1.0, parent: QWidget | None = None):
        super().__init__(parent)
        self.scale_by = scale
        self.setFixedSize(round(self.BASE.width() * scale), round(self.BASE.height() * scale))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        art = sloth_art()
        if art.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        size = art.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x, y = (self.width() - size.width()) / 2, (self.height() - size.height()) / 2
        p.drawPixmap(QRectF(x, y, size.width(), size.height()), art, QRectF(art.rect()))
        p.end()


class EmptyState(QWidget):
    """The napping sloth, a short line, and an optional button for the first thing to do."""

    def __init__(self, text: str, action: str | None = None, on_action: Callable[[], None] | None = None,
                 scale: float = 1.0, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        col = QVBoxLayout(self)
        col.setContentsMargins(12, 16, 12, 16)
        col.setSpacing(14)
        self.sloth = NappingSloth(scale)
        col.addWidget(self.sloth, 0, Qt.AlignmentFlag.AlignHCenter)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        col.addWidget(self.label)
        self.button = None
        if action and on_action is not None:
            self.button = QPushButton(action)
            self.button.setStyleSheet(theme.LINK_STYLE)
            self.button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.button.clicked.connect(on_action)
            col.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)
        self.setMinimumHeight(self.sizeHint().height())  # never squeezed so the picture overlaps the words

    def set_text(self, text: str) -> None:
        self.label.setText(text)
