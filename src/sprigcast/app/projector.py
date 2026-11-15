"""Projectors: a canvas, a bloom, a sprig or one source, full screen on a monitor you choose
(or in a window you can move).

A canvas projector shows the newest frame the engine already renders for that canvas. A bloom,
sprig or source projector uses one of the engine's views ("view:<name>", the same thing the
virtual camera uses), drawn only while the projector is open, about 30 times a second, with no
encoding. Esc, a double-click or the right-click menu closes it.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QScreen
from PySide6.QtWidgets import QMenu, QWidget


def screen_label(index: int, screen: QScreen) -> str:
    size = screen.geometry()
    primary = "  (main)" if screen is QGuiApplication.primaryScreen() else ""
    return f"Monitor {index + 1}: {size.width()}×{size.height()}{primary}"


class Projector(QWidget):
    """One canvas on one screen. screen=None opens a normal window instead of full screen."""

    def __init__(self, canvas_id: str, title: str, screen: QScreen | None,
                 on_closed: Callable[["Projector"], None], view: str | None = None):
        super().__init__(None, Qt.WindowType.Window)
        self.canvas_id = canvas_id
        self.view = view  # engine view name for a bloom, sprig or source projector; None for a canvas
        self.pending = None  # newest view frame, handed over by the render thread (see take_pending)
        self.on_closed = on_closed
        self._image: QImage | None = None
        self.setWindowTitle(f"{title} · SprigCast projector")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setCursor(Qt.CursorShape.BlankCursor if screen is not None else Qt.CursorShape.ArrowCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        if screen is not None:
            self.setGeometry(screen.geometry())
            self.showFullScreen()
            handle = self.windowHandle()
            if handle is not None:
                handle.setScreen(screen)
            self.setGeometry(screen.geometry())
        else:
            self.resize(960, 540)
            self.show()

    def pixel_size(self) -> tuple[int, int]:
        dpr = self.devicePixelRatioF()
        return int(self.width() * dpr), int(self.height() * dpr)

    def tap(self, frame) -> None:
        """Render thread: keep only the newest frame (one assignment, so no lock is needed)."""
        self.pending = frame

    def take_pending(self) -> None:
        frame, self.pending = self.pending, None
        if frame is not None:
            self.set_frame(frame.data, frame.width, frame.height)

    def set_frame(self, data: bytes, width: int, height: int) -> None:
        self._image = QImage(data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))
        if self._image is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            w, h = self._image.width(), self._image.height()
            scale = min(self.width() / w, self.height() / h)
            dw, dh = w * scale, h * scale
            p.drawImage(QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh), self._image)
        p.end()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, _event) -> None:  # noqa: N802
        self.close()

    def _menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Close projector  (Esc)").triggered.connect(self.close)
        menu.exec(self.mapToGlobal(pos))

    def closeEvent(self, event) -> None:  # noqa: N802
        self.on_closed(self)
        super().closeEvent(event)
