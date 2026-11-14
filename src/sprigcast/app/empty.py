"""Friendly empty states: a little sloth napping on a branch, with a line saying what goes here.

Used wherever a list or panel has nothing in it yet (Inside, the Sound card, Chat, filters, the
inspector), so an empty spot reads as "ready for you" instead of broken.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from sprigcast.app import theme

FUR = "#9a8670"
FUR_DARK = "#6f5f4e"
FACE = "#e3d5bf"


class NappingSloth(QWidget):
    """A sloth asleep, hanging from a leafy branch, drawn to fit its box (aspect kept)."""

    BASE = QSize(132, 78)

    def __init__(self, scale: float = 1.0, parent: QWidget | None = None):
        super().__init__(parent)
        self.scale_by = scale
        self.setFixedSize(round(self.BASE.width() * scale), round(self.BASE.height() * scale))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(self.scale_by, self.scale_by)
        self.draw(p)
        p.end()

    @staticmethod
    def draw(p: QPainter) -> None:
        """Paint in a 132 x 78 box."""
        # The branch: a gentle curve across the top with a few leaves.
        branch = QPainterPath(QPointF(4, 18))
        branch.cubicTo(QPointF(40, 10), QPointF(88, 24), QPointF(128, 14))
        p.setPen(QPen(QColor(theme.ACCENT_DARK), 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(branch)
        for x, y, tilt, mirror in ((14, 4, -20, False), (26, 13, 160, True), (100, 4, 25, True)):
            theme.paint_leaf(p, QRectF(x, y, 15, 15), True, theme.ACCENT, vein=theme.ACCENT_DARK, width=1.0,
                             tilt=tilt, mirror=mirror)
        # Arms and legs wrapped over the branch.
        p.setPen(QPen(QColor(FUR_DARK), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for x0, x1 in ((46, 42), (58, 60), (78, 80), (90, 94)):
            p.drawLine(QPointF(x0, 44), QPointF(x1, 17))
        # Body: a soft round shape hanging below.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(FUR))
        p.drawEllipse(QRectF(38, 30, 62, 40))
        # Head, tucked to one side, with its pale face and dark eye stripes.
        p.drawEllipse(QRectF(88, 34, 30, 28))
        p.setBrush(QColor(FACE))
        p.drawEllipse(QRectF(92, 39, 22, 19))
        p.setBrush(QColor(FUR_DARK))
        p.drawEllipse(QRectF(94, 44, 8, 5))
        p.drawEllipse(QRectF(104, 44, 8, 5))
        # Closed eyes and a small smile.
        p.setPen(QPen(QColor(FACE), 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(95.5, 44.5, 5, 3), 200 * 16, 140 * 16)
        p.drawArc(QRectF(105.5, 44.5, 5, 3), 200 * 16, 140 * 16)
        p.setPen(QPen(QColor(FUR_DARK), 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(QRectF(100, 50, 6, 4), 200 * 16, 140 * 16)
        p.setBrush(QColor(FUR_DARK))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(101.8, 49, 2.6, 1.8))  # nose
        # Zzz drifting up.
        p.setPen(QColor(theme.MUTED))
        for x, y, size in ((119, 34, 7), (125, 27, 9)):
            font = QFont(p.font())
            font.setPixelSize(size)
            font.setBold(True)
            p.setFont(font)
            p.drawText(QPointF(x, y), "z")


class EmptyState(QWidget):
    """The napping sloth, a short line, and an optional button for the first thing to do."""

    def __init__(self, text: str, action: str | None = None, on_action: Callable[[], None] | None = None,
                 scale: float = 1.0, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        col = QVBoxLayout(self)
        col.setContentsMargins(12, 16, 12, 16)
        col.setSpacing(10)
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

    def set_text(self, text: str) -> None:
        self.label.setText(text)
