"""A widget that shows one canvas, scaled to fit with its aspect ratio kept."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget


class Preview(QWidget):
    def __init__(self, title: str, aspect: tuple[int, int], parent: QWidget | None = None):
        super().__init__(parent)
        self.title = title
        self.subtitle = ""
        self.aspect = aspect
        self._image: QImage | None = None
        self.setMinimumSize(160, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_frame(self, data: bytes, width: int, height: int) -> None:
        # copy() detaches the image from the bytes object, which the engine may drop at any time.
        self._image = QImage(data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
        self.aspect = (width, height)
        self.update()

    def canvas_rect(self) -> QRectF:
        """Where the canvas is drawn inside the widget, leaving room for the title line."""
        top = 24.0
        avail_w, avail_h = float(self.width()), float(self.height()) - top
        aw, ah = self.aspect
        scale = min(avail_w / aw, avail_h / ah)
        w, h = aw * scale, ah * scale
        return QRectF((avail_w - w) / 2, top + (avail_h - h) / 2, w, h)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#141414"))
        p.setPen(QColor("#d8d8d8"))
        label = f"{self.title}  ·  {self.subtitle}" if self.subtitle else self.title
        p.drawText(8, 17, label)
        rect = self.canvas_rect()
        if self._image is None:
            p.fillRect(rect, QColor("#000000"))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Waiting for the renderer…")
        else:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(rect, self._image)
        p.end()
