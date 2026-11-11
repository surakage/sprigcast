"""Assign a key combo to scenes, Go live and recording."""

from __future__ import annotations

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QKeySequenceEdit,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app.hotkeys import actions, to_pynput
from sprigcast.core.scene import SceneCollection


class HotkeysDialog(QDialog):
    def __init__(self, collection: SceneCollection, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Hotkeys")
        self.setMinimumSize(560, 420)
        layout = QVBoxLayout(self)
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
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(grid_widget)
        layout.addWidget(scroll, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_hotkeys(self) -> dict[str, str]:
        out = {}
        for action, edit in self.edits.items():
            combo = edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            if combo:
                out[action] = combo
        return out

    def _accept(self) -> None:
        chosen = self.result_hotkeys()
        seen: dict[str, str] = {}
        for action, combo in chosen.items():
            if to_pynput(combo) is None:
                QMessageBox.warning(self, "Hotkeys", f"{combo} can't be used as a hotkey. Add a letter, number or F-key.")
                return
            if combo in seen:
                QMessageBox.warning(self, "Hotkeys", f"{combo} is used twice.")
                return
            seen[combo] = action
        self.accept()
