"""A settings form built from a source's `defaults`, used by the text source and by plugins.

str → text box (with a colour picker for keys ending in "color", Browse… for "file"/"path"),
int / float → number box, bool → checkbox.

A source class can also give:
    choices = {"language": (("en", "English"), ("es", "Spanish"))}  # a drop-down instead of a text box
              (or a function returning that list, read each time the form opens: devices plugged in now)
    secrets = {"google_key": "captions/google_key"}  # a hidden box whose value is kept DPAPI-encrypted in
              SprigCast's settings under that name, never in the garden file (so it isn't shared or exported);
    secrets_after = "engine"  # where those boxes go in the form (at the end without it)
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
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
    "hide_after": "Hide after (seconds)",
    "platform_marks": "Platform colours",
    "text_color": "Text colour",
    "card_color": "Card colour",
    "hour_24": "24-hour clock",
    "time_zone": "Show time zone",
    "bubble_color": "Bubble colour",
    "background_color": "Box colour",
    "path": "File",
    "restart_on_show": "Restart when shown",
    "clear_on_end": "Show nothing when it ends",
    "play_on_start": "Play when SprigCast opens",
    "save_polaroid_copy": "Save a polaroid copy",
    "listen_to": "Mic",
    "engine": "Captions by",
    "google_key": "Google key",
    "lines": "Lines",
    "color": "Colour",
    "peak_color": "Peak colour",
    "look": "Style",
    "text_size": "Size",
    "all_caps": "All caps",
    "align": "Alignment",
    "outline_color": "Outline colour",
}


def _label(key: str) -> str:
    return LABELS.get(key) or key.replace("_", " ").capitalize()


class SettingsDialog(QDialog):
    def __init__(self, title: str, defaults: dict[str, Any], settings: dict[str, Any], parent: QWidget | None = None,
                 cls: type | None = None):
        super().__init__(parent)
        self.choices: dict[str, Any] = dict(getattr(cls, "choices", None) or {})
        self.secrets: dict[str, str] = dict(getattr(cls, "secrets", None) or {})
        self.secret_fields: dict[str, QLineEdit] = {}
        secrets_after = getattr(cls, "secrets_after", None)  # the field the secrets follow (else at the end)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        self.defaults = defaults
        self.fields: dict[str, Any] = {}
        form = QFormLayout(self)
        for key, default in defaults.items():
            value = settings.get(key, default)
            widget = self._field(key, default, value)
            form.addRow(_label(key), widget)
            if key == secrets_after:
                self._add_secrets(form)
        if secrets_after not in defaults:
            self._add_secrets(form)
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
        if "hide_after" in defaults and "bubble_color" in defaults:  # the chat source
            tip = QLabel("Shows the chats you picked in the Chat panel (⋯ → Channels…). Hide after 0 keeps "
                         "messages up. A colour ending in 00 is see-through.")
            tip.setWordWrap(True)
            tip.setStyleSheet(f"color: {theme.MUTED}")
            form.addRow("", tip)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _add_secrets(self, form: QFormLayout) -> None:
        for key in self.secrets:
            edit = QLineEdit(read_secret(self.secrets[key]))
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.secret_fields[key] = edit
            form.addRow(_label(key), edit)

    def _field(self, key: str, default: Any, value: Any) -> QWidget:
        if key in self.choices:
            options = self.choices[key]
            try:
                options = list(options() if callable(options) else options)
            except Exception:
                options = []
            box = QComboBox()
            for data, label in options:
                box.addItem(str(label), data)
            index = box.findData(value)
            if index < 0 and value not in (None, ""):  # a device that isn't plugged in now stays picked
                box.addItem(str(value), value)
                index = box.count() - 1
            box.setCurrentIndex(max(0, index))
            self.fields[key] = box
            return box
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
        if key == "folder":
            return self._with_button(edit, "Browse…", lambda: self._pick_folder(edit))
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

    def _pick_folder(self, edit: QLineEdit) -> None:
        from PySide6.QtWidgets import QFileDialog

        path = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text())
        if path:
            edit.setText(path)

    def accept(self) -> None:
        for key, edit in self.secret_fields.items():
            save_secret(self.secrets[key], edit.text().strip())
        super().accept()

    def values(self) -> dict[str, Any]:
        out = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QComboBox):
                out[key] = widget.currentData()
            elif isinstance(widget, QCheckBox):
                out[key] = widget.isChecked()
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                out[key] = widget.value()
            elif isinstance(widget, QPlainTextEdit):
                out[key] = widget.toPlainText()
            else:
                out[key] = widget.text()
        return out


def read_secret(name: str) -> str:
    """A secret a plugin keeps in SprigCast's settings (see `secrets` above), decrypted; "" if none."""
    from PySide6.QtCore import QSettings

    from sprigcast.control import secret_store

    saved = QSettings().value(name, "")
    return secret_store.unprotect(saved) if isinstance(saved, str) and saved else ""


def save_secret(name: str, value: str) -> None:
    from PySide6.QtCore import QSettings

    from sprigcast.control import secret_store

    if value:
        QSettings().setValue(name, secret_store.protect(value))
    else:
        QSettings().remove(name)
