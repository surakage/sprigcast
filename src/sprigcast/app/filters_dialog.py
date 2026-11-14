"""The Filters window for one source: add, remove, reorder, switch on/off and tune its filters."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.core import looks
from sprigcast.core.filters import Field, filter_kind, filter_kinds, settings_for
from sprigcast.core.scene import Filter, SourceConfig

AUDIO_KINDS = {"audio_input", "audio_output", "app_audio"}


def media_of(source: SourceConfig) -> str:
    return "audio" if source.kind in AUDIO_KINDS else "video"


def looks_folder() -> Path:
    """Documents\\SprigCast\\Looks: where SprigCast saves looks you make, and where it looks first for .cube files."""
    from PySide6.QtCore import QStandardPaths

    docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    return Path(docs or Path.home() / "Documents") / "SprigCast" / "Looks"


def file_problem(path: str) -> str:
    """Why a picked file can't be used, or "" if it's fine (or nothing is picked yet)."""
    if not path:
        return ""
    if not Path(path).is_file():
        return "That file isn't there any more."
    if path.lower().endswith(".cube"):
        try:
            looks.read_cube(path)
        except looks.LutError as exc:
            return str(exc)
    return ""


class FilterForm(QWidget):
    """Sliders, checkboxes and color buttons for one filter's fields. Emits `changed` on every edit."""

    changed = Signal()

    def __init__(self, fields: list[Field], values: dict, parent: QWidget | None = None):
        super().__init__(parent)
        self.fields = fields
        self.getters: dict[str, Callable[[], object]] = {}
        self.setters: dict[str, Callable[[object], None]] = {}
        self.rows: dict[str, QWidget] = {}
        self.form = form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for f in fields:
            self.rows[f.key] = self._widget(f, values.get(f.key, f.default))
            form.addRow(f.label, self.rows[f.key])
        self.changed.connect(self._show_rows)
        self._show_rows()

    def _show_rows(self) -> None:
        """Show only the rows that matter for the current choices (a file row only when "from a file")."""
        for f in self.fields:
            if f.show_when is not None:
                key, wanted = f.show_when
                getter = self.getters.get(key)
                self.form.setRowVisible(self.rows[f.key], getter is not None and getter() in wanted)

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
        if kind == "file":
            return self._file(f, str(value))
        from PySide6.QtWidgets import QLineEdit

        edit = QLineEdit(str(value))
        edit.textChanged.connect(self.changed)
        self.getters[f.key], self.setters[f.key] = edit.text, lambda v: edit.setText(str(v))
        return edit

    def _file(self, f: Field, value: str) -> QWidget:
        """The picked file's name, a Choose… pill, and a note if the file can't be used."""
        from PySide6.QtWidgets import QFileDialog

        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(8)
        name = QLabel()
        name.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        choose = QPushButton("Choose…")
        choose.setStyleSheet(RAISED_PILL)
        choose.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(name, 1)
        row.addWidget(choose)
        col.addLayout(row)
        note = QLabel()
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.WARN}; background: transparent;")
        col.addWidget(note)
        box.setProperty("path", value)

        def show(path: str) -> None:
            box.setProperty("path", path)
            name.setText(Path(path).name if path else "Nothing picked yet")
            name.setToolTip(path)
            problem = file_problem(path)
            note.setText(problem)
            note.setVisible(bool(problem))

        def pick() -> None:
            current = str(box.property("path") or "")
            start = str(Path(current).parent) if current else str(looks_folder() if f.key == "file" else Path.home())
            path, _ = QFileDialog.getOpenFileName(self, f.label, start, f.file_types or "All files (*)")
            if path:
                show(path)
                self.changed.emit()

        show(value)
        choose.clicked.connect(pick)
        self.getters[f.key] = lambda: str(box.property("path") or "")
        self.setters[f.key] = lambda v: show(str(v))
        return box

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


# On a PANEL pop-up, pills and number boxes are one step lighter so they read as things to press.
RAISED_PILL = theme.SMALL_PILL_STYLE + (
    f"QPushButton, QToolButton {{ background: {theme.RAISED}; }}"
    f"QPushButton:hover, QToolButton:hover {{ background: {theme.RAISED_HOVER}; }}")
NUMBER_STYLE = (
    f"QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox {{ background: {theme.RAISED}; color: {theme.TEXT};"
    f" border: none; border-radius: 12px; padding: 5px 10px; }}"
    f"QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{ background: {theme.RAISED_HOVER}; }}"
    "QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button"
    " { subcontrol-origin: margin; width: 0px; height: 0px; border: none; }")


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
        self.resize(820, 520)
        self.form: FilterForm | None = None

        # Calm and roomy: the filters as a list of rounded rows on the left (tick to switch one off),
        # its settings on a soft card on the right, and pill buttons with words instead of symbols.
        self.list = QListWidget()
        self.list.setMinimumWidth(240)
        self.list.setSpacing(3)
        self.list.setStyleSheet(
            f"QListWidget {{ background: transparent; border: none; outline: none; }}"
            f"QListWidget::item {{ background: {theme.RAISED}; color: {theme.TEXT}; border-radius: 12px;"
            f" padding: 9px 10px; }}"
            f"QListWidget::item:hover {{ background: {theme.RAISED_HOVER}; }}"
            f"QListWidget::item:selected {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}")
        self.list.currentRowChanged.connect(self._show_selected)
        self.list.itemChanged.connect(self._toggled)
        self.add_menu = QMenu(self)
        self.add_menu.aboutToShow.connect(self._fill_add_menu)
        add = QPushButton("+ Add filter")
        add.setStyleSheet(RAISED_PILL)
        add.setMenu(self.add_menu)
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.addWidget(add, 1)
        for text, tip, target in (
            ("↑", "Move up", lambda: self._move(-1)),
            ("↓", "Move down", lambda: self._move(+1)),
            ("Remove", "Remove the selected filter", self._remove),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setStyleSheet(RAISED_PILL)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(target)
            bar.addWidget(b)
        left = QVBoxLayout()
        left.setSpacing(12)
        title = QLabel("Sound filters" if self.media == "audio" else "Effects")
        title.setStyleSheet("font-weight: 600; font-size: 15px;")
        left.addWidget(title)
        self.empty = QLabel("No filters yet. Add one: a noise gate or compressor is a good start for a mic."
                            if self.media == "audio" else "No effects yet.")
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(f"color: {theme.MUTED}")
        left.addWidget(self.empty)
        left.addWidget(self.list, 1)
        left.addLayout(bar)

        self.title = QLabel("")
        self.title.setStyleSheet("font-weight: 600; font-size: 15px;")
        self.form_area = QScrollArea()
        self.form_area.setWidgetResizable(True)
        self.form_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.form_area.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.reset_button = QPushButton("Defaults")
        self.reset_button.setStyleSheet(RAISED_PILL)
        self.reset_button.setToolTip("Put this filter's settings back to their defaults")
        self.reset_button.clicked.connect(self._reset)
        hint = QLabel(
            "They run top to bottom. Untick one to switch it off without removing it." if self.media == "audio"
            else "Effects run on the GPU in a fixed order: scroll, sharpen, key, colour, look, then shape."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED}")
        card = QFrame()
        card.setObjectName("filterCard")
        card.setStyleSheet(f"#filterCard {{ background: {theme.CARD}; border-radius: 20px; }}" + NUMBER_STYLE)
        right = QVBoxLayout(card)
        right.setContentsMargins(18, 16, 18, 16)
        right.setSpacing(12)
        self.save_look_button = QPushButton("Save as look…")
        self.save_look_button.setStyleSheet(RAISED_PILL)
        self.save_look_button.setToolTip("Save these colour settings as a .cube LUT you can use anywhere")
        self.save_look_button.clicked.connect(self._save_look)
        self.save_look_button.hide()
        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addWidget(self.save_look_button)
        top.addWidget(self.reset_button)
        right.addLayout(top)
        right.addWidget(self.form_area, 1)
        right.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(20)
        body.addLayout(left)
        body.addWidget(card, 1)
        done = QPushButton("Done")
        done.setStyleSheet(theme.PRIMARY_STYLE)
        done.clicked.connect(self.reject)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 22, 22, 22)
        root.setSpacing(18)
        root.addLayout(body, 1)
        root.addWidget(done, 0, Qt.AlignmentFlag.AlignRight)
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
        self.empty.setVisible(not self.source.filters)
        self.list.setVisible(bool(self.source.filters))
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
            self.save_look_button.hide()
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
        self.save_look_button.setVisible(flt.kind == "color_correction")
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

    def _save_look(self, path: str | None = None) -> Path | None:
        """Bake the selected Color Correction into a .cube file (in Documents\\SprigCast\\Looks by default)."""
        flt = self._current()
        if flt is None or flt.kind != "color_correction":
            return None
        if path is None:
            from PySide6.QtWidgets import QFileDialog

            folder = looks_folder()
            folder.mkdir(parents=True, exist_ok=True)
            path, _ = QFileDialog.getSaveFileName(self, "Save as look", str(folder / f"{self.source.name} look.cube"),
                                                  "LUT (*.cube)")
            if not path:
                return None
        if not path.lower().endswith(".cube"):
            path += ".cube"
        table = looks.bake_color_correction(settings_for(flt.kind, flt.settings))
        saved = looks.write_cube(path, table, title=Path(path).stem)
        self.title.setText(f"Color Correction  ·  saved {saved.name}")
        return saved

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
