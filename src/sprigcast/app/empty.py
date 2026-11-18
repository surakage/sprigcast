"""Friendly empty states: a little sloth napping on a vine branch (art/sloth-napping.png), with a line saying what goes here.

Used wherever a list or panel has nothing in it yet (Inside, the Sound card, Chat, filters, the
inspector), so an empty spot reads as "ready for you" instead of broken.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from sprigcast.app import theme
from sprigcast.app.open_folder import open_folder

ART_FOLDER = Path(__file__).with_name("art")
_pictures: dict[str, QPixmap] = {}
_images: dict[str, QImage] = {}
_shrunk: dict[tuple, QPixmap] = {}  # (name, width, height, screen scale): every picture of that size shares it


ART_MAX = 640  # kept this wide at most: the largest shows 260 px wide, so 2.5x screens still get every pixel
WARM_ART = ("welcome", "happy")  # what About, Getting started, the Garden check and Stream doctor open with


def _read(name: str) -> QImage:
    image = QImage(str(ART_FOLDER / f"sloth-{name}.png"))
    if image.width() > ART_MAX:  # 1.6 MB kept instead of the PNG's 4 MB
        image = image.scaledToWidth(ART_MAX, Qt.TransformationMode.SmoothTransformation)
    return image


def warm_art(names=WARM_ART) -> None:
    """Read the pop-ups' sloth pictures (1024 px PNGs) on a worker, a while after start: read when a pop-up first
    showed one, it was most of opening About (64 ms) on the PC while live (2026-11-18). Never raises."""
    for name in names:
        if name not in _images:
            _images[name] = _read(name)


def _image(name: str) -> QImage:
    if name not in _images:
        _images[name] = _read(name)
    return _images[name]


def art(name: str) -> QPixmap:
    """A picture from app/art (loaded once): "napping", "welcome", "live", "ending", "trouble", "happy"...

    Empty if it's missing."""
    if name not in _pictures:
        image = _image(name)
        _pictures[name] = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
    return _pictures[name]


def shrunk_art(name: str, width: int, height: int, ratio: float) -> QPixmap:
    """The picture shrunk once, by area averaging, to width x height device-independent pixels at `ratio`.

    Drawing a 1024 px picture straight into ~100 px skips pixels and leaves light specks in the fur. Kept for the
    run: each new pop-up shrank its own copy again."""
    key = (name, width, height, ratio)
    if key not in _shrunk:
        image = _image(name).scaled(round(width * ratio), round(height * ratio),
                                    Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        pixmap = QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(ratio)
        _shrunk[key] = pixmap
    return _shrunk[key]


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
        picture = _image(name)
        ratio = picture.height() / picture.width() if not picture.isNull() else 0.6
        width, height = self.box
        if height is None:
            self.setFixedSize(width, round(width * ratio))
        else:
            self.setFixedSize(width, height)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        picture = _image(self.name)
        if picture.isNull():
            return
        p = QPainter(self)
        size = picture.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x, y = (self.width() - size.width()) / 2, (self.height() - size.height()) / 2
        p.drawPixmap(QPointF(x, y), self._shrunk(picture, size))
        p.end()

    def _shrunk(self, picture, size) -> QPixmap:
        """The picture shrunk to the pixels it covers on this screen (shrunk_art)."""
        return shrunk_art(self.name, size.width(), size.height(), self.devicePixelRatioF())


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
        for w in (self, self.title, self.where):
            w.ensurePolished()  # sized with the 18 px bold title, not the plain font it has before it's styled
        if line:  # the line under it wraps rather than being cut off
            width = min(max(170, self.where.fontMetrics().horizontalAdvance(line) + 2), 280)
            self.where.setFixedWidth(width)
            self.where.setFixedHeight(self.where.heightForWidth(width))
        self.layout().invalidate()
        self.layout().activate()
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

    DOT = (1.02, 0.46)  # dot centre, as a share of the art's size: just right of the open claws
    DOT_ROOM = 0.09  # extra width to the right of the art so the dot and its glow fit

    def __init__(self, width: int):
        from PySide6.QtCore import QTimer

        super().__init__("live", width)
        self.art_width = width
        self.setFixedSize(width + round(width * self.DOT_ROOM), self.height())  # the dot sits just past the art
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

        p = QPainter(self)
        picture = _image(self.name)
        side = self.art_width
        if not picture.isNull():
            size = picture.size().scaled(QSize(side, self.height()), Qt.AspectRatioMode.KeepAspectRatio)
            p.drawPixmap(QPointF(0, (self.height() - size.height()) / 2), self._shrunk(picture, size))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        centre = QPointF(side * self.DOT[0], self.height() * self.DOT[1])
        r = max(3.0, side * 0.034)
        glow = QColor(theme.LIVE)
        glow.setAlphaF(0.1 + 0.12 * (0.5 + 0.5 * math.sin(self.phase * 2 * math.pi)))
        p.setBrush(glow)
        p.drawEllipse(centre, r * 1.45, r * 1.45)
        p.setBrush(QColor(theme.LIVE))
        p.drawEllipse(centre, r, r)
        p.end()


class SavedNote(QWidget):
    """"Screenshot saved" / "Clip saved": a clear note over the bottom of the window for a few seconds, so a save
    from a hotkey, the Stream Deck or a bot is easy to see. A click opens the folder."""

    SHOW_MS = 3000
    MAX_W = 620  # wider than this, the words wrap (and never past the window's edges)

    def __init__(self, parent: QWidget):
        from PySide6.QtCore import QPropertyAnimation, QTimer
        from PySide6.QtWidgets import QGraphicsOpacityEffect, QHBoxLayout

        super().__init__(parent)
        self.setObjectName("savedNote")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"#savedNote {{ background: {theme.RAISED}; border: 1px solid {theme.EDGE_STRONG};"
                           " border-radius: 20px; }")  # stands out from the cards it floats over
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 10, 22, 10)
        row.setSpacing(12)
        self.tick = tick = QLabel("\u2713")
        tick.setFixedSize(28, 28)
        tick.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(tick, 0, Qt.AlignmentFlag.AlignTop)
        from PySide6.QtWidgets import QVBoxLayout

        words = QVBoxLayout()
        words.setSpacing(2)
        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 16px; font-weight: 700; background: transparent;")
        words.addWidget(self.title)
        self.detail = QLabel("")  # a line under the title (what's new in an updated plugin)
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        self.detail.hide()
        words.addWidget(self.detail)
        row.addLayout(words, 1)
        self.open = QLabel("Open folder")
        self.open.setStyleSheet(f"color: {theme.ACCENT}; background: transparent;")
        row.addWidget(self.open)
        self.folder = None
        self.on_click = None
        self.fade = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.fade)
        self._anim = QPropertyAnimation(self.fade, b"opacity", self)
        self._anim.finished.connect(lambda: self.fade.opacity() <= 0.01 and self.hide())
        self._timer = QTimer(self, singleShot=True, interval=self.SHOW_MS)
        self._timer.timeout.connect(lambda: self._fade_to(0.0, 500))
        self.hide()

    def say(self, title: str, folder=None, detail: str = "", link: str = "", on_click=None, ms: int | None = None,
            kind: str = "done") -> None:
        """Show the note; a click opens `folder`, or runs `on_click` (named by `link`). kind "warn": an amber "!"
        for a heads-up (the PC is busy) instead of the green tick."""
        warn = kind == "warn"
        self.tick.setText("!" if warn else "\u2713")
        self.tick.setStyleSheet(f"background: {theme.WARN if warn else theme.ACCENT}; color: {theme.ACCENT_TEXT};"
                                " border-radius: 14px; font-size: 16px; font-weight: 800;")
        self.title.setText(title)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))
        self.folder = folder
        self.on_click = on_click
        self.open.setText(link or "Open folder")
        self.open.setVisible(folder is not None or on_click is not None)
        self._timer.setInterval(ms or self.SHOW_MS)
        self._fit()
        parent = self.parentWidget()
        self.move(max(0, (parent.width() - self.width()) // 2), max(0, parent.height() - self.height() - 48))
        self.raise_()
        self.show()
        self._fade_to(1.0, 200, start=0.0 if self.fade.opacity() <= 0.01 or not self.isVisible() else None)
        self._timer.start()

    def _fit(self) -> None:
        """As wide as its words, up to MAX_W (and the window less a margin each side); longer words wrap onto
        more lines, so nothing is ever cut off. Sized after its look is applied: the 16 px bold title is
        wider than the plain font a not-yet-styled label measures with."""
        for w in (self, self.tick, self.title, self.detail, self.open):
            w.ensurePolished()
        parent = self.parentWidget()
        limit = min(self.MAX_W, (parent.width() if parent is not None else self.MAX_W) - 64)
        margins = self.layout().contentsMargins()
        spacing = self.layout().spacing()
        fixed = margins.left() + margins.right() + self.tick.width() + spacing
        if self.open.isVisibleTo(self):
            fixed += self.open.sizeHint().width() + spacing
        room = max(120, limit - fixed)
        words = 0
        for label in (self.title, self.detail):
            label.setMinimumSize(0, 0)
            label.setMaximumSize(16777215, 16777215)
            if label.isHidden() and label is self.detail:
                continue
            natural = label.fontMetrics().horizontalAdvance(label.text()) + 2
            words = max(words, min(natural, room))
        for label in (self.title, self.detail):
            if label is self.detail and not self.detail.text():
                continue
            label.setFixedWidth(words)
            label.setFixedHeight(label.heightForWidth(words))
        self.layout().invalidate()
        self.layout().activate()
        self.adjustSize()

    def mousePressEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        if self.on_click is not None:
            self.on_click()
        elif self.folder is not None:
            open_folder(self.folder)
        self._timer.stop()
        self._fade_to(0.0, 200)

    def _fade_to(self, value: float, ms: int, start: float | None = None) -> None:
        self._anim.stop()
        self._anim.setDuration(ms)
        self._anim.setStartValue(self.fade.opacity() if start is None else start)
        self._anim.setEndValue(value)
        self._anim.start()
