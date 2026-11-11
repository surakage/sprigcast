"""The New part dialog: name, shape and whether to drop it into the show you're working on, in one step."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.core.scene import Scene, SceneCollection

EXAMPLES = "Webcam frame, Alerts box, Brand bar, Chat panel"


class NewPartDialog(QDialog):
    def __init__(self, collection: SceneCollection, show: Scene | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("New part")
        intro = QLabel(
            "A part is a reusable piece, like a webcam frame or a brand bar. Build it once, then add it "
            "to any show on either canvas. Change it, and every show using it changes too."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #8d8a83")
        self.name = QLineEdit()
        self.name.setPlaceholderText(EXAMPLES)
        shapes = QWidget()
        row = QHBoxLayout(shapes)
        row.setContentsMargins(0, 0, 0, 0)
        self.shape_group = QButtonGroup(self)
        default = show.canvas if show is not None else collection.canvases[0].id
        for canvas in collection.canvases:
            button = QRadioButton(f"{canvas.id.capitalize()}  {canvas.width}×{canvas.height}")
            button.setProperty("canvas", canvas.id)
            button.setChecked(canvas.id == default)
            self.shape_group.addButton(button)
            row.addWidget(button)
        row.addStretch(1)
        shape_hint = QLabel("You can add the other shape later from the part editor.")
        shape_hint.setStyleSheet("color: #8d8a83")
        self.add_to_show = QCheckBox(f"Add it to “{show.name}” now" if show is not None else "")
        self.add_to_show.setChecked(show is not None)
        self.add_to_show.setVisible(show is not None)
        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Shape", shapes)
        form.addRow("", shape_hint)
        form.addRow("", self.add_to_show)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Create part")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.name.textChanged.connect(lambda text: self.ok.setEnabled(bool(text.strip())))
        self.ok.setEnabled(False)
        root = QVBoxLayout(self)
        root.addWidget(intro)
        root.addLayout(form)
        root.addWidget(buttons)
        self.resize(460, self.sizeHint().height())

    def values(self) -> tuple[str, str, bool]:
        """(name, canvas id, add to the show)."""
        checked = self.shape_group.checkedButton()
        return self.name.text().strip(), str(checked.property("canvas")), self.add_to_show.isChecked()

    @staticmethod
    def ask(collection: SceneCollection, show: Scene | None, parent: QWidget | None = None) -> tuple[str, str, bool] | None:
        dialog = NewPartDialog(collection, show, parent)
        if not dialog.exec():
            return None
        name, canvas_id, add = dialog.values()
        return (name, canvas_id, add) if name else None
