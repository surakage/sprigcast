"""A 3 x 3 grid of dots for picking a pin point (top left, centre, bottom right...).

    ○ ○ ○
    ○ ● ○     the filled dot is the current one; click another to pick it
    ○ ○ ○
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from sprigcast.app import theme
from sprigcast.core.scene import ALIGNS

NAMES = {"top_left": "Top left", "top": "Top", "top_right": "Top right", "left": "Left", "center": "Center",
         "right": "Right", "bottom_left": "Bottom left", "bottom": "Bottom", "bottom_right": "Bottom right"}


class PinGrid(QWidget):
    STEP, DOT, PAD = 12, 3.6, 4

    def __init__(self, on_pick: Callable[[str], None], parent: QWidget | None = None):
        super().__init__(parent)
        self.current = "center"
        self.hover: int | None = None
        self.on_pick = on_pick
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        side = int(self.PAD * 2 + self.STEP * 2 + self.DOT * 2 + 2)
        self.setFixedSize(side, side)

    def set_current(self, align: str) -> None:
        self.current = align if align in ALIGNS else "center"
        self.setToolTip(NAMES[self.current])
        self.update()

    def _centre(self, i: int) -> QPointF:
        o = self.PAD + self.DOT + 1
        return QPointF(o + (i % 3) * self.STEP, o + (i // 3) * self.STEP)

    def _at(self, pos) -> int | None:
        for i in range(9):
            c = self._centre(i)
            if abs(pos.x() - c.x()) <= self.STEP / 2 and abs(pos.y() - c.y()) <= self.STEP / 2:
                return i
        return None

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.CARD))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
        on = ALIGNS.index(self.current)
        for i in range(9):
            c = self._centre(i)
            if i == on:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(theme.ACCENT))
                p.drawEllipse(c, self.DOT + 0.5, self.DOT + 0.5)
            else:
                p.setPen(QPen(QColor(theme.TEXT if i == self.hover else theme.MUTED), 1.4))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, self.DOT - 1, self.DOT - 1)
        p.end()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        at = self._at(event.position())
        if at != self.hover:
            self.hover = at
            self.setToolTip(NAMES[ALIGNS[at]] if at is not None else NAMES[self.current])
            self.update()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self.hover = None
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        at = self._at(event.position())
        if at is None or ALIGNS[at] == self.current:
            return
        self.set_current(ALIGNS[at])
        self.on_pick(self.current)
