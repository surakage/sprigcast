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

ART_FOLDER = Path(__file__).with_name("art")
_pictures: dict[str, QPixmap] = {}


def art(name: str) -> QPixmap:
    """A picture from app/art (loaded once): "napping", "welcome", "live", "ending", "trouble", "happy"...

    Empty if it's missing."""
    if name not in _pictures:
        _pictures[name] = QPixmap(str(ART_FOLDER / f"sloth-{name}.png"))
    return _pictures[name]


def sloth_art() -> QPixmap:
    return art("napping")


class ArtPicture(QWidget):
    """One of the sloth pictures, `width` px wide, drawn smoothly at any screen scale."""

    def __init__(self, name: str, width: int, parent: QWidget | None = None, height: int | None = None):
        super().__init__(parent)
        self.box = (width, height)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.set_art(name)

    def set_art(self, name: str) -> None:
        """Show another picture; fits `width` (and `height`, if given) keeping its shape."""
        self.name = name
        picture = art(name)
        ratio = picture.height() / picture.width() if not picture.isNull() else 0.6
        width, height = self.box
        if height is None:
            self.setFixedSize(width, round(width * ratio))
        else:
            self.setFixedSize(width, height)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        picture = art(self.name)
        if picture.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        size = picture.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x, y = (self.width() - size.width()) / 2, (self.height() - size.height()) / 2
        p.drawPixmap(QRectF(x, y, size.width(), size.height()), picture, QRectF(picture.rect()))
        p.end()


class NappingSloth(ArtPicture):
    """A sloth asleep on a leafy vine branch."""

    BASE = QSize(150, 96)

    def __init__(self, scale: float = 1.0, parent: QWidget | None = None):
        super().__init__("napping", round(self.BASE.width() * scale), parent)
        self.setFixedSize(round(self.BASE.width() * scale), round(self.BASE.height() * scale))


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


class LiveCheer(QWidget):
    """"You're live!": the waving sloth with a softly pulsing live dot, shown a few seconds when a stream starts.

    A child of the Live screen, floating over its top centre. It fades away by itself, and a click
    sends it away at once.
    """

    SHOW_MS = 5000

    def __init__(self, parent: QWidget):
        from PySide6.QtCore import QPropertyAnimation, QTimer
        from PySide6.QtWidgets import QGraphicsOpacityEffect, QHBoxLayout

        super().__init__(parent)
        self.setObjectName("liveCheer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"#liveCheer {{ background: {theme.PANEL}; border-radius: 22px; }}")
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 12, 26, 12)
        row.setSpacing(14)
        self.picture = _PulsingLive(96)
        row.addWidget(self.picture)
        self.still = ArtPicture("ending", 96, height=96)  # the goodbye sloth, after a stream ends
        self.still.hide()
        row.addWidget(self.still)
        words = QVBoxLayout()
        words.setSpacing(4)
        words.addStretch(1)
        self.title = QLabel("You're live!")
        self.title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 18px; font-weight: 700;"
                                 " background: transparent;")
        self.where = QLabel("")
        self.where.setWordWrap(True)
        self.where.setMinimumWidth(170)
        self.where.setMaximumWidth(280)
        self.where.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        words.addWidget(self.title)
        words.addWidget(self.where)
        words.addStretch(1)
        row.addLayout(words)
        self.fade = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.fade)
        self._anim = QPropertyAnimation(self.fade, b"opacity", self)
        self._anim.finished.connect(self._faded)
        self._timer = QTimer(self, singleShot=True, interval=self.SHOW_MS)
        self._timer.timeout.connect(lambda: self._fade_to(0.0, 600))
        self.hide()

    def cheer(self, where: str) -> None:
        self._card(True, "You're live!", f"On {where}" if where else "")

    def goodbye(self, line: str) -> None:
        """'Nice stream!' with the waving-goodnight sloth, when a stream ends."""
        self._card(False, "Nice stream!", line)

    def _card(self, live: bool, title: str, line: str) -> None:
        self.picture.setVisible(live)
        self.still.setVisible(not live)
        self.title.setText(title)
        self.where.setText(line)
        self.where.setVisible(bool(line))
        self.adjustSize()
        parent = self.parentWidget()
        self.move(max(0, (parent.width() - self.width()) // 2), 24)
        self.raise_()
        self.show()
        if live:
            self.picture.start()
        self._fade_to(1.0, 250, start=0.0)
        self._timer.start()

    def mousePressEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        self._timer.stop()
        self._fade_to(0.0, 250)

    def _fade_to(self, value: float, ms: int, start: float | None = None) -> None:
        self._anim.stop()
        self._anim.setDuration(ms)
        self._anim.setStartValue(self.fade.opacity() if start is None else start)
        self._anim.setEndValue(value)
        self._anim.start()

    def _faded(self) -> None:
        if self.fade.opacity() <= 0.01:
            self.hide()
            self.picture.stop()


class _PulsingLive(ArtPicture):
    """The waving sloth with SprigCast's own live dot by its paw, glowing in and out."""

    def __init__(self, width: int):
        from PySide6.QtCore import QTimer

        super().__init__("live", width)
        self.phase = 0.0
        self._tick = QTimer(self, interval=50)
        self._tick.timeout.connect(self._step)

    def start(self) -> None:
        self._tick.start()

    def stop(self) -> None:
        self._tick.stop()

    def _step(self) -> None:
        self.phase = (self.phase + 0.05) % 1.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        import math

        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor

        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        centre = QPointF(self.width() * 0.955, self.height() * 0.572)  # just above the waving claws, not covering them
        r = max(3.0, self.width() * 0.045)
        glow = QColor(theme.LIVE)
        glow.setAlphaF(0.1 + 0.12 * (0.5 + 0.5 * math.sin(self.phase * 2 * math.pi)))
        p.setBrush(glow)
        p.drawEllipse(centre, r * 1.45, r * 1.45)
        p.setBrush(QColor(theme.LIVE))
        p.drawEllipse(centre, r, r)
        p.end()
