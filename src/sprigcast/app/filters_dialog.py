"""The Filters window for one source: add, remove, reorder, switch on/off and tune its filters."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.card import CardWindow
from sprigcast.core import looks
from sprigcast.core.filters import (
    Field,
    filter_kind,
    filter_kinds,
    settings_for,
    shown_name,
    switch,
)
from sprigcast.core.scene import Filter, SourceConfig, canvas_shared, new_filter_id


def _unshown(flt: Filter) -> dict:
    """What a filter keeps that isn't a field in its form (a barrel roll's count of runs): editing keeps it."""
    return {k: v for k, v in flt.settings.items() if k == "run"}


AUDIO_KINDS = {"audio_input", "audio_output", "app_audio"}


def media_of(source: SourceConfig) -> str:
    if source.kind == "media":  # a sound file takes sound filters; a video, picture filters
        from sprigcast.sources.media import is_sound_file

        return "audio" if is_sound_file(source.settings.get("path", "")) else "video"
    return "audio" if source.kind in AUDIO_KINDS else "video"


# Copied filters (user 2026-11-26): one filter or all of a leaf's, from the Filters window or the Leaves menu,
# pasted onto another leaf. Kept for the whole session, shared by every Filters window.
COPIED_FILTERS: list[Filter] = []


def copy_filters(filters: list[Filter]) -> int:
    COPIED_FILTERS[:] = [f.model_copy(deep=True) for f in filters]
    return len(COPIED_FILTERS)


def filter_media(flt: Filter) -> str | None:
    info = filter_kind(flt.kind)
    return info.media if info is not None else None  # from a plugin that isn't loaded: not pasted


def pasteable(medias: set[str], own_canvas: bool = False) -> list[Filter]:
    """The copied filters that fit here: the right kind of filter (picture or sound), and on a canvas's own
    list none of the ones that change the source itself."""
    return [f for f in COPIED_FILTERS if filter_media(f) in medias and not (own_canvas and canvas_shared(f.kind))]


def paste_filters(target: list[Filter], medias: set[str], own_canvas: bool = False) -> int:
    """The copied filters onto `target`, a leaf's list. A picture effect the leaf already has takes the copied
    settings in its place (one of each effect per leaf); anything else goes on the end. Returns how many."""
    pasted = 0
    for copied in pasteable(medias, own_canvas):
        flt = copied.model_copy(deep=True, update={"id": new_filter_id()})
        same = next((i for i, f in enumerate(target) if f.kind == flt.kind), None)
        if filter_media(flt) == "video" and same is not None:
            target[same] = flt.model_copy(update={"id": target[same].id})
        else:
            names, base, n = {f.name for f in target}, flt.name, 2
            while flt.name in names:
                flt.name, n = f"{base} {n}", n + 1
            target.append(flt)
        pasted += 1
    return pasted


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

    def __init__(self, fields: list[Field], values: dict, parent: QWidget | None = None,
                 sounds: list[tuple[str, str]] | None = None):
        super().__init__(parent)
        self.fields = fields
        self.sounds = sounds or []  # (id, name): what a "sound" field (Ducking's "When this plays") offers
        self.getters: dict[str, Callable[[], object]] = {}
        self.setters: dict[str, Callable[[object], None]] = {}
        self.rows: dict[str, QWidget] = {}
        self.form = form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(14)
        for f in fields:
            self.rows[f.key] = self._widget(f, values.get(f.key, f.default))
            label = QLabel(f.label)
            label.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")  # the theme's text, not Qt's dark
            form.addRow(label, self.rows[f.key])
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
        if kind in ("choice", "sound"):
            combo = QComboBox()
            combo.setObjectName("filterPick")
            choices = f.choices if kind == "choice" else [("", "Pick a sound"), *self.sounds]
            for data, label in choices:
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


# On a PANEL pop-up, pills use the shared button look and number boxes are one step lighter.
RAISED_PILL = theme.SMALL_PILL_STYLE
# A part of the Filters window (the list, a filter's settings): a soft card on the window, so the parts read apart.
SECTION_STYLE = "#{name} {{ background: %s; border: 1px solid %s; border-radius: 18px; }}" % (theme.SECTION,
                                                                                         theme.SECTION_EDGE)
NUMBER_STYLE = (
    f"QSpinBox, QDoubleSpinBox, QLineEdit, QComboBox {{ background: {theme.RAISED}; color: {theme.TEXT};"
    f" border: none; border-radius: 12px; padding: 5px 10px; }}"
    f"QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{ background: {theme.RAISED_HOVER}; }}"
    "QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button"
    " { subcontrol-origin: margin; width: 0px; height: 0px; border: none; }")


# Each filter's icon (app/icons.py), drawn sharp at every Windows scale.
KIND_ICONS = {
    "barrel_roll": "spin", "blackout": "curtain", "blur": "mist", "chroma_key": "greenscreen",
    "color_correction": "tone", "color_key": "colorcut", "lut": "mood", "freeze": "freeze", "glow": "glow",
    "luma_key": "lightcut", "pixelate": "mosaic", "scroll": "drift", "shadow": "shadow", "shape": "frame",
    "sharpen": "crisp", "stroke": "outline", "background_effect": "backdrop",
}


def icon_name(kind: str) -> str:
    return KIND_ICONS.get(kind, "sliders")


class FilterRow(QStyledItemDelegate):
    """One filter in the list: its icon, its name and an on/off switch, all painted (crisp at any scale).
    A click on the switch turns the filter on or off; a click anywhere else picks it."""

    HEIGHT = 44
    SWITCH = QSize(34, 20)

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), self.HEIGHT)

    def _switch_rect(self, rect: QRect) -> QRect:
        return QRect(rect.right() - 10 - self.SWITCH.width(), rect.center().y() - self.SWITCH.height() // 2 + 1,
                     self.SWITCH.width(), self.SWITCH.height())

    def paint(self, p: QPainter, option, index) -> None:
        from sprigcast.app import icons

        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect.adjusted(0, 2, 0, -2)
        picked = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        on = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value or \
            index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        if picked or hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.SELECT_BG if picked else theme.RAISED_HOVER))
            p.drawRoundedRect(QRectF(r), 12, 12)
        tile = QRectF(r.left() + 8, r.center().y() - 14, 28, 28)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.SELECT_BG if on and not picked else theme.RAISED))
        p.drawRoundedRect(tile, 8, 8)
        kind = index.data(Qt.ItemDataRole.UserRole + 1) or ""
        icons.paint_icon(p, tile.adjusted(6, 6, -6, -6), icon_name(kind), theme.ACCENT if on else theme.MUTED, 1.6)
        sw = self._switch_rect(r)
        text = QRect(int(tile.right()) + 10, r.top(), sw.left() - int(tile.right()) - 18, r.height())
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold if picked else QFont.Weight.Medium)
        p.setFont(font)
        p.setPen(QColor(theme.SELECT_TEXT if picked else (theme.TEXT if on else theme.MUTED)))
        name = QFontMetrics(font).elidedText(index.data(Qt.ItemDataRole.DisplayRole) or "", Qt.TextElideMode.ElideRight,
                                             text.width())
        p.drawText(text, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.ACCENT if on else theme.BUTTON_EDGE))
        p.drawRoundedRect(QRectF(sw), sw.height() / 2, sw.height() / 2)
        knob = sw.height() - 6
        x = sw.right() - 2 - knob if on else sw.left() + 3
        p.setBrush(QColor(theme.ACCENT_TEXT if on else theme.TEXT))
        p.drawEllipse(QRectF(x, sw.top() + 3, knob, knob))
        p.restore()

    def editorEvent(self, event, model, option, index) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.MouseButtonRelease and \
                self._switch_rect(option.rect.adjusted(0, 2, 0, -2)).adjusted(-6, -6, 6, 6).contains(event.position().toPoint()):
            now = index.data(Qt.ItemDataRole.CheckStateRole)
            on = now == Qt.CheckState.Checked or now == Qt.CheckState.Checked.value
            model.setData(index, Qt.CheckState.Unchecked if on else Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
            return True
        return super().editorEvent(event, model, option, index)


# Where each filter sits in the Add filter picker (A to Z inside each group; a plugin's lands in More).
KIND_GROUPS = {
    "color_correction": "Color", "lut": "Color",
    "background_effect": "Cut out", "chroma_key": "Cut out", "color_key": "Cut out", "luma_key": "Cut out",
    "sharpen": "Look", "shape": "Look", "glow": "Look", "blur": "Look", "pixelate": "Look", "stroke": "Look",
    "shadow": "Look",
    "blackout": "Motion", "scroll": "Motion", "freeze": "Motion", "barrel_roll": "Motion",
}
GROUP_ORDER = ("Color", "Cut out", "Look", "Motion", "More")


class AddPicker(QFrame):
    """Add filter: a search box over every filter as an icon tile, grouped (Color, Cut out, Look, Motion).
    Type to narrow it, Enter adds the first match. A filter the source already has is greyed out."""

    picked = Signal(str)  # kind
    TILE = QSize(116, 92)
    INSET = 12  # room each side of a tile's name, so no name brushes the tile's edge

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("addPicker")
        self.tiles: dict[str, QToolButton] = {}  # label -> tile
        self._groups: list[tuple[QLabel, list[QToolButton]]] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.box = QFrame()
        self.box.setObjectName("addPickerBox")
        edge = QColor(theme.ACCENT)
        edge.setAlpha(90)
        self.box.setStyleSheet(
            f"#addPickerBox {{ background: {theme.PANEL}; border: 1px solid {edge.name(QColor.NameFormat.HexArgb)};"
            " border-radius: 18px; }"
            f"QToolButton {{ background: {theme.CARD}; color: {theme.TEXT}; border: 1px solid {theme.EDGE_STRONG};"
            f" border-radius: 14px; padding: 8px {self.INSET}px 8px; font-weight: 550; }}"
            f"QToolButton:hover, QToolButton:focus {{ background: {theme.SELECT_BG}; border-color: {theme.ACCENT_DARK};"
            f" color: {theme.SELECT_TEXT}; }}"
            f"QToolButton:disabled {{ color: {theme.MUTED}; background: transparent; }}"
            f"QLineEdit {{ background: {theme.CHROME}; color: {theme.TEXT}; border: 1px solid {theme.FIELD_EDGE};"
            " border-radius: 12px; padding: 7px 10px; }"
            f"QLineEdit:focus {{ border-color: {theme.ACCENT}; }}"
            f"QLabel {{ color: {theme.MUTED}; font-weight: 600; background: transparent; }}"
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }")
        outer.addWidget(self.box)
        lay = QVBoxLayout(self.box)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)
        from sprigcast.app import icons

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.addAction(icons.icon("search", theme.MUTED, 16), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._narrow)
        self.search.returnPressed.connect(self._first)
        lay.addWidget(self.search)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        lay.addWidget(self.scroll, 1)
        self.setFixedWidth(4 * self.TILE.width() + 3 * 8 + 32 + 4 + 24)  # 4 tiles, margins, the scroll bar

    def fill(self, kinds: list, used: set[str], media: str) -> None:
        """Tiles for these filter kinds; `used` kinds are greyed (one of each effect per source)."""
        from sprigcast.app import icons
        from sprigcast.core.filters import OLD_LABELS

        self.tiles.clear()
        self._groups.clear()
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 4, 0)
        col.setSpacing(8)
        grouped: dict[str, list] = {}
        for info in kinds:
            group = KIND_GROUPS.get(info.kind, "More") if media == "video" else "Sound"
            grouped.setdefault(group, []).append(info)
        for group in [g for g in (*GROUP_ORDER, "Sound") if g in grouped]:
            heading = QLabel(group)
            col.addWidget(heading)
            grid = QGridLayout()
            grid.setSpacing(8)
            buttons = []
            for n, info in enumerate(sorted(grouped[group], key=lambda k: k.label.lower())):
                tile = QToolButton()
                tile.setText(self.fit_name(info.label))
                picture = QIcon(icons.icon(icon_name(info.kind), theme.ACCENT, 22))
                for ratio in (1.0, 1.25, 1.5, 2.0, 3.0):  # greyed when already added, sharp at every scale
                    picture.addPixmap(icons.pixmap(icon_name(info.kind), theme.MUTED, 22, ratio), QIcon.Mode.Disabled)
                tile.setIcon(picture)
                tile.setIconSize(QSize(22, 22))
                tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
                tile.setFixedSize(self.TILE)
                tile.setCursor(Qt.CursorShape.PointingHandCursor)
                tile.setProperty("kind", info.kind)
                tile.setProperty("words", f"{info.label} {OLD_LABELS.get(info.kind, '')} {group}".lower())
                if media == "video" and info.kind in used:
                    tile.setEnabled(False)
                    tile.setToolTip("Already added")
                tile.clicked.connect(lambda _=False, kind=info.kind: self._pick(kind))
                grid.addWidget(tile, n // 4, n % 4)
                buttons.append(tile)
                self.tiles[info.label] = tile
            grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            col.addLayout(grid)
            self._groups.append((heading, buttons))
        col.addStretch(1)
        self.scroll.setWidget(inner)
        self.search.clear()
        rows = sum((len(b) + 3) // 4 for _, b in self._groups)
        self.setFixedHeight(min(560, 16 * 2 + 40 + 10 + len(self._groups) * 30 + rows * (self.TILE.height() + 8) + 8))

    def name_font(self) -> QFont:
        font = QFont(self.font())
        font.setWeight(QFont.Weight(550))
        return font

    def fit_name(self, label: str) -> str:
        """A tile's name whole: on one line when it fits inside the tile's inset, else broken onto two lines
        at the space that keeps the longer line shortest (Noise / Suppression), never cut short with ...."""
        metrics = QFontMetrics(self.name_font())
        room = self.TILE.width() - 2 * self.INSET - 4
        if metrics.horizontalAdvance(label) <= room or " " not in label:
            return label
        words = label.split(" ")
        splits = [(" ".join(words[:n]), " ".join(words[n:])) for n in range(1, len(words))]
        top, bottom = min(splits, key=lambda s: max(metrics.horizontalAdvance(s[0]), metrics.horizontalAdvance(s[1])))
        return f"{top}\n{bottom}"

    def _narrow(self, text: str) -> None:
        wanted = text.strip().lower()
        for heading, buttons in self._groups:
            any_shown = False
            for tile in buttons:
                show = not wanted or wanted in str(tile.property("words"))
                tile.setVisible(show)
                any_shown |= show
            heading.setVisible(any_shown)

    def _first(self) -> None:
        tile = next((t for _, b in self._groups for t in b if not t.isHidden() and t.isEnabled()), None)
        if tile is not None:
            self._pick(str(tile.property("kind")))

    def _pick(self, kind: str) -> None:
        self.hide()
        self.picked.emit(kind)

    def open_below(self, anchor: QWidget) -> None:
        from PySide6.QtGui import QGuiApplication

        spot = anchor.mapToGlobal(QPoint(0, anchor.height() + 6))
        screen = QGuiApplication.screenAt(spot) or QGuiApplication.primaryScreen()
        room = screen.availableGeometry()
        x = min(max(room.left(), spot.x()), room.right() - self.width())
        y = spot.y() if spot.y() + self.height() <= room.bottom() else anchor.mapToGlobal(QPoint(0, 0)).y() - self.height() - 6
        self.move(x, max(room.top(), y))
        self.show()
        self.search.setFocus()


def nv12_to_rgba(data: bytes, width: int, height: int):
    """An NV12 picture (BT.709, limited range, as the engine makes for encoders) as H x W x 4 RGBA."""
    import numpy as np

    y = np.frombuffer(data, np.uint8, width * height).reshape(height, width).astype(np.float32)
    uv = np.frombuffer(data, np.uint8, width * height // 2, width * height).reshape(height // 2, width // 2, 2)
    uv = uv.astype(np.float32).repeat(2, 0).repeat(2, 1) - 128.0
    luma = (y - 16.0) * (255.0 / 219.0)
    u, v = uv[..., 0] * (255.0 / 224.0), uv[..., 1] * (255.0 / 224.0)
    r = luma + 1.5748 * v
    g = luma - 0.1873 * u - 0.4681 * v
    b = luma + 1.8556 * u
    out = np.empty((height, width, 4), np.uint8)
    out[..., 0], out[..., 1], out[..., 2] = (np.clip(c, 0, 255) for c in (r, g, b))
    out[..., 3] = 255
    return out


class EngineGrab:
    """The Filters window's pictures from the engine.

    With the native engine both come from it, as two small views of the source alone (Engine.set_view): Before
    with no looks at all and After with the window's unapplied filters (Engine.set_view_effects), so filters only
    the native engine draws (Backdrop) show too, and the pictures come every frame, ready for the window to take
    15 times a second. The Python engine draws them on request instead (Engine.grab_source), 5 times a second.
    stop() lets go of the views when the window closes.

    PC 2026-11-22: After showed nothing with Backdrop. Asking for Before let go of After's view every time, and the
    view showed the source's own filters, not the window's."""

    BEFORE, AFTER = "filters-before", "filters-after"
    LONG_SIDE = 480

    def __init__(self, engine, collection):
        self.engine, self.collection = engine, collection
        self.native = getattr(engine, "_native", None) is not None and hasattr(engine, "set_view_effects")
        self.every_ms = 66 if self.native else 200
        self.latest: dict[str, tuple] = {}  # view name -> (bytes, w, h, frame number), from the render thread
        self._made: dict[str, tuple] = {}  # view name -> (frame number, RGBA picture) last handed to the window
        self._count = 0
        self._tapped: str | None = None  # the source the views show
        self._looks: tuple | None = None  # what the After view's looks were made from

    def __call__(self, source_id: str, back: Callable, raw: bool, limit: int, filters: list | None = None) -> None:
        """filters: the window's unapplied copies, drawn instead of the source's own (After only)."""
        if not self.native:
            self.engine.grab_source(source_id, back, raw, limit, filters=None if raw else filters)
            return
        if self._tapped != source_id:
            self._tap(source_id)
        if not raw:
            source = self.collection.source(source_id)
            self._send_looks(source_id, filters if filters is not None else (source.filters if source else []))
        back(self._picture(self.BEFORE if raw else self.AFTER))

    def _picture(self, name: str):
        got = self.latest.get(name)
        if got is None:
            return None
        made = self._made.get(name)
        if made is not None and made[0] == got[3]:
            return made[1]  # no new frame since: the same picture, not converted again
        picture = nv12_to_rgba(got[0], got[1], got[2])
        self._made[name] = (got[3], picture)
        return picture

    def _send_looks(self, source_id: str, filters: list) -> None:
        from sprigcast.core.filters import VideoEffects, video_effects
        from sprigcast.core.scene import item_key

        looks = (source_id, tuple((f.kind, f.enabled, repr(sorted(f.settings.items()))) for f in filters))
        if looks == self._looks:
            return
        self._looks = looks
        self.engine.set_view_effects({item_key(f"view:{self.BEFORE}", source_id): VideoEffects(),
                                      item_key(f"view:{self.AFTER}", source_id): video_effects(filters)})

    def _tap(self, source_id: str) -> None:
        self.stop()
        canvases = [c for c in self.collection.canvases if c.enabled] or self.collection.canvases
        if not canvases:
            return
        canvas = max(canvases, key=lambda c: c.width / max(1, c.height))  # the widest: the window's picture is wide
        k = min(1.0, self.LONG_SIDE / max(canvas.width, canvas.height))
        size = (max(2, round(canvas.width * k) // 2 * 2), max(2, round(canvas.height * k) // 2 * 2))
        for name in (self.BEFORE, self.AFTER):
            self.engine.set_view(name, canvas.id, "draft", (source_id, self.LONG_SIDE))
            self.engine.add_frame_tap(f"view:{name}", self._taker(name), nv12_size=size)
        self._tapped = source_id

    def _taker(self, name: str) -> Callable:
        takers = self.__dict__.setdefault("_takers", {})
        if name not in takers:
            def take(frame, name=name) -> None:  # render thread: keep the newest, nothing more
                self._count += 1
                self.latest[name] = (bytes(frame.data), frame.width, frame.height, self._count)

            takers[name] = take
        return takers[name]

    def stop(self) -> None:
        if self._tapped is None:
            return
        for name in (self.BEFORE, self.AFTER):
            self.engine.remove_frame_tap(f"view:{name}", self._taker(name))
            self.engine.set_view(name, None)
        self.engine.set_view_effects({})
        self._tapped, self._looks = None, None
        self.latest, self._made = {}, {}


class _Grabbed(QObject):
    """Carries a picture from the render thread to the window (queued). No parent: a grab that lands after the
    window closed reaches no one instead of a deleted widget."""

    got = Signal(object, bool)  # QImage or None, raw (Before)


class BeforeAfter(QWidget):
    """The source live, with its filters off on the left of the split and on to the right. Drag the split.
    Fixed height (a height that follows the width loops in a scroll area: the Transitions crash, #688)."""

    HEIGHT = 240
    EVERY_MS = 200  # 5 pictures a second of each side, small (LIMIT), only while the window shows

    LIMIT = 480

    def __init__(self, source_id: str, grab: Callable, parent: QWidget | None = None,
                 draft: Callable[[], list] | None = None):
        super().__init__(parent)
        self.source_id, self.fetch = source_id, grab
        self.draft = draft  # the window's unapplied filters (copies) for the After side
        self.before: QImage | None = None
        self.after: QImage | None = None
        self.split = 0.5
        self._waiting = 0
        self._dragging = False
        self.setFixedHeight(self.HEIGHT)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setAccessibleName("Before and after")
        self.bridge = _Grabbed()
        self.bridge.got.connect(self._take)
        self.timer = QTimer(self)
        self.timer.setInterval(getattr(grab, "every_ms", self.EVERY_MS))  # the native engine's views: smoother
        self.timer.timeout.connect(self.ask)
        # The first pictures wait until the window is up. Asking straight away had the engine make its two
        # views in the same moment the window was being laid out and painted: one late frame of 148 ms the
        # first time Filters opened and 48 ms after (PC 2026-11-25). Apart, each fits in a frame or two.
        self.first = QTimer(self)
        self.first.setSingleShot(True)
        self.first.setInterval(self.FIRST_MS)
        self.first.timeout.connect(self._start)

    FIRST_MS = 150

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.first.start()

    def _start(self) -> None:
        if self.isVisible():
            self.ask()
            self.timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self.first.stop()
        self.timer.stop()
        super().hideEvent(event)

    def ask(self) -> None:
        """Ask for the next pair, unless the last pair hasn't come back yet (a busy PC isn't asked for more)."""
        if self._waiting or not self.isVisible():
            return
        bridge = self.bridge
        for raw in (True, False):
            def back(picture, raw=raw):
                image = None
                if picture is not None and getattr(picture, "ndim", 0) == 3 and picture.shape[0] and picture.shape[1]:
                    h, w = picture.shape[:2]
                    image = QImage(picture.tobytes(), w, h, w * 4, QImage.Format.Format_RGBA8888_Premultiplied).copy()
                bridge.got.emit(image, raw)

            self._waiting += 1
            try:
                if self.draft is not None and not raw:
                    self.fetch(self.source_id, back, raw, self.LIMIT, filters=self.draft())
                else:
                    self.fetch(self.source_id, back, raw, self.LIMIT)
            except Exception:
                self._waiting = 0
                return

    def _take(self, image, raw: bool) -> None:
        self._waiting = max(0, self._waiting - 1)
        if raw:
            self.before = image
        else:
            self.after = image
        self.update()

    def _picture_rect(self, inner: QRectF) -> QRectF:
        image = self.after or self.before
        if image is None or image.isNull():
            return inner
        scale = min(inner.width() / image.width(), inner.height() / image.height())
        w, h = image.width() * scale, image.height() * scale
        return QRectF(inner.center().x() - w / 2, inner.center().y() - h / 2, w, h)

    def paintEvent(self, event) -> None:  # noqa: N802
        from sprigcast.app import icons

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        box = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(box, 14, 14)
        p.setClipPath(clip)
        p.fillRect(box, QColor(theme.CHROME))
        split_x = box.left() + box.width() * self.split
        # the After side on a soft checkerboard, so a key or cut-out shows what's see-through
        cell = 12
        light, dark = QColor(theme.CARD), QColor(theme.PANEL)
        right = QRectF(split_x, box.top(), box.right() - split_x, box.height())
        p.save()
        p.setClipRect(right, Qt.ClipOperation.IntersectClip)
        for y in range(0, int(box.height()) + cell, cell):
            for x in range(int(split_x) // cell * cell, int(box.right()) + cell, cell):
                p.fillRect(QRectF(x, y, cell, cell), light if (x // cell + y // cell) % 2 else dark)
        p.restore()
        picture = self._picture_rect(box.adjusted(10, 10, -10, -10))
        for image, side in ((self.before, QRectF(box.left(), box.top(), split_x - box.left(), box.height())),
                            (self.after, right)):
            if image is not None and not image.isNull():
                p.save()
                p.setClipRect(side, Qt.ClipOperation.IntersectClip)
                p.drawImage(picture, image)
                p.restore()
        if self.before is None and self.after is None:
            p.setPen(QColor(theme.MUTED))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, "No picture yet")
        # the split, its round handle, and the two chips
        p.setPen(QPen(QColor(theme.TEXT_BRIGHT), 2))
        p.drawLine(QPointF(split_x, box.top()), QPointF(split_x, box.bottom()))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.TEXT_BRIGHT))
        p.drawEllipse(QPointF(split_x, box.center().y()), 15, 15)
        icons.paint_icon(p, QRectF(split_x - 8, box.center().y() - 8, 16, 16), "drift", theme.ACCENT_TEXT, 2.0)
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        metrics = QFontMetrics(font)
        for text, left in (("Before", True), ("After", False)):
            w = metrics.horizontalAdvance(text) + 22
            chip = QRectF(box.left() + 12 if left else box.right() - 12 - w, box.top() + 12, w, 26)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawRoundedRect(chip, 13, 13)
            p.setPen(QColor(theme.TEXT_BRIGHT))
            p.drawText(chip, Qt.AlignmentFlag.AlignCenter, text)
        p.end()

    def _move_split(self, x: float) -> None:
        self.split = min(1.0, max(0.0, x / max(1, self.width())))
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._move_split(event.position().x())
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._move_split(event.position().x())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._dragging = False
        event.accept()


# Sound filters -------------------------------------------------------------------------------------------
# User 2026-11-21: sound filters get their own live view, picked by what the filter does, so a change can be
# understood as you make it: a live curve for loudness filters, a spectrum for tone filters, a waveform for the
# rest. Before and after meters under it, and a Listen switch (Off, Before, After) to hear either side.

CURVE_KINDS = frozenset({"noise_gate", "compressor", "upward_compressor", "expander", "limiter"})
SPECTRUM_KINDS = frozenset({"eq3", "low_cut", "de_esser", "noise_suppression"})
DB_LOW = -60.0  # the curve's and meters' quietest shown level
SPECTRUM_LOW = -96.0


def view_for(kind: str | None) -> str:
    """Which live view explains a filter best: "curve", "spectrum" or "waveform"."""
    if kind in CURVE_KINDS:
        return "curve"
    if kind in SPECTRUM_KINDS:
        return "spectrum"
    return "waveform"


def curve_out(kind: str, settings: dict, level_db: float) -> float:
    """What a loudness filter makes of a steady sound at level_db (its static curve, as drawn)."""
    s = settings
    if kind == "compressor":
        t, r = float(s["threshold_db"]), max(1.0, float(s["ratio"]))
        out = level_db if level_db <= t else t + (level_db - t) / r
        return out + float(s.get("output_db", 0.0))
    if kind == "limiter":
        return min(level_db, float(s["threshold_db"]))
    if kind == "expander":
        t, r = float(s["threshold_db"]), max(1.0, float(s["ratio"]))
        return level_db if level_db >= t else max(-120.0, t + (level_db - t) * r)
    if kind == "upward_compressor":
        t, r = float(s["threshold_db"]), max(1.0, float(s["ratio"]))
        return level_db if level_db >= t else t + (level_db - t) / r
    if kind == "noise_gate":
        return level_db if level_db >= float(s["open_db"]) else -120.0
    return level_db


def bands(samples, count: int = 40, low_hz: float = 60.0, high_hz: float = 16000.0):
    """Loudness (dB) in `count` bands spaced like hearing (log), from the newest 4096 samples."""
    import numpy as np

    take = samples[-4096:]
    if len(take) < 4096:
        take = np.pad(take, (4096 - len(take), 0))
    window = np.hanning(len(take)).astype(np.float32)
    power = np.abs(np.fft.rfft(take * window)) / (len(take) / 4)
    freqs = np.fft.rfftfreq(len(take), 1 / 48000)
    edges = np.geomspace(low_hz, high_hz, count + 1)
    out = np.empty(count, np.float32)
    for i in range(count):
        picked = power[(freqs >= edges[i]) & (freqs < edges[i + 1])]
        level = float(picked.max()) if len(picked) else 0.0
        out[i] = 20 * np.log10(level) if level > 1e-6 else SPECTRUM_LOW
    return np.maximum(out, SPECTRUM_LOW)


class SoundView(QWidget):
    """The live view over a sound filter's settings: the curve, the spectrum or the waveform (by the picked
    filter), twin Before/After meters, and the Listen switch. Draws from preview.snapshot() 15 times a second,
    only while showing. Fixed height, as BeforeAfter."""

    HEIGHT = 286
    PANEL_HEIGHT = 196
    EVERY_MS = 66

    def __init__(self, preview, selected: Callable[[], Filter | None], parent: QWidget | None = None):
        super().__init__(parent)
        self.preview = preview
        self.selected = selected
        self.setFixedHeight(self.HEIGHT)
        self.setAccessibleName("Sound before and after")
        self._held = [DB_LOW, DB_LOW]  # meter peaks, falling slowly
        self.panel = QWidget(self)
        self.panel.setFixedHeight(self.PANEL_HEIGHT)
        self.panel.paintEvent = self._paint_panel  # type: ignore[method-assign]
        self.meters = QWidget(self)
        self.meters.setFixedHeight(40)
        self.meters.paintEvent = self._paint_meters  # type: ignore[method-assign]
        listen = QLabel("Listen")
        listen.setStyleSheet(f"color: {theme.LABEL}; background: transparent; font-weight: 600;")
        self.listen_buttons: dict[str, QPushButton] = {}
        switch_row = QHBoxLayout()
        switch_row.setSpacing(0)
        for mode, text in (("off", "Off"), ("before", "Before"), ("after", "After")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setAccessibleName(f"Listen {text.lower()}" if mode != "off" else "Listen off")
            first, last = mode == "off", mode == "after"
            radius = f"border-top-left-radius: {15 if first else 0}px; border-bottom-left-radius: {15 if first else 0}px;" \
                     f" border-top-right-radius: {15 if last else 0}px; border-bottom-right-radius: {15 if last else 0}px;"
            b.setStyleSheet(
                f"QPushButton {{ background: {theme.RAISED}; color: {theme.TEXT}; border: 1px solid {theme.BUTTON_EDGE};"
                f" {radius} padding: 5px 16px; min-height: 20px; }}"
                f"QPushButton:hover {{ background: {theme.RAISED_HOVER}; }}"
                f"QPushButton:checked {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; border-color: {theme.ACCENT};"
                " font-weight: 650; }")
            b.clicked.connect(lambda _=False, m=mode: self.set_listen(m))
            self.listen_buttons[mode] = b
            switch_row.addWidget(b)
        self.listen_buttons["off"].setChecked(True)
        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        bottom.addWidget(self.meters, 1)
        bottom.addWidget(listen)
        bottom.addLayout(switch_row)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        col.addWidget(self.panel)
        col.addLayout(bottom)
        self.timer = QTimer(self)
        self.timer.setInterval(self.EVERY_MS)
        self.timer.timeout.connect(self._tick)

    def set_listen(self, mode: str) -> None:
        for m, b in self.listen_buttons.items():
            b.setChecked(m == mode)
        self.preview.set_listen(mode)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(event)

    def _tick(self) -> None:
        self.panel.update()
        self.meters.update()

    def view(self) -> str:
        flt = self.selected()
        return view_for(flt.kind if flt is not None else None)

    # Drawing ------------------------------------------------------------------------------------------

    def _chip(self, p: QPainter, box: QRectF, text: str, end: float | None = None, color: str | None = None,
              dot: bool = False) -> QRectF:
        """A dark chip in the top row: from the left, or ending at x=end. dot: a colored dot as its key."""
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        w = QFontMetrics(font).horizontalAdvance(text) + (36 if dot else 22)
        chip = QRectF(end - w if end is not None else box.left() + 12, box.top() + 12, w, 26)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(chip, 13, 13)
        text_box = chip
        if dot:
            p.setBrush(QColor(color or theme.TEXT_BRIGHT))
            p.drawEllipse(QPointF(chip.left() + 15, chip.center().y()), 4, 4)
            text_box = chip.adjusted(14, 0, 0, 0)
        p.setPen(QColor(theme.TEXT_BRIGHT))
        p.drawText(text_box, Qt.AlignmentFlag.AlignCenter, text)
        return chip

    def _paint_panel(self, _event) -> None:
        before, after, levels = self.preview.snapshot()
        p = QPainter(self.panel)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.panel.rect())
        clip = QPainterPath()
        clip.addRoundedRect(box, 14, 14)
        p.setClipPath(clip)
        p.fillRect(box, QColor(theme.CHROME))
        inner = box.adjusted(16, 48, -16, -14)
        view = self.view()
        flt = self.selected()
        if view == "curve" and flt is not None:
            self._paint_curve(p, inner, flt, levels)
            self._chip(p, box, "Live curve")
            key = (("Now", theme.TEXT_BRIGHT), ("Your settings", theme.ACCENT))
        elif view == "spectrum":
            self._paint_spectrum(p, inner, before, after)
            self._chip(p, box, "Spectrum")
            key = (("After", theme.ACCENT), ("Before", theme.MUTED))
        else:
            self._paint_wave(p, inner, before, after)
            self._chip(p, box, "Waveform")
            key = (("After", theme.ACCENT), ("Before", theme.MUTED))
        end = box.right() - 12
        for text, color in key:
            end = self._chip(p, box, text, end=end, color=color, dot=True).left() - 8
        p.end()

    def _paint_wave(self, p: QPainter, box: QRectF, before, after) -> None:
        import numpy as np

        mid = box.center().y()
        p.setPen(QPen(QColor(theme.EDGE_STRONG), 1))
        p.drawLine(QPointF(box.left(), mid), QPointF(box.right(), mid))
        columns = max(1, int(box.width() / 3))
        for samples, color, alpha in ((before, theme.MUTED, 110), (after, theme.ACCENT, 255)):
            chunks = np.array_split(np.abs(samples), columns)
            c = QColor(color)
            c.setAlpha(alpha)
            p.setPen(QPen(c, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for i, chunk in enumerate(chunks):
                level = min(1.0, float(chunk.max()) if len(chunk) else 0.0)
                h = max(1.0, level * box.height() / 2)
                x = box.left() + (i + 0.5) * box.width() / columns
                p.drawLine(QPointF(x, mid - h), QPointF(x, mid + h))

    def _paint_spectrum(self, p: QPainter, box: QRectF, before, after) -> None:
        low, high = SPECTRUM_LOW, 0.0
        b, a = bands(before), bands(after)
        n = len(b)
        width = box.width() / n
        grey = QColor(theme.MUTED)
        grey.setAlpha(90)
        for i in range(n):  # before: soft bars
            h = (b[i] - low) / (high - low) * box.height()
            p.fillRect(QRectF(box.left() + i * width + 1.5, box.bottom() - h, width - 3, h), grey)
        path = QPainterPath()  # after: a green line over them
        for i in range(n):
            y = box.bottom() - (a[i] - low) / (high - low) * box.height()
            point = QPointF(box.left() + (i + 0.5) * width, y)
            path.moveTo(point) if i == 0 else path.lineTo(point)
        p.setPen(QPen(QColor(theme.ACCENT), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.setPen(QColor(theme.MUTED))
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
        p.setFont(small)
        for label, frac in (("100 Hz", 0.093), ("1 kHz", 0.47), ("10 kHz", 0.85)):
            p.drawText(QRectF(box.left() + frac * box.width() - 30, box.bottom() - 16, 60, 16),
                       Qt.AlignmentFlag.AlignCenter, label)

    def _paint_curve(self, p: QPainter, box: QRectF, flt: Filter, levels) -> None:
        side = min(box.width(), box.height())
        area = QRectF(box.center().x() - side / 2 * 1.6, box.top(), side * 1.6, side)
        area = area.intersected(box)

        def at(level_in: float, level_out: float) -> QPointF:
            fx = (level_in - DB_LOW) / -DB_LOW
            fy = (min(0.0, max(DB_LOW, level_out)) - DB_LOW) / -DB_LOW
            return QPointF(area.left() + fx * area.width(), area.bottom() - fy * area.height())

        p.setPen(QPen(QColor(theme.EDGE_STRONG), 1))
        for db in (-48, -36, -24, -12):
            x = at(db, db).x()
            y = at(db, db).y()
            p.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
            p.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
        p.setPen(QPen(QColor(theme.MUTED), 1.2, Qt.PenStyle.DashLine))
        p.drawLine(at(DB_LOW, DB_LOW), at(0, 0))  # as it comes in
        settings = settings_for(flt.kind, flt.settings)
        path = QPainterPath()
        steps = 120
        for i in range(steps + 1):
            level = DB_LOW + (-DB_LOW) * i / steps
            point = at(level, curve_out(flt.kind, settings, level))
            path.moveTo(point) if i == 0 else path.lineTo(point)
        p.setPen(QPen(QColor(theme.ACCENT), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        threshold = settings.get("threshold_db", settings.get("open_db"))
        if threshold is not None:
            x = at(float(threshold), 0).x()
            p.setPen(QPen(QColor(theme.WARN), 1.2, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
        level_in = levels[0]
        if level_in > DB_LOW:  # the sound now: how loud it comes in, and where this filter's curve takes it
            dot = at(min(0.0, level_in), curve_out(flt.kind, settings, min(0.0, level_in)))
            glow = QColor(theme.ACCENT)
            glow.setAlpha(70)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(dot, 11, 11)
            p.setBrush(QColor(theme.TEXT_BRIGHT))
            p.drawEllipse(dot, 5, 5)
        p.setPen(QColor(theme.MUTED))
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
        p.setFont(small)
        p.drawText(QRectF(area.left(), area.bottom() - 16, area.width() - 4, 16),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "In")
        p.drawText(QRectF(area.left() + 4, area.top(), 40, 16), Qt.AlignmentFlag.AlignLeft, "Out")

    def _paint_meters(self, _event) -> None:
        _before, _after, levels = self.preview.snapshot()
        p = QPainter(self.meters)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(self.meters.rect())
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        label_w = QFontMetrics(font).horizontalAdvance("Before") + 10
        number_w = QFontMetrics(font).horizontalAdvance("−60 dB") + 6
        for row, (name, color) in enumerate((("Before", theme.MUTED), ("After", theme.ACCENT))):
            level = levels[row]
            self._held[row] = max(level, self._held[row] - 1.5)  # the peak falls back gently
            y = box.top() + row * box.height() / 2
            line = QRectF(box.left(), y, box.width(), box.height() / 2)
            p.setPen(QColor(theme.LABEL))
            p.drawText(QRectF(line.left(), line.top(), label_w, line.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
            track = QRectF(line.left() + label_w, line.center().y() - 3, line.width() - label_w - number_w - 8, 6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.TRACK))
            p.drawRoundedRect(track, 3, 3)
            frac = max(0.0, min(1.0, (level - DB_LOW) / -DB_LOW))
            if frac > 0:
                p.setBrush(QColor(color))
                p.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * frac, track.height()), 3, 3)
            held = max(0.0, min(1.0, (self._held[row] - DB_LOW) / -DB_LOW))
            if held > 0:
                p.setBrush(QColor(theme.TEXT_BRIGHT))
                p.drawRect(QRectF(track.left() + track.width() * held - 1, track.top() - 2, 2, track.height() + 4))
            p.setPen(QColor(theme.TEXT))
            shown = "−∞ dB" if level <= DB_LOW else f"{level:.0f} dB".replace("-", "−")
            p.drawText(QRectF(track.right() + 8, line.top(), number_w, line.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, shown)
        p.end()


class CanvasSwitch(QWidget):
    """Horizontal | Vertical over the filter list, when each canvas has its own filters."""

    def __init__(self, canvases: list[str], picked: Callable[[str], None], parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.buttons: dict[str, QPushButton] = {}
        for n, canvas in enumerate(canvases):
            button = QPushButton(canvas.capitalize())
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            first, last = n == 0, n == len(canvases) - 1
            corners = (("border-top-left-radius: 15px; border-bottom-left-radius: 15px;" if first else "")
                       + ("border-top-right-radius: 15px; border-bottom-right-radius: 15px;" if last else ""))
            button.setStyleSheet(
                f"QPushButton {{ background: {theme.RAISED}; color: {theme.TEXT}; border: none; {corners}"
                f" min-height: 30px; padding: 0 14px; }}"
                f"QPushButton:hover {{ background: {theme.RAISED_HOVER}; }}"
                f"QPushButton:checked {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; font-weight: 650; }}")
            button.clicked.connect(lambda _=False, c=canvas: (self.pick(c), picked(c)))
            lay.addWidget(button, 1)
            self.buttons[canvas] = button
        if canvases:
            self.pick(canvases[0])

    def pick(self, canvas: str | None) -> None:
        for name, button in self.buttons.items():
            button.setChecked(name == canvas)


class FiltersDialog(CardWindow):
    """Edits `source.filters` in place and calls on_change(save=False) after every edit, so the
    preview updates live. Closing calls on_change(save=True).

    The Before and after design (user 2026-11-21): the filters with their icons and switches on the left, the
    source live with its filters off and on (drag the split) above the picked filter's settings on the right.
    grab(source_id, callback, raw, limit) is Engine.grab_source; without it (sound filters, tests) there's no
    picture. Sound filters get a SoundView instead: hear(source_id, filters) makes the audio/preview.py Preview
    that runs the window's draft filters on the sound as it comes in."""

    def __init__(self, source: SourceConfig, on_change: Callable[..., None], parent: QWidget | None = None,
                 media: str | None = None, sounds: list[tuple[str, str]] | None = None, collection=None,
                 grab: Callable | None = None, hear: Callable | None = None):
        self.media = media or media_of(source)  # a browser page has both: the Sound card asks for "audio"
        picture = grab is not None and self.media == "video"
        sound = self.media == "audio"
        live_sound = sound and hear is not None
        super().__init__(f"Filters for “{source.name}”", parent,
                         (920, 640) if picture else (920, 690) if live_sound else (820, 540))
        edge = QColor(theme.ACCENT)
        edge.setAlpha(80)  # a soft green edge, so the window stands off whatever is behind it
        self.card.setStyleSheet(f"#windowCard {{ background: {theme.PANEL}; border: 1px solid {edge.name(QColor.NameFormat.HexArgb)};"
                                " border-radius: 22px; }")
        # Preview first (user 2026-11-21): with a picture, the window edits a copy. Changes show only in the
        # After side until Apply puts them on the source (and the stream); Cancel or × drops them. Sound filters
        # Sound filters too (user 2026-11-21): you hear the draft in the window (Listen After), not on the stream.
        self.real = source
        self.preview_first = picture or sound
        self.source = source.model_copy(deep=True) if self.preview_first else source
        self.applied = False
        self.sounds = [(sid, name) for sid, name in (sounds or []) if sid != source.id]
        # A placed sprig has a placement on each canvas: its filters follow there, unless it keeps its own.
        self.collection = collection if collection is not None and collection.filter_twins(source) else None
        # A source placed on both canvases (one page, capture or video: it runs once) may have each canvas's
        # own picture filters ("Same on both canvases" off). self.canvas is the canvas the list edits.
        showing = (collection.canvases_showing(source) if collection is not None and self.collection is None
                   and self.media == "video" and collection.part_of(source) is None else [])
        self.canvas_col = collection if len(showing) > 1 else None
        self.canvases = showing if self.canvas_col is not None else []
        self.main_canvas = collection.main_canvas() if self.canvas_col is not None else None
        self.canvas = self.main_canvas
        self._on_change = on_change

        def changed(save: bool = False) -> None:
            if self.preview_first:
                self.apply_button.setEnabled(True)  # something to apply
            else:
                if self.collection is not None:
                    self.collection.share_filters(source)
                on_change(save=save)
            if self.preview is not None:
                self.preview.ask()  # show the change now, not on the next tick
            if self.hearing is not None:
                self.hearing.set_filters(self._draft())  # heard (and drawn) from the next block

        self.preview: BeforeAfter | None = None
        self.hearing = None  # audio/preview.py's Preview, for sound filters
        self.sound_view: SoundView | None = None
        self.on_change = changed
        self.form: FilterForm | None = None
        from sprigcast.app import icons
        from sprigcast.app.audio_page import _pick_style

        # Header: what this is, whose filters, and a close button (a frameless window has no title bar ×).
        heading = QLabel("Filters")
        heading.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 17px; font-weight: 650; background: transparent;")
        whose = QLabel(source.name)
        whose.setStyleSheet(f"color: {theme.MUTED}; font-size: 15px; background: transparent;")
        close = QPushButton()
        close.setIcon(icons.icon("close", theme.MUTED, 18))
        close.setToolTip("Close")
        close.setAccessibleName("Close")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; border-radius: 15px; min-width: 30px;"
            f" max-width: 30px; min-height: 30px; max-height: 30px; padding: 0; }}"
            f"QPushButton:hover {{ background: {theme.RAISED_HOVER}; }}")
        close.clicked.connect(self.reject)
        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(heading)
        header.addWidget(whose)
        header.addStretch(1)
        header.addWidget(close)

        self.list = QListWidget()
        self.list.setFixedWidth(236)
        self.list.setItemDelegate(FilterRow(self.list))
        self.list.setMouseTracking(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setStyleSheet("QListWidget { background: transparent; border: none; outline: none; }")
        if self.media == "audio":  # sound filters run top to bottom; effects run in a fixed order on the GPU
            self.list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
            self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
            self.list.model().rowsMoved.connect(self._dragged)
        self.list.currentRowChanged.connect(self._show_selected)
        self.list.itemChanged.connect(self._toggled)
        from PySide6.QtGui import QKeySequence, QShortcut

        rename = QShortcut(QKeySequence(Qt.Key.Key_F2), self)  # F2 renames the picked filter, as in the library
        rename.setContext(Qt.ShortcutContext.WindowShortcut)
        rename.activated.connect(self.rename_selected)
        for keys, slot in (("Ctrl+C", self.copy_selected), ("Ctrl+Shift+C", self.copy_all),
                           ("Ctrl+V", self.paste)):
            key = QShortcut(QKeySequence(keys), self)  # a text box being typed in keeps its own Ctrl+C and V
            key.setContext(Qt.ShortcutContext.WindowShortcut)
            key.activated.connect(slot)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        self.picker = AddPicker(self)
        self.picker.picked.connect(self.add)
        self.add_button = add = QPushButton("Add filter")
        add.setIcon(icons.icon("plus", theme.TEXT, 16))
        add.setStyleSheet(RAISED_PILL)
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(self._open_picker)
        self.empty = QLabel("No filters yet")
        self.empty.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)  # Paste into an empty list too
        self.empty.customContextMenuRequested.connect(
            lambda pos: self._list_menu(self.list.viewport().mapFromGlobal(self.empty.mapToGlobal(pos))))
        self.same = QCheckBox("Same on both canvases")
        self.same.setCursor(Qt.CursorShape.PointingHandCursor)
        self.same.setVisible(self.collection is not None or self.canvas_col is not None)
        if self.canvas_col is not None:
            self.same.setChecked(not any(c in source.canvas_filters for c in self.canvases))
        else:
            self.same.setChecked(self.collection is None or self.collection.shares_filters(source))
        self.same.toggled.connect(self._same_toggled)
        self.canvas_switch = CanvasSwitch(self.canvases, self._pick_canvas)
        self.canvas_switch.setVisible(self.canvas_col is not None and not self.same.isChecked())
        # Each part of the window on a card of its own (user 2026-11-25: "they all blend into one").
        self.list_card = QFrame()
        self.list_card.setObjectName("filterListCard")
        self.list_card.setStyleSheet(SECTION_STYLE.format(name="filterListCard"))
        left = QVBoxLayout(self.list_card)
        left.setContentsMargins(12, 14, 12, 14)
        self.list_card.setFixedWidth(236 + 24)  # the list's width, whether or not there are filters yet
        left.setSpacing(10)
        left.addWidget(self.canvas_switch)
        left.addWidget(self.list, 1)
        left.addWidget(self.empty, 1)
        left.addWidget(add)
        left.addWidget(self.same)

        # The picked filter's name (and a plugin's note), over its settings.
        self.title = QLabel("")
        self.title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 15px; font-weight: 650; background: transparent;")
        # A plugin filter that reports how it's doing ("Downloading 40%"): its note beside the name, kept fresh.
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._show_status)
        self.form_area = QScrollArea()
        self.form_area.setWidgetResizable(True)
        self.form_area.setFrameShape(QScrollArea.Shape.NoFrame)
        self.form_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.form_area.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.reset_button = QPushButton("Defaults")
        self.reset_button.setStyleSheet(RAISED_PILL)
        self.reset_button.setToolTip("Put this filter's settings back to their defaults")
        self.reset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_button.clicked.connect(self._reset)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setIcon(icons.icon("trash", theme.TEXT, 15))
        self.remove_button.setStyleSheet(RAISED_PILL)
        self.remove_button.setToolTip("Remove this filter")
        self.remove_button.setAccessibleName("Remove the selected filter")
        self.remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_button.clicked.connect(self._remove)
        self.save_look_button = QPushButton("Save as look…")
        self.save_look_button.setStyleSheet(RAISED_PILL)
        self.save_look_button.setToolTip("Save these color settings as a .cube LUT you can use anywhere")
        self.save_look_button.clicked.connect(self._save_look)
        self.save_look_button.hide()
        self.settings_box = card = QWidget()  # the right side: picture, name, settings, buttons
        card.setObjectName("filterSettings")
        card.setStyleSheet(NUMBER_STYLE + _pick_style("filterPick"))
        right = QVBoxLayout(card)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(14)
        if picture:
            self.preview = BeforeAfter(source.id, grab,
                                       draft=lambda: [f.model_copy(deep=True) for f in self._drawn_filters()])
            right.addWidget(self.preview)
        if live_sound:
            try:
                self.hearing = hear(source.id, self._draft())
                self.hearing.start()
            except Exception:  # noqa: BLE001 - the window still edits; there's just nothing to hear
                import logging

                logging.getLogger(__name__).exception("filters: can't hear %s", source.id)
                self.hearing = None
        if self.hearing is not None:
            self.sound_view = SoundView(self.hearing, self._current)
            right.addWidget(self.sound_view)
        self.form_card = QFrame()
        self.form_card.setObjectName("filterFormCard")
        self.form_card.setStyleSheet(SECTION_STYLE.format(name="filterFormCard"))
        form_box = QVBoxLayout(self.form_card)
        form_box.setContentsMargins(18, 14, 14, 14)
        form_box.setSpacing(12)
        title_row = QHBoxLayout()
        title_row.addWidget(self.title, 1)
        title_row.addWidget(self.save_look_button)
        title_row.addWidget(self.reset_button)
        title_row.addWidget(self.remove_button)
        form_box.addLayout(title_row)
        form_box.addWidget(self.form_area, 1)
        right.addWidget(self.form_card, 1)

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self.list_card, 0)
        body.addWidget(card, 1)
        root = QVBoxLayout(self.card)
        root.setContentsMargins(24, 20, 24, 22)
        root.setSpacing(16)
        root.addLayout(header)
        root.addLayout(body, 1)
        self.apply_button = QPushButton("Apply")
        self.apply_button.setStyleSheet(theme.PRIMARY_STYLE)
        self.apply_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply)
        if self.preview_first:
            cancel = QPushButton("Cancel")
            cancel.setStyleSheet(RAISED_PILL)
            cancel.setCursor(Qt.CursorShape.PointingHandCursor)
            cancel.clicked.connect(self.reject)
            footer = QHBoxLayout()
            footer.addStretch(1)
            footer.addWidget(cancel)
            footer.addWidget(self.apply_button)
            root.addLayout(footer)
        self._refresh(0)

    def _draft(self) -> list[Filter]:
        return [f.model_copy(deep=True) for f in self.source.filters]

    def _editing(self) -> list[Filter]:
        """The list this window edits: the source's own, or the picked canvas's own list."""
        if self.canvas is not None and self.canvas != self.main_canvas and self.canvas in self.source.canvas_filters:
            return self.source.canvas_filters[self.canvas]
        return self.source.filters

    def _drawn_filters(self) -> list[Filter]:
        """What the picked canvas draws the source with (its own list and the shared ones)."""
        if self.canvas_col is None:
            return self.source.filters
        return self.canvas_col.filters_for(self.source, self.canvas)

    def _own_canvas(self) -> bool:
        return self._editing() is not self.source.filters

    def _pick_canvas(self, canvas_id: str) -> None:
        self.canvas = canvas_id
        self._refresh(0)
        if self.preview is not None:
            self.preview.ask()

    def apply(self) -> None:
        """Put the previewed filters on the source (and so on the stream), save, and close."""
        self.real.filters = [f.model_copy(deep=True) for f in self.source.filters]
        self.real.canvas_filters = {c: [f.model_copy(deep=True) for f in fl]
                                    for c, fl in self.source.canvas_filters.items()}
        if self.collection is not None:
            self.collection.share_filters(self.real)
        self.applied = True
        self.accept()

    # List ---------------------------------------------------------------------

    def _shown(self) -> list[Filter]:
        """This window's filters: sound filters in Sound filters, effects in Effects (a page has both)."""
        own = media_of(self.source)

        def media(flt: Filter) -> str:
            info = filter_kind(flt.kind)
            return info.media if info is not None else own  # from a plugin that isn't loaded

        return [f for f in self._editing() if media(f) == self.media]

    def _refresh(self, select: int | None = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        shown = self._shown()
        for flt in shown:
            item = QListWidgetItem(shown_name(flt.kind, flt.name))
            item.setData(Qt.ItemDataRole.UserRole, flt.id)
            item.setData(Qt.ItemDataRole.UserRole + 1, flt.kind)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if flt.enabled else Qt.CheckState.Unchecked)
            item.setToolTip("Drag to reorder" if self.media == "audio" else "")
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.empty.setVisible(not shown)
        self.list.setVisible(bool(shown))
        if shown:
            self.list.setCurrentRow(max(0, min(select or 0, len(shown) - 1)))
        self._show_selected(self.list.currentRow())

    def _fill_add_menu(self) -> None:
        """The Add filter picker's tiles: this window's kind of filter, plugins too."""
        kinds = filter_kinds(self.media)
        if self._own_canvas():  # a canvas's own list: not the ones that change the source itself
            kinds = [k for k in kinds if not canvas_shared(k.kind)]
        self.picker.fill(kinds, {f.kind for f in self._editing()}, self.media)

    def _open_picker(self) -> None:
        self._fill_add_menu()
        self.picker.open_below(self.add_button)

    def add(self, kind: str) -> Filter:
        info = filter_kind(kind)
        names = {f.name for f in self._editing()}
        name, n = info.label, 2
        while name in names:
            name, n = f"{info.label} {n}", n + 1
        flt = Filter(id=new_filter_id(), kind=kind, name=name, settings=dict(info.defaults))
        self._editing().append(flt)
        self._refresh(len(self._shown()) - 1)
        self.on_change(save=False)
        return flt

    def rename_selected(self) -> bool:
        """F2 or a double-click: name the picked filter (the window's own copy; Apply keeps it)."""
        from sprigcast.app import card as popups

        flt = self._current()
        if flt is None:
            return False
        text, ok = popups.ask_text(self, "Rename filter", "Filter name", shown_name(flt.kind, flt.name), "Rename")
        text = text.strip()
        if not ok or not text or text == shown_name(flt.kind, flt.name):
            return False
        flt.name = text
        self._refresh(self.list.currentRow())
        self.on_change(save=False)
        return True

    def copy_selected(self) -> None:
        flt = self._current()
        if flt is not None:
            copy_filters([flt])

    def copy_all(self) -> None:
        if self._shown():
            copy_filters(self._shown())

    def paste(self) -> int:
        """The copied filters into this window's list (Apply still puts them on the stream)."""
        pasted = paste_filters(self._editing(), {self.media}, self._own_canvas())
        if pasted:
            self._refresh(len(self._shown()) - 1)
            self.on_change(save=False)
        return pasted

    def _list_menu(self, pos) -> None:
        menu = self.list_menu(pos)
        if menu.actions():
            theme.ui_burst()
            menu.exec(self.list.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def list_menu(self, pos):
        """The filter list's right-click menu: Copy (the filter under the pointer), Copy all, Paste."""
        from PySide6.QtGui import QKeySequence
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)

        def entry(text: str, keys: str, slot) -> None:
            action = menu.addAction(text)
            action.setShortcut(QKeySequence(keys))
            action.setShortcutVisibleInContextMenu(True)
            action.triggered.connect(lambda _=False: slot())

        on = self.list.itemAt(pos)
        if on is not None:
            self.list.setCurrentItem(on)
            entry("Copy", "Ctrl+C", self.copy_selected)
        if self._shown():
            entry("Copy all", "Ctrl+Shift+C", self.copy_all)
        count = len(pasteable({self.media}, self._own_canvas()))
        if count:
            entry("Paste" if count == 1 else f"Paste {count} filters", "Ctrl+V", self.paste)
        return menu

    def _current(self) -> Filter | None:
        row, shown = self.list.currentRow(), self._shown()
        return shown[row] if 0 <= row < len(shown) else None

    def _remove(self) -> None:
        row, flt = self.list.currentRow(), self._current()
        if flt is None:
            return
        self._editing().remove(flt)
        self._refresh(row)
        self.on_change(save=False)

    def _move(self, delta: int) -> None:
        """Swap with the next filter shown here (the other kind, hidden, keeps its place)."""
        row, shown = self.list.currentRow(), self._shown()
        new = row + delta
        if self._current() is None or not 0 <= new < len(shown):
            return
        filters = self._editing()
        a, b = filters.index(shown[row]), filters.index(shown[new])
        filters[a], filters[b] = filters[b], filters[a]
        self._refresh(new)
        self.on_change(save=False)

    def _set_title(self, text: str) -> None:
        """The picked filter's name (and a plugin's note), over its settings."""
        self.title.setText(text)

    def _dragged(self, *_args) -> None:
        """A filter was dragged to a new place: the list's order is the new order (the other kind keeps its
        places among them)."""
        by_id = {f.id: f for f in self._shown()}
        order = [by_id[i] for i in (self.list.item(r).data(Qt.ItemDataRole.UserRole)
                                    for r in range(self.list.count())) if i in by_id]
        filters = self._editing()
        slots = [n for n, f in enumerate(filters) if f.id in by_id]
        if len(order) != len(slots) or [filters[n] for n in slots] == order:
            return
        for slot, flt in zip(slots, order, strict=True):
            filters[slot] = flt
        self.on_change(save=False)

    def _same_toggled(self, on: bool) -> None:
        if self.canvas_col is not None:
            for canvas in self.canvases:
                if canvas != self.main_canvas:
                    self.canvas_col.set_canvas_filters(self.source, canvas, not on)
            self.canvas_switch.setVisible(not on)
            self.canvas_switch.pick(self.main_canvas)
            self.canvas = self.main_canvas
            self._refresh(0)
            self.on_change(save=False)
            return
        if self.collection is not None:
            self.collection.set_shares_filters(self.real, on)
            if self.preview_first:
                self._on_change(save=False)  # a choice about the source, not a filter change: no Apply needed
            else:
                self.on_change(save=False)

    def _toggled(self, item: QListWidgetItem) -> None:
        row, shown = self.list.row(item), self._shown()
        if 0 <= row < len(shown):
            switch(shown[row], item.checkState() == Qt.CheckState.Checked)
            self.on_change(save=False)

    # Settings -----------------------------------------------------------------

    def _show_selected(self, row: int) -> None:
        flt = self._current()
        self.reset_button.setVisible(flt is not None)
        self.remove_button.setVisible(flt is not None)
        # The before and after picture (or the sound view) only once there's a filter to show it for.
        for view in (self.preview, self.sound_view):
            if view is not None:
                view.setVisible(flt is not None)
        if flt is None:
            self.save_look_button.hide()
            self._set_title("")
            if self._shown():
                empty = QLabel("")
            else:
                from sprigcast.app.empty import EmptyState

                empty = EmptyState("Add a filter to start")
            self.form_area.setWidget(empty)
            self.form = None
            return
        info = filter_kind(flt.kind)
        self.save_look_button.setVisible(flt.kind == "color_correction")
        self._set_title(info.label if info else flt.kind)
        self._show_status()
        if info is None:
            self.form_area.setWidget(QLabel(f"The {flt.kind!r} filter comes from a plugin that isn't loaded."))
            self.form = None
            return
        self.form = FilterForm(info.fields, settings_for(flt.kind, flt.settings), sounds=self.sounds)
        self.form.changed.connect(self._edited)
        self.form_area.setWidget(self.form)

    def _show_status(self) -> None:
        flt = self._current()
        info = filter_kind(flt.kind) if flt is not None else None
        if info is None or info.status is None:
            self._status_timer.stop()
            return
        try:
            note = info.status(self.source.id)
        except Exception:  # a plugin's note: never break the window for it
            note = ""
        self._set_title(f"{info.label}  ·  {note}" if note else info.label)
        if not self._status_timer.isActive():
            self._status_timer.start()

    def _edited(self) -> None:
        flt = self._current()
        if flt is not None and self.form is not None:
            flt.settings = {**_unshown(flt), **self.form.values()}
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
        self._set_title(f"{filter_kind(flt.kind).label}  ·  saved {saved.name}")
        return saved

    def _reset(self) -> None:
        flt = self._current()
        info = filter_kind(flt.kind) if flt else None
        if info is None or self.form is None:
            return
        self.form.set_values(info.defaults)
        flt.settings = {**_unshown(flt), **info.defaults}
        self.on_change(save=False)

    def done(self, result: int) -> None:  # Qt override
        if self.preview is not None:
            self.preview.timer.stop()
            stop = getattr(self.preview.fetch, "stop", None)
            if stop is not None:
                stop()  # let go of the native view a Backdrop's After came from
        if self.sound_view is not None:
            self.sound_view.timer.stop()
        if self.hearing is not None:
            hearing, self.hearing = self.hearing, None
            import threading

            # it lets go of the sound at once; closing the Listen device can take a moment: not on the UI thread
            threading.Thread(target=hearing.stop, name="sprigcast-filter-preview-stop", daemon=True).start()
        if self.preview_first:
            self._on_change(save=True)  # applied filters, or nothing changed (Cancel): either way the garden is saved
        else:
            self.on_change(save=True)
        super().done(result)
