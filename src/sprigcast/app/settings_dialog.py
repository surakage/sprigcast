"""A settings form built from a source's `defaults`, used by the text source and by plugins.

str → text box (with a colour picker for keys ending in "color", Browse… for "file"/"path"),
int / float → number box, bool → checkbox.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from sprigcast.app import theme


LABELS = {
    "url": "URL",
    "file": "Local file",
    "fps": "Frames per second",
    "css": "Custom CSS",
    "mute": "Mute page sound",
    "audio_to_mixer": "Sound in the Sound card",
    "keep_running": "Keep running when hidden",
}


def _label(key: str) -> str:
    return LABELS.get(key) or key.replace("_", " ").capitalize()


class SettingsDialog(QDialog):
    def __init__(self, title: str, defaults: dict[str, Any], settings: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.defaults = defaults
        self.fields: dict[str, Any] = {}
        form = QFormLayout(self)
        for key, default in defaults.items():
            value = settings.get(key, default)
            widget = self._field(key, default, value)
            form.addRow(_label(key), widget)
        if "text" in defaults:
            tip = QLabel("{time}, {time12} and {date} show a live clock. A file's text replaces Text when set.")
            tip.setWordWrap(True)
            tip.setStyleSheet(f"color: {theme.MUTED}")
            form.addRow("", tip)
        if "css" in defaults and "url" in defaults:
            tip = QLabel("A local file is used instead of the URL when set. With Sound in the Sound card, the page's "
                         "sound gets its own volume, mute and ducking there; off, it plays on your desktop audio. "
                         "Keep running when hidden keeps alerts connected while another bloom is on.")
            tip.setWordWrap(True)
            tip.setStyleSheet(f"color: {theme.MUTED}")
            form.addRow("", tip)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _field(self, key: str, default: Any, value: Any) -> QWidget:
        if isinstance(default, bool):
            box = QCheckBox()
            box.setChecked(bool(value))
            self.fields[key] = box
            return box
        if isinstance(default, int):
            box = QSpinBox()
            box.setRange(-100000, 100000)
            box.setValue(int(value))
            self.fields[key] = box
            return box
        if isinstance(default, float):
            box = QDoubleSpinBox()
            box.setRange(-100000.0, 100000.0)
            box.setDecimals(2)
            box.setValue(float(value))
            self.fields[key] = box
            return box
        if key in ("text", "css"):
            edit = QPlainTextEdit(str(value))
            edit.setFixedHeight(80)
            self.fields[key] = edit
            return edit
        edit = QLineEdit(str(value))
        self.fields[key] = edit
        if key.endswith("color"):
            return self._with_button(edit, "Pick…", lambda: self._pick_color(edit))
        if key in ("file", "path"):
            return self._with_button(edit, "Browse…", lambda: self._pick_file(edit))
        return edit

    @staticmethod
    def _with_button(edit: QLineEdit, text: str, slot) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit, 1)
        button = QPushButton(text)
        button.clicked.connect(slot)
        layout.addWidget(button)
        return row

    def _pick_color(self, edit: QLineEdit) -> None:
        current = QColor(edit.text()[:7]) if edit.text().startswith("#") else QColor("white")
        color = QColorDialog.getColor(current, self, "Color", QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if color.isValid():
            edit.setText("#{:02x}{:02x}{:02x}{:02x}".format(color.red(), color.green(), color.blue(), color.alpha()))

    def _pick_file(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose file", edit.text())
        if path:
            edit.setText(path)

    def values(self) -> dict[str, Any]:
        out = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QCheckBox):
                out[key] = widget.isChecked()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                out[key] = widget.value()
            elif isinstance(widget, QPlainTextEdit):
                out[key] = widget.toPlainText()
            else:
                out[key] = widget.text()
        return out
