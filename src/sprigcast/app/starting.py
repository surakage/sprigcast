"""A small "Starting" card shown the moment the app exists, until the main window is up, and the notice a second
launch shows (SprigCast already open, or still starting).

On a slow PC with a hard drive the window took close to a minute to show (tester PC, 2026-11-22): with nothing on
screen, people clicked again and again, and each click opened another copy. The card is painted by hand (no
stylesheet, no fonts to load) so it costs nothing to show early.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from sprigcast.app import theme

WIDTH, HEIGHT = 340, 132
DOTS_MS = 400  # the dots after "Starting" move, so it never looks stuck


class StartingCard(QWidget):
    def __init__(self, mark: QIcon | None = None, title: str = "SprigCast", line: str = "Starting",
                 dots: bool = True, ok: bool = False):
        super().__init__(None, Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        if not ok:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle("SprigCast")
        self.title, self.line, self.dots = title, line, dots
        words = max(QFontMetrics(self._font(22, True)).horizontalAdvance(title),
                    QFontMetrics(self._font(15)).horizontalAdvance(line + ("..." if dots else "")))
        self.setFixedSize(max(WIDTH, 28 + (72 if mark is not None else 0) + words + 36), HEIGHT + (58 if ok else 0))
        self._mark = mark
        self._dots = 1
        self.ok_button = None
        if ok:
            self.ok_button = QPushButton("OK", self)
            self.ok_button.setStyleSheet(theme.button_style(14, "8px 26px"))
            self.ok_button.adjustSize()
            self.ok_button.move(self.width() - self.ok_button.width() - 24, self.height() - self.ok_button.height() - 22)
            self.ok_button.clicked.connect(self.done)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        screen = QApplication.primaryScreen()
        if screen is not None:
            self.move(screen.availableGeometry().center() - self.rect().center())

    def _font(self, pixels: int, bold: bool = False) -> QFont:
        font = QFont(self.font())
        font.setPixelSize(pixels)
        font.setBold(bold)
        return font

    def _tick(self) -> None:
        self._dots = self._dots % 3 + 1
        self.update()

    def showEvent(self, event) -> None:
        if self.dots:
            self._timer.start(DOTS_MS)
        super().showEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(card, 18, 18)
        p.fillPath(path, QColor(theme.SECTION))
        p.setPen(QPen(QColor(theme.SECTION_EDGE), 1))
        p.drawPath(path)
        left = 28.0
        top = 6 if self.ok_button is not None else 0  # with a button: the words sit a little lower
        if self._mark is not None:
            self._mark.paint(p, 28, (HEIGHT - 52) // 2 + top, 52, 52)
            left = 28 + 52 + 20
        width = self.width() - left - 24
        p.setFont(self._font(22, True))
        p.setPen(QColor(theme.TEXT_BRIGHT))
        p.drawText(QRectF(left, 34 + top, width, 30), Qt.AlignmentFlag.AlignVCenter, self.title)
        p.setFont(self._font(15))
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(left, 68 + top, width, 24), Qt.AlignmentFlag.AlignVCenter,
                   self.line + ("." * self._dots if self.dots else ""))
        p.end()

    def done(self) -> None:
        self._timer.stop()
        self.close()
        self.deleteLater()


def show_starting(mark: QIcon | None = None) -> StartingCard | None:
    """Show the card at once (painted before the slow part begins); None if it can't be shown."""
    try:
        card = StartingCard(mark)
        card.show()
        card.repaint()
        QApplication.processEvents()  # nothing else is queued yet: this only puts the card on screen
        return card
    except Exception:  # noqa: BLE001 - a missing card never stops SprigCast from opening
        return None
