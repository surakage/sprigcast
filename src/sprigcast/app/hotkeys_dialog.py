"""Hotkeys page: assign a key combo to scenes, Go live and recording."""

from __future__ import annotations

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QGridLayout,
    QKeySequenceEdit,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app.hotkeys import actions, to_pynput
from sprigcast.core.scene import SceneCollection


class HotkeysPage(QWidget):
    def __init__(self, collection: SceneCollection, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        intro = QLabel(
            "Click a box and press the keys, for example Ctrl+Alt+1. Hotkeys work while a game has focus. "
            "Pick combos your games don't use."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        self.edits: dict[str, QKeySequenceEdit] = {}
        for row, (action, label) in enumerate(actions(collection)):
            grid.addWidget(QLabel(label), row, 0)
            edit = QKeySequenceEdit(QKeySequence(collection.hotkeys.get(action, "")))
            edit.setMaximumSequenceLength(1)
            grid.addWidget(edit, row, 1)
            clear = QPushButton("Clear")
            clear.clicked.connect(edit.clear)
            grid.addWidget(clear, row, 2)
            self.edits[action] = edit
        grid.setRowStretch(grid.rowCount(), 1)  # keep rows together at the top
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(grid_widget)
        layout.addWidget(scroll, 1)

    def result_hotkeys(self) -> dict[str, str]:
        out = {}
        for action, edit in self.edits.items():
            combo = edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            if combo:
                out[action] = combo
        return out

    def problem(self) -> str | None:
        """Why the chosen hotkeys can't be saved, or None when they're fine."""
        seen: dict[str, str] = {}
        for action, combo in self.result_hotkeys().items():
            if to_pynput(combo) is None:
                return f"{combo} can't be used as a hotkey. Add a letter, number or F-key."
            if combo in seen:
                return f"{combo} is used twice."
            seen[combo] = action
        return None
