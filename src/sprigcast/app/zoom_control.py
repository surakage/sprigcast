"""Build's zoom, in the top bar: − 100% + Fit and a lock. The % opens the scaling menu (Fit, lock, real size...).
Locked (user, 2026-11-20), the canvas fits and stays put: the wheel, these buttons and dragging leave it alone.

It drives whichever canvas Build is showing (the shape's, or an open sprig's).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton

from sprigcast.app import icons, theme


class ZoomControl(QFrame):
    def __init__(self, preview: Callable[[], object], previews: Callable[[], list] | None = None):
        super().__init__()
        self.preview = preview
        self.previews = previews or (lambda: [p for p in [preview()] if p is not None])  # all of Build's canvases
        self.setObjectName("zoomControl")
        self.setStyleSheet(
            f"#zoomControl {{ background: {theme.PANEL}; border-radius: 19px; }}"
            + theme.button_style(15, "0 10px", "#zoomControl QPushButton", "min-height: 28px;"))
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(4)
        self.out = QPushButton()
        self.out.setIcon(icons.icon("minus", theme.TEXT, 14))
        self.out.setIconSize(QSize(14, 14))
        self.label = QPushButton("100%")
        self.label.setMinimumWidth(64)
        self.inn = QPushButton()
        self.inn.setIcon(icons.icon("plus", theme.TEXT, 14))
        self.inn.setIconSize(QSize(14, 14))
        self.fit = QPushButton("Fit")
        self.lock = QPushButton()
        self.lock.setCheckable(True)
        self.lock.setIconSize(QSize(14, 14))
        self.out.setToolTip("Zoom out (Ctrl+-)")
        self.inn.setToolTip("Zoom in (Ctrl+=)")
        self.label.setToolTip("Scaling: fit, lock, real size")
        self.fit.setToolTip("Fit the canvas (Ctrl+0)")
        for button, name in ((self.out, "Zoom out"), (self.label, "Scaling"), (self.inn, "Zoom in"),
                             (self.fit, "Fit"), (self.lock, "Lock zoom")):
            button.setAccessibleName(name)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(button)
        self.out.clicked.connect(lambda: self._zoom(0.8))
        self.inn.clicked.connect(lambda: self._zoom(1.25))
        self.fit.clicked.connect(self._fit)
        self.label.clicked.connect(self._menu)
        self.lock.toggled.connect(self._lock)
        self.refresh()

    def _zoom(self, factor: float) -> None:
        preview = self.preview()
        if preview is None:
            return
        if preview.zoom_locked:
            return
        preview.zoom_at(factor)
        self.refresh()

    def _fit(self) -> None:
        preview = self.preview()
        if preview is not None and not preview.zoom_locked:
            preview.fit_view()
        self.refresh()

    def _lock(self, on: bool) -> None:
        """Every canvas in Build locks or unlocks together, so switching shape or opening a sprig keeps it."""
        for preview in self.previews():
            if preview.zoom_locked != on:
                preview.set_zoom_locked(on, remember=False)
        from PySide6.QtCore import QSettings

        QSettings().setValue("build/zoom_locked", bool(on))  # remembered for next time
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
        locked = bool(preview.zoom_locked)
        self.label.setText(f"{preview.pixel_scale() * 100:.0f}%")
        self.label.setAccessibleName(f"Scaling {self.label.text()}")  # screen readers hear the value too
        for button in (self.out, self.inn, self.fit):
            button.setEnabled(not locked)
        shade = theme.MUTED if locked else theme.TEXT  # icons don't dim on their own
        self.out.setIcon(icons.icon("minus", shade, 14))
        self.inn.setIcon(icons.icon("plus", shade, 14))
        if self.lock.isChecked() != locked:
            blocked = self.lock.blockSignals(True)
            self.lock.setChecked(locked)
            self.lock.blockSignals(blocked)
        self.lock.setIcon(icons.icon("lock" if locked else "unlock", theme.ACCENT if locked else theme.TEXT, 14))
        self.lock.setToolTip("Zoom locked: click to zoom and move the canvas again" if locked
                             else "Lock the zoom: the canvas fits and stays put")

