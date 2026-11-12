"""The Live panel shown in Live mode: big scene buttons per canvas and a large on-air clock.

In Live mode the editing docks step aside; switching scenes is one click on a big button
(linked scenes on the other canvas follow, as everywhere else).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app.terms import PART_MARK, PARTS, cap
from sprigcast.core.scene import SceneCollection

COLUMNS = 4


class LivePanel(QWidget):
    def __init__(self, on_scene: Callable[[str], None], on_state: Callable[[str, str | None], None] | None = None,
                 on_studio: Callable[[bool], None] | None = None, on_go: Callable[[], None] | None = None):
        super().__init__()
        self.on_scene = on_scene
        self.on_state = on_state or (lambda part_id, name: None)
        self.staged: set[str] = set()
        self.buttons: dict[str, QPushButton] = {}
        self.state_buttons: dict[tuple[str, str | None], QPushButton] = {}  # (sprig id, state name or None)
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
        studio_row = QHBoxLayout()
        self.studio = QCheckBox("Studio mode")
        self.studio.setToolTip("Click a scene to line it up in the Next preview, then press Go to send it on air")
        self.studio.toggled.connect(lambda on: (on_studio or (lambda _on: None))(on))
        self.go = QPushButton("Go  ▶")
        self.go.setToolTip("Send the lined-up scenes on air (with the transition)")
        self.go.setMinimumHeight(40)
        self.go.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 6px 28px; }"
            "QPushButton:enabled { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ff8fc4, stop:1 #f23f8f);"
            " color: #0c1d14; border: 1px solid #c02670; }")
        self.go.clicked.connect(lambda: (on_go or (lambda: None))())
        self.go.setVisible(False)
        studio_row.addWidget(self.studio)
        studio_row.addStretch(1)
        studio_row.addWidget(self.go)
        root.addLayout(studio_row)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(8)
        root.addLayout(self.rows)
        root.addStretch(1)

    def show_scenes(self, collection: SceneCollection) -> None:
        signature = (tuple((s.id, s.name, s.canvas, s.kind, s.folder, s.linked_to) for s in collection.scenes),
                     tuple((f.id, f.name, f.colour, f.live) for f in collection.folders),
                     tuple((p.id, p.name, tuple(s.name for s in p.states)) for p in collection.parts()))
        if signature != self._signature:
            self._signature = signature
            self._rebuild(collection)
        current = {p.id: p.state for p in collection.parts()}
        for (part_id, name), button in self.state_buttons.items():
            button.blockSignals(True)
            button.setChecked(current.get(part_id) == name)
            button.blockSignals(False)
        for canvas in collection.canvases:
            active = collection.active_scenes.get(canvas.id)
            for scene in collection.scenes_for(canvas.id):
                button = self.buttons.get(scene.id)
                if button is None:
                    continue  # not in a Live folder
                button.blockSignals(True)
                button.setChecked(scene.id == active)
                staged = scene.id in self.staged and scene.id != active
                if button.property("staged") != staged:
                    button.setProperty("staged", staged)
                    button.setStyleSheet("QPushButton { border: 2px dashed #ffd23f; }" if staged else "")
                button.blockSignals(False)

    def _rebuild(self, collection: SceneCollection) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)  # off screen now, not at the next event loop turn
                widget.deleteLater()
        self.buttons = {}
        self.state_buttons = {}
        for canvas in collection.canvases:
            groups = [(f, shows) for f, shows in collection.live_scenes(canvas.id) if shows]
            if not groups:
                continue
            title = QLabel(f"{canvas.id.capitalize()}  ·  {canvas.width}×{canvas.height}")
            title.setStyleSheet("color: #8cc4a3")
            self.rows.addWidget(title)
            for folder, group in groups:
                if folder is not None and len(groups) > 1:  # one Live folder needs no heading
                    heading = QLabel(f"●  {folder.name}")
                    heading.setStyleSheet(f"color: {folder.colour}; font-weight: bold; margin-top: 4px;")
                    self.rows.addWidget(heading)
                self._add_grid(group)
        self._add_state_rows(collection)

    def _add_state_rows(self, collection: SceneCollection) -> None:
        """One row per sprig with Live states: a button per state, plus "As built"."""
        parts = [p for p in collection.parts() if p.states]
        if not parts:
            return
        title = QLabel(f"{cap(PARTS)}")
        title.setStyleSheet("color: #8cc4a3; margin-top: 6px;")
        self.rows.addWidget(title)
        for part in parts:
            row_box = QWidget()
            row = QHBoxLayout(row_box)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            name = QLabel(f"{PART_MARK} {part.name}")
            name.setMinimumWidth(110)
            row.addWidget(name)
            for state in [None, *(s.name for s in part.states)]:
                button = QPushButton("As built" if state is None else state)
                button.setCheckable(True)
                button.setMinimumHeight(36)
                button.setToolTip("Switches with a quick fade, on every bloom that uses it")
                button.clicked.connect(lambda _=False, pid=part.id, st=state: self.on_state(pid, st))
                self.state_buttons[(part.id, state)] = button
                row.addWidget(button, 1)
            self.rows.addWidget(row_box)

    def _add_grid(self, scenes) -> None:
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
        self.clock.setStyleSheet("color: #ff5fa2" if live else "")

    def set_recording(self, text: str) -> None:
        self.rec.setText(text)
