"""Build's canvas in a window of its own (user, 2026-11-20: "allow the build canvas to pop out as well").

The main window lends its canvas card's contents (the shape's canvas, or the open sprig's) to this window
and keeps everything else: the library, Leaves and the inspector still drive what's edited here. Closing the
window puts the canvas back in its card."""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import theme

GEOMETRY = "build/canvas_window"


class CanvasWindow(QWidget):
    """The popped-out canvas. `closed` fires when you close it (not when the main window takes it back)."""

    closed = Signal()

    def __init__(self, canvas: QWidget, actions=()):
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("Build canvas · SprigCast")
        self.setObjectName("canvasWindow")
        self.setStyleSheet(f"#canvasWindow {{ background: {theme.CHROME}; }}"
                           f"#canvasWindowCard {{ background: {theme.PANEL}; border: none; border-radius: 28px; }}"
                           "#canvasWindowCard QSplitter::handle { background: transparent; }")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        card = QFrame()
        card.setObjectName("canvasWindowCard")
        self.card = QVBoxLayout(card)
        self.card.setContentsMargins(14, 14, 14, 14)
        self.card.addWidget(canvas)
        canvas.show()
        layout.addWidget(card)
        # The main window's shortcuts (undo, delete, copy, nudge...) work here too.
        self.addActions(list(actions))
        geometry = QSettings().value(GEOMETRY)
        if geometry is None or not self.restoreGeometry(geometry):
            self.resize(1280, 780)
        self._quiet = False

    def take_back(self) -> None:
        """Close without firing `closed` (the main window is taking the canvas back itself)."""
        self._quiet = True
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        QSettings().setValue(GEOMETRY, self.saveGeometry())
        super().closeEvent(event)
        if not self._quiet:
            self.closed.emit()


def placeholder(bring_back) -> QWidget:
    """What the canvas card shows while the canvas is in its own window."""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.addStretch(1)
    words = QLabel("The canvas is in its own window")
    words.setAlignment(Qt.AlignmentFlag.AlignCenter)
    words.setStyleSheet(f"color: {theme.MUTED}; font-size: 15px;")
    layout.addWidget(words)
    back = QPushButton("Bring it back")
    back.setStyleSheet(theme.SMALL_PILL_STYLE)
    back.setCursor(Qt.CursorShape.PointingHandCursor)
    back.clicked.connect(bring_back)
    layout.addWidget(back, 0, Qt.AlignmentFlag.AlignHCenter)
    layout.addStretch(1)
    box.hide()
    return box
