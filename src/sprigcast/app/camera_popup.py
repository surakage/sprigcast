"""The Live screen's Virtual camera pop-up: what SprigCast Camera shows, in two drop-downs.

It reads and writes the same "Shows" setting as Settings → Video → Virtual camera (CAMERA_SHOWS),
so the two always agree. The camera reads the setting on every poll, so a change shows at once.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import theme
from sprigcast.app.output_page import CAMERA_SHOWS, VIDEO_KINDS_SKIP, camera_choice
from sprigcast.core.scene import SceneCollection


def _blooms(collection: SceneCollection) -> list[tuple[str, str]]:
    return [(f"{s.name} ({s.canvas})", s.id) for s in collection.scenes if s.kind == "show" and not s.shape_of]


def _sources(collection: SceneCollection) -> list[tuple[str, str]]:
    return [(s.name, s.id) for s in collection.sources if s.kind not in VIDEO_KINDS_SKIP]


class CameraPicker(QWidget):
    """Shows: Live output, a canvas, a bloom or a source. A second drop-down appears to pick which one."""

    def __init__(self, collection: SceneCollection, repair: Callable[[], None] | None = None,
                 status: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.collection = collection
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        self.shows = QComboBox()
        self.shows.addItem("Live output", "auto")
        for canvas in collection.canvases:
            self.shows.addItem(canvas.id.capitalize(), f"canvas:{canvas.id}")
        if _blooms(collection):
            self.shows.addItem("A bloom", "bloom")
        if _sources(collection):
            self.shows.addItem("A source", "source")
        self.shows.setMinimumWidth(240)
        form.addRow("Shows", self.shows)
        self.which = QComboBox()
        self.which_label = QLabel("Which")
        form.addRow(self.which_label, self.which)
        box.addLayout(form)
        from sprigcast.app.starter import combo_style

        for combo in (self.shows, self.which):
            combo.setStyleSheet(combo_style())  # rounded pill boxes, like the Starter kit's
        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        box.addWidget(self.note)
        self.repair = QPushButton("Repair the camera")
        self.repair.setStyleSheet(theme.PILL_STYLE)
        self.repair.setCursor(Qt.CursorShape.PointingHandCursor)
        self.repair.setVisible(repair is not None)
        if repair is not None:
            self.repair.clicked.connect(repair)
        box.addWidget(self.repair, 0, Qt.AlignmentFlag.AlignLeft)
        self.set_status(status)
        self._load(camera_choice())
        self.shows.currentIndexChanged.connect(self._kind_changed)
        self.which.currentIndexChanged.connect(lambda _i: self._save())

    def _load(self, choice: str) -> None:
        kind, _, ref = choice.partition(":")
        index = self.shows.findData(choice if kind in ("auto", "canvas") else kind)
        self.shows.setCurrentIndex(max(0, index))
        self._fill_which(ref)

    def _fill_which(self, ref: str = "") -> None:
        kind = self.shows.currentData()
        items = _blooms(self.collection) if kind == "bloom" else _sources(self.collection) if kind == "source" else []
        self.which.blockSignals(True)
        self.which.clear()
        for label, item_id in items:
            self.which.addItem(label, item_id)
        self.which.setCurrentIndex(max(0, self.which.findData(ref)))
        self.which.blockSignals(False)
        self.which.setVisible(bool(items))
        self.which_label.setVisible(bool(items))
        self.which_label.setText("Bloom" if kind == "bloom" else "Source")

    def _kind_changed(self) -> None:
        self._fill_which()
        self._save()

    def choice(self) -> str:
        kind = self.shows.currentData()
        if kind in ("bloom", "source"):
            return f"{kind}:{self.which.currentData() or ''}"
        return kind or "auto"

    def _save(self) -> None:
        QSettings().setValue(CAMERA_SHOWS, self.choice())

    def set_status(self, text: str) -> None:
        self.note.setText(text or "Apps list it as “SprigCast Camera”.")


def camera_popup(collection: SceneCollection, parent: QWidget | None = None,
                 repair: Callable[[], None] | None = None, status: str = ""):
    from sprigcast.app.transitions_panel import PopUp

    pop = PopUp("Virtual camera", parent)
    pop.picker = CameraPicker(collection, repair, status)
    pop.add_tab("camera", "Camera", pop.picker)
    pop.show_tab("camera")
    return pop
