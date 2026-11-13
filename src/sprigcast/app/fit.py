"""Widgets that fit narrow docks: text that shortens with "…" and rows that stack when squeezed.

Docks can be dragged quite narrow, or the whole window made small. Plain Qt buttons and labels then
refuse to get narrower than their text, so a dock's contents get cut off at the edge. These give way
instead: the full text comes back (and shows in the tooltip) once there is room again.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QBoxLayout, QPushButton, QRadioButton, QSizePolicy, QWidget

MIN_TEXT_WIDTH = 44  # how narrow a shortened button may get, in pixels


class _Elides:
    """Mixin for buttons: keep the full text, show as much as fits."""

    def _elide_init(self, text: str) -> None:
        self._full = ""
        self._chrome = 0
        self._own_tip = False
        policy = self.sizePolicy()  # buttons default to "never narrower than the text": allow it
        policy.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        self.setSizePolicy(policy)
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = text
        super().setText(text)
        self._chrome = super().sizeHint().width() - self.fontMetrics().horizontalAdvance(text)
        self._elide()

    def full_text(self) -> str:
        return self._full

    def setToolTip(self, tip: str) -> None:
        self._own_tip = bool(tip)
        super().setToolTip(tip)

    def _elide(self) -> None:
        room = max(0, self.width() - self._chrome)
        if not self.isVisible():
            room = 1 << 20  # not laid out yet: keep the full text until it's on screen at its real size
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, room)
        if shown != self.text():
            super().setText(shown)
        if not self._own_tip:
            super().setToolTip(self._full if shown != self._full else "")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._elide()

    def sizeHint(self) -> QSize:
        return QSize(self._chrome + self.fontMetrics().horizontalAdvance(self._full), super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:
        return QSize(min(self.sizeHint().width(), self._chrome + MIN_TEXT_WIDTH), super().minimumSizeHint().height())


class FitButton(_Elides, QPushButton):
    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._elide_init(text)


class FitRadio(_Elides, QRadioButton):
    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._elide_init(text)


class AdaptiveRow(QWidget):
    """Widgets side by side while they fit at their natural width, stacked one under another when not."""

    def __init__(self, *widgets: QWidget, spacing: int = 4):
        super().__init__()
        self._box = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(spacing)
        self._widgets = widgets
        for w in widgets:
            self._box.addWidget(w, 1)

    def _needed(self) -> int:
        return sum(w.sizeHint().width() for w in self._widgets) + self._box.spacing() * (len(self._widgets) - 1)

    def minimumSizeHint(self) -> QSize:
        widest = max((w.minimumSizeHint().width() for w in self._widgets), default=0)
        return QSize(widest, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:
        wanted = (QBoxLayout.Direction.LeftToRight if event.size().width() >= self._needed()
                  else QBoxLayout.Direction.TopToBottom)
        if self._box.direction() != wanted:
            self._box.setDirection(wanted)
        super().resizeEvent(event)

    def is_stacked(self) -> bool:
        return self._box.direction() == QBoxLayout.Direction.TopToBottom
