"""The Live panel shown in Live mode: big scene buttons per canvas and a large on-air clock.

In Live mode the editing docks step aside; switching scenes is one click on a big button
(linked scenes on the other canvas follow, as everywhere else).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.core.scene import SceneCollection

COLUMNS = 4


class LivePanel(QWidget):
    def __init__(self, on_scene: Callable[[str], None]):
        super().__init__()
        self.on_scene = on_scene
        self.buttons: dict[str, QPushButton] = {}
        self._signature: tuple = ()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.clock = QLabel("Offline")
        font = self.clock.font()
        font.setPointSizeF(font.pointSizeF() * 1.8)
        font.setBold(True)
        self.clock.setFont(font)
        self.rec = QLabel("")
        top.addWidget(self.clock, 1)
        top.addWidget(self.rec)
        root.addLayout(top)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(8)
        root.addLayout(self.rows)
        root.addStretch(1)

    def show_scenes(self, collection: SceneCollection) -> None:
        signature = tuple((s.id, s.name, s.canvas) for s in collection.scenes)
        if signature != self._signature:
            self._signature = signature
            self._rebuild(collection)
        for canvas in collection.canvases:
            active = collection.active_scenes.get(canvas.id)
            for scene in collection.scenes_for(canvas.id):
                button = self.buttons[scene.id]
                button.blockSignals(True)
                button.setChecked(scene.id == active)
                button.blockSignals(False)

    def _rebuild(self, collection: SceneCollection) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.buttons = {}
        for canvas in collection.canvases:
            scenes = collection.scenes_for(canvas.id)
            if not scenes:
                continue
            title = QLabel(f"{canvas.id.capitalize()}  ·  {canvas.width}×{canvas.height}")
            title.setStyleSheet("color: #8d8a83")
            self.rows.addWidget(title)
            grid_box = QWidget()
            grid = QGridLayout(grid_box)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setSpacing(6)
            for i, scene in enumerate(scenes):
                button = QPushButton(scene.name + ("  ⇄" if scene.linked_to else ""))
                if scene.linked_to:
                    button.setToolTip("Linked: switches together with its partner on the other canvas")
                button.setCheckable(True)
                button.setMinimumHeight(52)
                font = button.font()
                font.setPointSizeF(font.pointSizeF() * 1.15)
                button.setFont(font)
                button.clicked.connect(lambda _=False, sid=scene.id: self.on_scene(sid))
                self.buttons[scene.id] = button
                grid.addWidget(button, i // COLUMNS, i % COLUMNS)
            self.rows.addWidget(grid_box)

    def set_clock(self, text: str, live: bool) -> None:
        self.clock.setText(text)
        self.clock.setStyleSheet("color: #e8a4b8" if live else "")

    def set_recording(self, text: str) -> None:
        self.rec.setText(text)
