"""Build's zoom, in the top bar: − 100% + Fit. The % opens the scaling menu (Fit, lock, real size...).

It drives whichever canvas Build is showing (the shape's, or an open sprig's).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton

from sprigcast.app import theme


class ZoomControl(QFrame):
    def __init__(self, preview: Callable[[], object]):
        super().__init__()
        self.preview = preview
        self.setObjectName("zoomControl")
        self.setStyleSheet(
            f"#zoomControl {{ background: {theme.PANEL}; border-radius: 19px; }}"
            f"#zoomControl QPushButton {{ background: transparent; border: none; border-radius: 15px;"
            f" color: {theme.TEXT}; font-weight: 600; padding: 0 10px; min-height: 30px; }}"
            f"#zoomControl QPushButton:hover {{ background: {theme.RAISED}; }}")
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(0)
        self.out = QPushButton("−")
        self.label = QPushButton("100%")
        self.label.setMinimumWidth(64)
        self.inn = QPushButton("+")
        self.fit = QPushButton("Fit")
        self.out.setToolTip("Zoom out (Ctrl+-)")
        self.inn.setToolTip("Zoom in (Ctrl+=)")
        self.label.setToolTip("Scaling: fit, lock, real size")
        self.fit.setToolTip("Fit the canvas (Ctrl+0)")
        for button in (self.out, self.label, self.inn, self.fit):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(button)
        self.out.clicked.connect(lambda: self._zoom(0.8))
        self.inn.clicked.connect(lambda: self._zoom(1.25))
        self.fit.clicked.connect(self._fit)
        self.label.clicked.connect(self._menu)

    def _zoom(self, factor: float) -> None:
        preview = self.preview()
        if preview is None:
            return
        if preview.zoom_locked:
            preview.set_zoom_locked(False)
        preview.zoom_at(factor)
        self.refresh()

    def _fit(self) -> None:
        preview = self.preview()
        if preview is not None:
            preview.fit_view()
        self.refresh()

    def _menu(self) -> None:
        preview = self.preview()
        if preview is not None:
            preview._scaling_menu(self.label.mapToGlobal(QPoint(0, self.label.height() + 6)), above=False)
        self.refresh()

    def refresh(self) -> None:
        preview = self.preview()
        if preview is None:
            return
        self.label.setText("Fit" if preview.zoom_locked else f"{preview.pixel_scale() * 100:.0f}%")

