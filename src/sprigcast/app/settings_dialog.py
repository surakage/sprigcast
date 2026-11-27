"""A settings form built from a source's `defaults`, used by the text source and by plugins.

str → text box (with a colour picker for keys ending in "color", Browse… for "file"/"path"),
int / float → number box, bool → checkbox.

A source class can also give:
    choices = {"language": (("en", "English"), ("es", "Spanish"))}  # a drop-down instead of a text box
              (or a function returning that list, read each time the form opens: devices plugged in now;
              a function taking one argument gets the garden, to list its sounds)
    secrets = {"api_key": "myplugin/api_key"}  # a hidden box whose value is kept DPAPI-encrypted in
              SprigCast's settings under that name, never in the garden file (so it isn't shared or exported);
    secrets_after = "language"  # where those boxes go in the form (at the end without it)
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.card import BOX_HEIGHT, CardDialog, combo, field_label
from sprigcast.app.card import section as card_section
from sprigcast.core import style

log = logging.getLogger(__name__)


PICTURE_FILES = "Pictures (*.png *.gif *.webp *.apng)"

LABELS = {
    "url": "URL",
    "file": "Local file",
    "fps": "Frames per second",
    "css": "Custom CSS",
    "mute": "Mute page sound",
    "audio_to_mixer": "Sound in the Sound card",
    "keep_running": "Keep running when hidden",
    "hide_after": "Hide after (seconds)",
    "platform_marks": "Platform colors",
    "chat_look": "Look",
    "text_color": "Text color",
    "card_color": "Card color",
    "hour_24": "24-hour clock",
    "time_zone": "Show time zone",
    "bubble_color": "Bubble color",
    "background_color": "Box color",
    "path": "File",
    "restart_on_show": "Restart when shown",
    "clear_on_end": "Show nothing when it ends",
    "play_on_start": "Play when SprigCast opens",
    "save_polaroid_copy": "Save a polaroid copy",
    "freeze_screen": "Freeze the screen while it prints",
    "listen_to": "Mic",
    "color": "Color",
    "peak_color": "Peak color",
    "talk_at": "Talks above (dB)",
    "yell_at": "Yells above (dB)",
    "hold": "Hold (seconds)",
    "idle_picture": "Idle picture",
    "talk_picture": "Talk picture",
    "look": "Style",
    "text_size": "Size",
    "all_caps": "All caps",
    "align": "Alignment",
    "outline_color": "Outline color",
    "capture": "Replay",
    "corner": "In the corner",
    "corner_at": "Corner",
    "corner_size": "Corner size (%)",
    "replay_sound": "Replay sound",
    "sound_level": "Sound level (%)",
    "accent_color": "Accent color",
    "font": "Font",
    "text_scale": "Text size",
    "corners": "Corners",
    "custom_look": "Custom look",
    "show_dot": "Show dot",
    "show_date": "Show date",
    "show_label": "Show label",
    "show_art": "Show cover",
    "show_album": "Show album",
    "show_time": "Show time",
}


def _label(key: str) -> str:
    return LABELS.get(key) or key.replace("_", " ").capitalize()


FIELD_RADIUS = 16
FORM_STYLE = (
    f"QLineEdit, QSpinBox, QDoubleSpinBox {{ background: {theme.CHROME}; color: {theme.TEXT};"
    f" border: 1px solid {theme.EDGE_STRONG}; border-radius: {FIELD_RADIUS}px; padding: 6px 14px; min-height: 22px; }}"
    f"QPlainTextEdit {{ background: {theme.CHROME}; color: {theme.TEXT}; border: 1px solid {theme.EDGE_STRONG};"
    " border-radius: 14px; padding: 6px 8px; }"
    f"QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{ border-color: {theme.ACCENT}; }}"
    "QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button"
    " { width: 0px; border: none; }"  # type a number or use the wheel: the tiny square arrows broke the round end
    "#formTabs::pane { border: none; top: 0px; }"
    "#formTabs > QTabBar::tab { margin: 0 6px 10px 0; }"
)


def _form(parent: QWidget | None = None) -> QFormLayout:
    form = QFormLayout(parent) if parent is not None else QFormLayout()
    form.setContentsMargins(0, 0, 0, 0)
    form.setHorizontalSpacing(16)
    form.setVerticalSpacing(12)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return form


def _paged() -> QWidget:
    """A tab's page: its fields on one section card."""
    page = QWidget()
    holder = QVBoxLayout(page)
    holder.setContentsMargins(0, 0, 0, 0)
    holder.addWidget(card_section()[0])
    holder.addStretch(1)
    return page


def _name(text: str) -> QLabel:
    """A field's name, level with the middle of a one-line box (and the first line of a bigger one)."""
    label = field_label(text)
    label.setStyleSheet(label.styleSheet() + " padding-top: 9px;")
    label.setMinimumHeight(BOX_HEIGHT)
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    return label


class SettingsDialog(CardDialog):
    def __init__(self, title: str, defaults: dict[str, Any], settings: dict[str, Any], parent: QWidget | None = None,
                 cls: type | None = None, collection: Any = None):
        super().__init__(title, parent, width=560)
        self.setStyleSheet(FORM_STYLE)
        self.collection = collection
        self.choices: dict[str, Any] = dict(getattr(cls, "choices", None) or {})
        self.secrets: dict[str, str] = dict(getattr(cls, "secrets", None) or {})
        self.placeholders: dict[str, str] = dict(getattr(cls, "placeholders", None) or {})  # shown when empty
        self.secret_fields: dict[str, QLineEdit] = {}
        secrets_after = getattr(cls, "secrets_after", None)  # the field the secrets follow (else at the end)
        hides = set(getattr(cls, "form_hides", None) or ()) - set(getattr(cls, "form_shows", None) or ())
        defaults = {k: v for k, v in defaults.items() if k not in hides}  # drawn from their defaults (text plugins)
        self.defaults = defaults
        self._opened_with = {k: settings.get(k, v) for k, v in defaults.items()}  # for the log line on OK
        self.fields: dict[str, Any] = {}
        self.look_note: QLabel | None = None
        styled = [k for k in style.style_keys(defaults) if k not in self.secrets]
        if len(styled) < 2 or len(styled) == len(defaults):  # a colour or two alone stays on one page
            styled = []
        outer = self.body
        if styled:  # each page's fields on a section card of their own (user 2026-11-25: no flat areas)
            tabs = QTabWidget()
            tabs.setObjectName("formTabs")
            page, style_page = _paged(), _paged()
            form, style_form = _form(), _form()
            page.layout().itemAt(0).widget().layout().addLayout(form)
            style_page.layout().itemAt(0).widget().layout().addLayout(style_form)
            tabs.addTab(page, "Settings")
            tabs.addTab(style_page, "Style")
            outer.addWidget(tabs)
        else:
            form = _form()
            style_form = form
            self.section().addLayout(form)
        for key, default in defaults.items():
            value = settings.get(key, default)
            widget = self._field(key, default, value)
            (style_form if key in styled else form).addRow(_name(_label(key)), widget)
            if key == "custom_look":
                self.look_note = QLabel()
                self.look_note.setWordWrap(True)
                self.look_note.setStyleSheet(f"color: {theme.MUTED}")
                keep = self.look_note.sizePolicy()  # its room stays when it's empty: the dialog doesn't resize as
                keep.setRetainSizeWhenHidden(True)  # you type (Qt couldn't fit the new size on the PC, 2026-11-18)
                self.look_note.setSizePolicy(keep)
                self.look_note.setMinimumHeight(self.look_note.fontMetrics().lineSpacing() * 2)
                style_form.addRow("", self.look_note)
                widget.textChanged.connect(self._check_look)
                self._check_look()
            if key == secrets_after:
                self._add_secrets(form)
        if secrets_after not in defaults:
            self._add_secrets(form)
        if styled:
            reset = QPushButton("Reset style")
            reset.setStyleSheet(theme.button_style(16, "7px 16px"))
            reset.setAutoDefault(False)
            reset.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)  # Enter never resets the style: it means OK
            reset.clicked.connect(lambda: self._reset_style(styled))
            style_form.addRow("", reset)
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
                         "messages up. A color ending in 00 is see-through.")
            tip.setWordWrap(True)
            tip.setStyleSheet(f"color: {theme.MUTED}")
            form.addRow("", tip)
        self.ok = self.add_buttons("Save")
        text = self.fields.get("text")
        if isinstance(text, (QLineEdit, QPlainTextEdit)):  # a text source: type straight away
            text.selectAll()
            text.setFocus()

    def _check_look(self) -> None:
        """Name the custom look lines that are skipped (they fall back to the plugin's own look)."""
        edit = self.fields.get("custom_look")
        if self.look_note is None or edit is None:
            return
        problems = style.look_problems(edit.toPlainText())
        self.look_note.setText("Skipped: " + "; ".join(problems) if problems else "")
        self.look_note.setVisible(bool(problems))

    def _reset_style(self, keys: list[str]) -> None:
        for key in keys:
            self._set(key, self.defaults[key])

    def _set(self, key: str, value: Any) -> None:
        widget = self.fields[key]
        if isinstance(widget, QComboBox):
            index = widget.findData(value)
            if index >= 0:
                widget.setCurrentIndex(index)
            elif widget.isEditable():
                widget.setEditText(str(value))
        elif isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            widget.setValue(value)
        elif isinstance(widget, QPlainTextEdit):
            widget.setPlainText(str(value))
        else:
            widget.setText(str(value))

    def _add_secrets(self, form: QFormLayout) -> None:
        for key in self.secrets:
            edit = QLineEdit(read_secret(self.secrets[key]))
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.secret_fields[key] = edit
            form.addRow(_name(_label(key)), edit)

    def _field(self, key: str, default: Any, value: Any) -> QWidget:
        if key == "corners" and key not in self.choices:
            self.choices[key] = style.CORNERS
        if key == "font" and isinstance(default, str) and not default.lower().endswith((".ttf", ".otf")):
            box = combo()  # a short list, typed names work too (a full font list is slow to open)
            box.setEditable(True)
            box.addItem("SprigCast's own", "")
            for family in style.FONTS[1:]:
                box.addItem(family, family)
            index = box.findData(value)
            if index >= 0:
                box.setCurrentIndex(index)
            else:
                box.setEditText(str(value))
            self.fields[key] = box
            return box
        if key == "text_scale":
            box = QSpinBox()
            box.setRange(50, 200)
            box.setSingleStep(10)
            box.setSuffix(" %")
            box.setValue(min(200, max(50, int(value or 100))))
            self.fields[key] = box
            return box
        if key in self.choices:
            options = self.choices[key]
            try:
                options = list(_list_of(options, self.collection) if callable(options) else options)
            except Exception:
                options = []
            box = combo()
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
            box.setMinimumHeight(BOX_HEIGHT)  # level with its name
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
        if key in ("text", "css", "custom_look"):
            edit = QPlainTextEdit(str(value))
            edit.setFixedHeight(110 if key == "custom_look" else 80)
            if key == "custom_look":
                edit.setTabChangesFocus(True)  # Tab goes on to the next box, as everywhere else in the form
                edit.setPlaceholderText("text: #fff8e7\ncard: #10201880\ncorners: leaf\nhide: date")
            self.fields[key] = edit
            return edit
        edit = QLineEdit(str(value))
        if key in self.placeholders:
            edit.setPlaceholderText(self.placeholders[key])
        self.fields[key] = edit
        if key.endswith("color"):
            return self._with_button(edit, "Pick…", lambda: self._pick_color(edit))
        if key in ("file", "path"):
            return self._with_button(edit, "Browse…", lambda: self._pick_file(edit))
        if key.endswith("_picture"):
            return self._with_button(edit, "Browse…", lambda: self._pick_file(edit, PICTURE_FILES))
        if key == "folder":
            return self._with_button(edit, "Browse…", lambda: self._pick_folder(edit))
        return edit

    @staticmethod
    def _with_button(edit: QLineEdit, text: str, slot) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(edit, 1)
        button = QPushButton(text)
        button.setStyleSheet(theme.button_style(16, "7px 14px"))
        button.setAutoDefault(False)  # Enter means OK, not Pick… or Browse…
        button.clicked.connect(slot)
        layout.addWidget(button)
        return row

    def _pick_color(self, edit: QLineEdit) -> None:
        current = QColor(edit.text()[:7]) if edit.text().startswith("#") else QColor("white")
        color = QColorDialog.getColor(current, self, "Color", QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if color.isValid():
            edit.setText("#{:02x}{:02x}{:02x}{:02x}".format(color.red(), color.green(), color.blue(), color.alpha()))

    def _pick_file(self, edit: QLineEdit, types: str = "") -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose file", edit.text(), types)
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
        changed = sorted(k for k, v in self.values().items() if v != self._opened_with.get(k))
        log.info("settings %r: OK, changed %s", self.windowTitle(), ", ".join(changed) or "nothing")
        super().accept()

    def values(self) -> dict[str, Any]:
        out = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QComboBox) and widget.isEditable():
                typed = widget.currentText().strip()
                index = widget.findText(typed)
                out[key] = widget.itemData(index) if index >= 0 else typed
            elif isinstance(widget, QComboBox):
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


def _list_of(fn, collection) -> Any:
    """A choices function's list: given the garden when it takes an argument."""
    import inspect

    try:
        takes = bool(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        takes = False
    return fn(collection) if takes else fn()


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
