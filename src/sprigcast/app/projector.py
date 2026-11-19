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
    main = " · main" if screen is QGuiApplication.primaryScreen() else ""
    return f"Full screen on Screen {index + 1}{main}  ({size.width()}×{size.height()})"


HIDE_POINTER = "projector/hide_pointer"  # Settings → General → Pop-out views (full-screen ones)
ON_TOP = "projector/on_top"


def hide_pointer() -> bool:
    from PySide6.QtCore import QSettings

    return QSettings().value(HIDE_POINTER, True, type=bool)


def on_top() -> bool:
    from PySide6.QtCore import QSettings

    return QSettings().value(ON_TOP, False, type=bool)


REOPEN = "projector/reopen"  # Settings → General → Pop-out views → Reopen them next time
OPEN_LIST = "projector/open"  # the pop-outs that were open when SprigCast closed (JSON), kept only with REOPEN on


def reopen() -> bool:
    from PySide6.QtCore import QSettings

    return QSettings().value(REOPEN, False, type=bool)


def describe(projector) -> dict:
    """What it takes to open this pop-out again: what it shows, and the screen it filled or where its window sat."""
    kind, ref = getattr(projector, "target", ("canvas", projector.canvas_id))
    full = projector.isFullScreen()
    screen = projector.screen()
    box = projector.geometry()
    return {"canvas": projector.canvas_id, "kind": kind, "ref": ref,
            "screen": (screen.name() if screen is not None else "") if full else "",
            "window": [] if full else [box.x(), box.y(), box.width(), box.height()]}


def save_open(projectors) -> None:
    """SprigCast is closing: keep the open pop-outs for next time (or forget them when the setting is off)."""
    import json

    from PySide6.QtCore import QSettings

    if reopen():
        QSettings().setValue(OPEN_LIST, json.dumps([describe(p) for p in projectors]))
    else:
        QSettings().remove(OPEN_LIST)


def saved() -> list[dict]:
    """The pop-outs kept by save_open ([] with the setting off, or when what's saved can't be read)."""
    import json

    from PySide6.QtCore import QSettings

    if not reopen():
        return []
    try:
        found = json.loads(QSettings().value(OPEN_LIST, "[]", type=str) or "[]")
    except ValueError:
        return []
    return [d for d in found if isinstance(d, dict)] if isinstance(found, list) else []


def on_a_screen(window: list, screens) -> bool:
    """Whether a saved window ([x, y, w, h]) would still be reachable: a good part of it on some screen."""
    from PySide6.QtCore import QRect

    if len(window) != 4:
        return False
    box = QRect(*(int(v) for v in window))
    return any(s.availableGeometry().intersected(box).width() >= 100
               and s.availableGeometry().intersected(box).height() >= 60 for s in screens)


class Projector(QWidget):
    """One canvas on one screen. screen=None opens a normal window instead of full screen."""

    def __init__(self, canvas_id: str, title: str, screen: QScreen | None,
                 on_closed: Callable[["Projector"], None], view: str | None = None):
        flags = Qt.WindowType.Window | (Qt.WindowType.WindowStaysOnTopHint if on_top() else Qt.WindowType.Widget)
        super().__init__(None, flags)
        self.canvas_id = canvas_id
        self.view = view  # engine view name for a bloom, sprig or source projector; None for a canvas
        self.pending = None  # newest view frame, handed over by the render thread (see take_pending)
        self.on_closed = on_closed
        self._image: QImage | None = None
        self._data = None  # the bytes _image shows in place
        self.setWindowTitle(f"{title} · SprigCast projector")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        blank = screen is not None and hide_pointer()
        self.setCursor(Qt.CursorShape.BlankCursor if blank else Qt.CursorShape.ArrowCursor)
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
        if isinstance(data, bytes):  # bytes never change: the image uses them in place while we hold them
            self._data = data
            self._image = QImage(data, width, height, width * 4, QImage.Format.Format_RGBX8888)
        else:
            self._data = None
            self._image = QImage(data, width, height, width * 4, QImage.Format.Format_RGBX8888).copy()
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        # Painting holds Python's lock, and a full-screen projector paints ~30 times a second: keep it to
        # one straight copy when the frame already has the screen's size (the stream drops alpha too).
        p = QPainter(self)
        image = self._image
        if image is None:
            p.fillRect(self.rect(), QColor("#000000"))
            p.end()
            return
        w, h = image.width(), image.height()
        scale = min(self.width() / w, self.height() / h)
        dw, dh = w * scale, h * scale
        rect = QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh)
        if dw < self.width() - 0.5 or dh < self.height() - 0.5:  # bars at the sides or top and bottom
            p.fillRect(self.rect(), QColor("#000000"))
        if abs(dw - w) > 0.5 or abs(dh - h) > 0.5:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(rect, image)
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
        menu.addAction("Close  (Esc)").triggered.connect(self.close)
        menu.exec(self.mapToGlobal(pos))
        menu.deleteLater()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.on_closed(self)
        super().closeEvent(event)
