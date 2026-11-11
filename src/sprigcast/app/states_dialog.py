"""The Live states window for a sprig: named looks, like "Alert showing", each showing some of its items."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app.terms import PART
from sprigcast.core.scene import Scene, SceneCollection, SprigState


class StatesDialog(QDialog):
    def __init__(self, collection: SceneCollection, part: Scene, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Live states · {part.name}")
        self.states = [s.model_copy(deep=True) for s in part.states]
        # Every item in any layout of the sprig, top-most first.
        self.items: list[tuple[str, str]] = []
        for shape in collection.shapes(part):
            for item in reversed(shape.items):
                if all(sid != item.source for sid, _ in self.items):
                    self.items.append((item.source, collection.source(item.source).name))

        intro = QLabel(f"Looks for this {PART} that you switch between in Live, with a quick fade. "
                       "Tick what each state shows.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #8cc4a3")
        self.state_list = QListWidget()
        self.state_list.currentRowChanged.connect(self._show_state)
        add = QPushButton("+ State")
        add.clicked.connect(self._add)
        rename = QPushButton("Rename")
        rename.clicked.connect(self._rename)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove)
        left = QVBoxLayout()
        left.addWidget(QLabel("States"))
        left.addWidget(self.state_list, 1)
        row = QHBoxLayout()
        for b in (add, rename, remove):
            row.addWidget(b)
        left.addLayout(row)
        self.item_list = QListWidget()
        self.item_list.itemChanged.connect(self._item_ticked)
        right = QVBoxLayout()
        self.shows_label = QLabel("Shows")
        right.addWidget(self.shows_label)
        right.addWidget(self.item_list, 1)
        body = QHBoxLayout()
        body.addLayout(left, 1)
        body.addLayout(right, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.addWidget(intro)
        root.addLayout(body, 1)
        root.addWidget(buttons)
        self.resize(560, 380)
        self._fill_states()

    def _fill_states(self, select: int = 0) -> None:
        self.state_list.clear()
        for state in self.states:
            self.state_list.addItem(state.name)
        if self.states:
            self.state_list.setCurrentRow(min(select, len(self.states) - 1))
        else:
            self._show_state(-1)

    def _show_state(self, row: int) -> None:
        self.item_list.blockSignals(True)
        self.item_list.clear()
        state = self.states[row] if 0 <= row < len(self.states) else None
        self.item_list.setEnabled(state is not None)
        self.shows_label.setText(f"“{state.name}” shows" if state else "Add a state with + State")
        for source_id, name in self.items:
            entry = QListWidgetItem(name)
            entry.setData(Qt.ItemDataRole.UserRole, source_id)
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            hidden = state is not None and source_id in state.hide
            entry.setCheckState(Qt.CheckState.Unchecked if hidden else Qt.CheckState.Checked)
            self.item_list.addItem(entry)
        self.item_list.blockSignals(False)

    def _item_ticked(self, entry: QListWidgetItem) -> None:
        row = self.state_list.currentRow()
        if not 0 <= row < len(self.states):
            return
        state = self.states[row]
        source_id = entry.data(Qt.ItemDataRole.UserRole)
        hide = set(state.hide)
        if entry.checkState() == Qt.CheckState.Checked:
            hide.discard(source_id)
        else:
            hide.add(source_id)
        state.hide = [sid for sid, _ in self.items if sid in hide]

    def _ask_name(self, title: str, text: str = "") -> str | None:
        name, ok = QInputDialog.getText(self, title, "Name (e.g. Alert showing):", text=text)
        name = name.strip()
        if not ok or not name:
            return None
        if any(s.name == name for s in self.states if s.name != text):
            return None  # names must be unique: Live and hotkeys find states by name
        return name

    def _add(self) -> None:
        name = self._ask_name("New state")
        if name:
            # Start from the current state's look, so a small change is a small edit.
            row = self.state_list.currentRow()
            hide = list(self.states[row].hide) if 0 <= row < len(self.states) else []
            self.states.append(SprigState(name=name, hide=hide))
            self._fill_states(len(self.states) - 1)

    def _rename(self) -> None:
        row = self.state_list.currentRow()
        if 0 <= row < len(self.states):
            name = self._ask_name("Rename state", self.states[row].name)
            if name:
                self.states[row].name = name
                self._fill_states(row)

    def _remove(self) -> None:
        row = self.state_list.currentRow()
        if 0 <= row < len(self.states):
            del self.states[row]
            self._fill_states(row)

    def apply_to(self, part: Scene) -> None:
        """Save the states on the sprig. Its current state survives if it still exists."""
        current = part.state
        part.states = self.states
        if current is not None and all(s.name != current for s in self.states):
            part.state = None
