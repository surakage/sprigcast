"""The Filters window for one source: add, remove, reorder, switch on/off and tune its filters."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.core.filters import Field, filter_kind, filter_kinds, settings_for
from sprigcast.core.scene import Filter, SourceConfig

AUDIO_KINDS = {"audio_input", "audio_output"}


def media_of(source: SourceConfig) -> str:
    return "audio" if source.kind in AUDIO_KINDS else "video"


class FilterForm(QWidget):
    """Sliders, checkboxes and color buttons for one filter's fields. Emits `changed` on every edit."""

    changed = Signal()

    def __init__(self, fields: list[Field], values: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.fields = fields
        self.getters: dict[str, Callable[[], object]] = {}
        self.setters: dict[str, Callable[[object], None]] = {}
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for f in fields:
            form.addRow(f.label, self._widget(f, values.get(f.key, f.default)))

    def _widget(self, f: Field, value) -> QWidget:
        kind = f.kind
        if kind == "bool":
            box = QCheckBox()
            box.setChecked(bool(value))
            box.toggled.connect(self.changed)
            self.getters[f.key], self.setters[f.key] = box.isChecked, lambda v: box.setChecked(bool(v))
            return box
        if kind == "choice":
            combo = QComboBox()
            for data, label in f.choices:
                combo.addItem(label, data)
            combo.setCurrentIndex(max(0, combo.findData(value)))
            combo.currentIndexChanged.connect(self.changed)
            self.getters[f.key] = combo.currentData
            self.setters[f.key] = lambda v: combo.setCurrentIndex(max(0, combo.findData(v)))
            return combo
        if kind == "color":
            button = QPushButton()
            button.setMinimumWidth(120)

            def show(text: str) -> None:
                button.setProperty("color", text)
                button.setText(text)
                fg = "#000" if QColor(text[:7]).lightness() > 128 else "#fff"
                button.setStyleSheet(f"background: {text[:7]}; color: {fg}; padding: 4px;")

            def pick() -> None:
                color = QColorDialog.getColor(QColor(str(button.property("color"))[:7]), self, f.label)
                if color.isValid():
                    show(color.name())
                    self.changed.emit()

            show(str(value))
            button.clicked.connect(pick)
            self.getters[f.key], self.setters[f.key] = (lambda: str(button.property("color"))), lambda v: show(str(v))
            return button
        if kind in ("int", "float"):
            return self._slider(f, value)
        from PySide6.QtWidgets import QLineEdit

        edit = QLineEdit(str(value))
        edit.textChanged.connect(self.changed)
        self.getters[f.key], self.setters[f.key] = edit.text, lambda v: edit.setText(str(v))
        return edit

    def _slider(self, f: Field, value) -> QWidget:
        is_int = f.kind == "int"
        scale = 1 if is_int else round(1 / f.step) if f.step < 1 else 1
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(round(f.minimum * scale), round(f.maximum * scale))
        spin: QSpinBox | QDoubleSpinBox
        if is_int:
            spin = QSpinBox()
            spin.setRange(int(f.minimum), int(f.maximum))
        else:
            spin = QDoubleSpinBox()
            spin.setRange(f.minimum, f.maximum)
            spin.setDecimals(2 if f.step < 0.1 else 1 if f.step < 1 else 0)
        spin.setSingleStep(f.step)
        spin.setSuffix(f.suffix)
        spin.setMinimumWidth(100)
        spin.setValue(value)
        slider.setValue(round(float(value) * scale))
        slider.valueChanged.connect(lambda v: spin.setValue(v if is_int else v / scale))

        def from_spin(v) -> None:
            slider.blockSignals(True)
            slider.setValue(round(v * scale))
            slider.blockSignals(False)
            self.changed.emit()

        spin.valueChanged.connect(from_spin)
        layout.addWidget(slider, 1)
        layout.addWidget(spin)
        self.getters[f.key] = spin.value
        self.setters[f.key] = lambda v: spin.setValue(v)
        return row

    def values(self) -> dict:
        return {key: get() for key, get in self.getters.items()}

    def set_values(self, values: dict) -> None:
        for key, value in values.items():
            if key in self.setters:
                self.setters[key](value)


class FiltersDialog(QDialog):
    """Edits `source.filters` in place and calls on_change(save=False) after every edit, so the
    preview updates live. Closing calls on_change(save=True)."""

    def __init__(self, source: SourceConfig, on_change: Callable[..., None], parent: QWidget | None = None,
                 media: str | None = None):
        super().__init__(parent)
        self.source = source
        self.on_change = on_change
        self.media = media or media_of(source)  # a browser page has both: the Sound card asks for "audio"
        self.setWindowTitle(f"Filters for “{source.name}”")
        self.resize(780, 460)
        self.form: FilterForm | None = None

        self.list = QListWidget()
        self.list.setMinimumWidth(220)
        self.list.currentRowChanged.connect(self._show_selected)
        self.list.itemChanged.connect(self._toggled)
        self.add_menu = QMenu(self)
        self.add_menu.aboutToShow.connect(self._fill_add_menu)
        bar = QHBoxLayout()
        bar.setSpacing(2)
        for text, tip, target in (
            ("+", "Add a filter", self.add_menu),
            ("−", "Remove the selected filter", self._remove),
            ("▲", "Move up", lambda: self._move(-1)),
            ("▼", "Move down", lambda: self._move(+1)),
        ):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.setMinimumSize(30, 28)
            if isinstance(target, QMenu):
                b.setMenu(target)
                b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            else:
                b.clicked.connect(target)
            bar.addWidget(b)
        bar.addStretch(1)
        left = QVBoxLayout()
        title = QLabel("Audio filters" if self.media == "audio" else "Effect filters")
        title.setStyleSheet("font-weight: bold")
        left.addWidget(title)
        left.addWidget(self.list, 1)
        left.addLayout(bar)

        self.title = QLabel("")
        self.title.setStyleSheet("font-weight: bold")
        self.form_area = QScrollArea()
        self.form_area.setWidgetResizable(True)
        self.form_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.reset_button = QPushButton("Defaults")
        self.reset_button.setToolTip("Put this filter's settings back to their defaults")
        self.reset_button.clicked.connect(self._reset)
        hint = QLabel(
            "Audio filters run top to bottom." if self.media == "audio"
            else "Effects run on the GPU in a fixed order: scroll, sharpen, key, then color correction."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED}")
        right = QVBoxLayout()
        right.addWidget(self.title)
        right.addWidget(self.form_area, 1)
        right.addWidget(hint)
        row = QHBoxLayout()
        row.addWidget(self.reset_button)
        row.addStretch(1)
        right.addLayout(row)

        body = QHBoxLayout()
        body.addLayout(left)
        body.addLayout(right, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.addLayout(body, 1)
        root.addWidget(buttons)
        self._refresh(0)

    # List ---------------------------------------------------------------------

    def _refresh(self, select: int | None = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for flt in self.source.filters:
            item = QListWidgetItem(flt.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if flt.enabled else Qt.CheckState.Unchecked)
            item.setToolTip("Untick to switch the filter off without removing it")
            self.list.addItem(item)
        self.list.blockSignals(False)
        if self.source.filters:
            self.list.setCurrentRow(max(0, min(select or 0, len(self.source.filters) - 1)))
        self._show_selected(self.list.currentRow())

    def _fill_add_menu(self) -> None:
        self.add_menu.clear()
        used = {f.kind for f in self.source.filters}
        for info in sorted(filter_kinds(self.media), key=lambda k: (not k.builtin, k.label)):
            action = self.add_menu.addAction(info.label)
            if self.media == "video" and info.kind in used:
                action.setEnabled(False)  # one of each effect per source
                action.setToolTip("Already added")
            action.triggered.connect(lambda _=False, kind=info.kind: self.add(kind))

    def add(self, kind: str) -> Filter:
        info = filter_kind(kind)
        names = {f.name for f in self.source.filters}
        name, n = info.label, 2
        while name in names:
            name, n = f"{info.label} {n}", n + 1
        flt = Filter(id=uuid.uuid4().hex[:8], kind=kind, name=name, settings=dict(info.defaults))
        self.source.filters.append(flt)
        self._refresh(len(self.source.filters) - 1)
        self.on_change(save=False)
        return flt

    def _current(self) -> Filter | None:
        row = self.list.currentRow()
        return self.source.filters[row] if 0 <= row < len(self.source.filters) else None

    def _remove(self) -> None:
        row = self.list.currentRow()
        if self._current() is None:
            return
        del self.source.filters[row]
        self._refresh(row)
        self.on_change(save=False)

    def _move(self, delta: int) -> None:
        row = self.list.currentRow()
        new = row + delta
        if self._current() is None or not 0 <= new < len(self.source.filters):
            return
        filters = self.source.filters
        filters[row], filters[new] = filters[new], filters[row]
        self._refresh(new)
        self.on_change(save=False)

    def _toggled(self, item: QListWidgetItem) -> None:
        row = self.list.row(item)
        if 0 <= row < len(self.source.filters):
            self.source.filters[row].enabled = item.checkState() == Qt.CheckState.Checked
            self.on_change(save=False)

    # Settings -----------------------------------------------------------------

    def _show_selected(self, row: int) -> None:
        flt = self._current()
        self.reset_button.setEnabled(flt is not None)
        if flt is None:
            self.title.setText("")
            if self.source.filters:
                empty = QLabel("")
            else:
                from sprigcast.app.empty import EmptyState

                empty = EmptyState("No filters yet. Click + to add one.")
            self.form_area.setWidget(empty)
            self.form = None
            return
        info = filter_kind(flt.kind)
        self.title.setText(info.label if info else flt.kind)
        if info is None:
            self.form_area.setWidget(QLabel(f"The {flt.kind!r} filter comes from a plugin that isn't loaded."))
            self.form = None
            return
        self.form = FilterForm(info.fields, settings_for(flt.kind, flt.settings))
        self.form.changed.connect(self._edited)
        self.form_area.setWidget(self.form)

    def _edited(self) -> None:
        flt = self._current()
        if flt is not None and self.form is not None:
            flt.settings = self.form.values()
            self.on_change(save=False)

    def _reset(self) -> None:
        flt = self._current()
        info = filter_kind(flt.kind) if flt else None
        if info is None or self.form is None:
            return
        self.form.set_values(info.defaults)
        flt.settings = dict(info.defaults)
        self.on_change(save=False)

    def done(self, result: int) -> None:  # Qt override
        self.on_change(save=True)
        super().done(result)
