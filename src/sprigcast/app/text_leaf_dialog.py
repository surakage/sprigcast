"""The text leaf's own settings window: a live preview on top, then three pages of titled cards.

    ╭ Deaths ───────────────────────────────────────╮
    │ ╭───────────────────────────────────────────╮ │
    │ │░░░░░░░░░░  Deaths: 12  ░░░░░░░░░░░░░░░░░░░│ │  drawn by the text source itself, so it's what the
    │ ╰───────────────────────────────────────────╯ │  canvas shows; follows a live file while open
    │ [ Words ][ Style ][ Layout ]                  │
    │ ╭ Text ─────────────────────────────────────╮ │
    │ │ ┌───────────────────────────────────────┐ │ │
    │ │ └───────────────────────────────────────┘ │ │
    │ │ [Date] [Time] [Time 12-hour]              │ │
    │ ╰───────────────────────────────────────────╯ │
    │ ╭ Live file ────────────────────────────────╮ │
    │ │ Read from a file                     ( ) │ │
    │ │ File   C:/…/deaths.txt          Choose…   │ │
    │ │ Show   [All][First line][Last line]       │ │
    │ ╰───────────────────────────────────────────╯ │
    │                           Cancel  [ Save ]    │
    ╰───────────────────────────────────────────────╯

Style: Font (family, size, Bold, Italic, Letters), Fill (color, opacity, gradient), Outline.
Layout: Lines (alignment, wrap, spacing), Shadow, Backing card.
values() gives every key of sources/text.py DEFAULTS.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QCompleter,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.card import BOX_HEIGHT, CardDialog, combo
from sprigcast.app.card import section as card_section
from sprigcast.app.live_stage import Switch
from sprigcast.app.media_dialog import CHIP, Choice
from sprigcast.app.settings_dialog import FORM_STYLE
from sprigcast.sources.text import DEFAULTS

log = logging.getLogger(__name__)

LABEL_WIDTH = 112
ROW_HEIGHT = 34
PREVIEW_HEIGHT = 132
PAGES = ("Words", "Style", "Layout")
TOKENS = (("Date", "{date}"), ("Time", "{time}"), ("Time 12-hour", "{time12}"))  # A to Z
FILE_TYPES = "Text files (*.txt);;All files (*)"
_PLAIN = {"gradient": False, "outline_width": 0, "shadow": False, "box": False, "bold": False, "italic": False}
LOOKS = (  # one click sets the style; A to Z. Everything stays changeable on Style and Layout after.
    ("Bold outline", {**_PLAIN, "bold": True, "color": "#ffffffff", "outline_width": 6, "outline_color": "#000000ff"}),
    ("Canopy card", {**_PLAIN, "color": "#f2f7f3ff", "box": True, "box_color": "#0e1311d9", "box_padding": 18,
                     "box_corners": 18}),
    ("Clean", {**_PLAIN, "color": "#ffffffff"}),
    ("Glow", {**_PLAIN, "color": "#f2fff7ff", "shadow": True, "shadow_color": "#4fd88fe6", "shadow_distance": 0,
              "shadow_blur": 14}),
    ("Sprout", {**_PLAIN, "bold": True, "color": "#ffffffff", "gradient": True, "gradient_color": "#4fd88fff",
                "gradient_direction": "down", "outline_width": 3, "outline_color": "#07130dff", "shadow": True,
                "shadow_color": "#000000b3", "shadow_distance": 4, "shadow_blur": 6}),
)


def _section(title: str) -> tuple[QFrame, QVBoxLayout]:
    return card_section(title, 10)


def _row(box: QVBoxLayout, name: str, *widgets: QWidget, stretch_last: bool = True) -> QWidget:
    """One labeled row in a card; returns the row so it can be hidden or dimmed as a whole."""
    holder = QWidget()
    holder.setStyleSheet("background: transparent;")
    line = QHBoxLayout(holder)
    line.setContentsMargins(0, 0, 0, 0)
    line.setSpacing(10)
    label = QLabel(name)
    label.setStyleSheet(f"color: {theme.LABEL}; background: transparent;")
    label.setFixedWidth(LABEL_WIDTH)
    label.setMinimumHeight(ROW_HEIGHT)
    line.addWidget(label)
    for i, widget in enumerate(widgets):
        grow = stretch_last and i == len(widgets) - 1 and not isinstance(widget, Switch)
        line.addWidget(widget, 1 if grow else 0)
    if not any(isinstance(w, (QLineEdit, QComboBox, QSlider, Choice)) for w in widgets) or isinstance(widgets[-1],
                                                                                                     Switch):
        line.addStretch(1)
    box.addWidget(holder)
    return holder


def _spin(value: Any, low: int, high: int, suffix: str = " px") -> QSpinBox:
    box = QSpinBox()
    box.setRange(low, high)
    box.setSuffix(suffix)
    try:
        box.setValue(int(value))
    except (TypeError, ValueError):
        box.setValue(low)
    box.setFixedWidth(104)
    box.setMinimumHeight(BOX_HEIGHT)
    return box


def _rgba(text: str) -> QColor:
    text = str(text or "").strip()
    color = QColor(text[:7]) if text.startswith("#") else QColor("white")
    if len(text) == 9:
        try:
            color.setAlpha(int(text[7:], 16))
        except ValueError:
            pass
    return color if color.isValid() else QColor("white")


def _hex(color: QColor) -> str:
    return "#{:02x}{:02x}{:02x}{:02x}".format(color.red(), color.green(), color.blue(), color.alpha())


class Swatch(QPushButton):
    """A round color well and its hex; clicking it opens the color picker (with opacity)."""

    def __init__(self, value: str, on_change, parent: QWidget | None = None):
        super().__init__(parent)
        self.color = _rgba(value)
        self.on_change = on_change
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(BOX_HEIGHT)
        self.setMinimumWidth(150)
        self.setAutoDefault(False)
        self.setStyleSheet(theme.button_style(17, "6px 14px 6px 40px") + "QPushButton { text-align: left; }")
        self.clicked.connect(self._pick)
        self._show()

    def _show(self) -> None:
        self.setText(_hex(self.color)[:7].upper())
        self.update()

    def value(self) -> str:
        return _hex(self.color)

    def set_value(self, value: str) -> None:
        self.color = _rgba(value)
        self._show()

    def set_alpha(self, alpha: int) -> None:
        self.color.setAlpha(max(0, min(255, alpha)))
        self.update()

    def _pick(self) -> None:
        picked = QColorDialog.getColor(self.color, self, "Color", QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if picked.isValid():
            self.color = picked
            self._show()
            self.on_change()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        well = QRectF(10, (self.height() - 20) / 2, 20, 20)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(well)
        p.setBrush(QColor("#c8c8c8"))  # a tiny checker under see-through colors
        p.drawPie(well, 0, 90 * 16)
        p.drawPie(well, 180 * 16, 90 * 16)
        p.setBrush(self.color)
        p.setPen(QColor(theme.BUTTON_EDGE))
        p.drawEllipse(well)
        p.end()


class Preview(QWidget):
    """The text as the canvas will show it, fitted into a rounded checkerboard well."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(PREVIEW_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.picture: QImage | None = None
        self.missing = False

    def show_frame(self, frame, missing: bool = False) -> None:
        if frame is None:
            self.picture = None
        else:
            h, w = frame.shape[:2]
            self.picture = QImage(frame.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
        self.missing = missing
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        clip = QPainterPath()
        clip.addRoundedRect(rect, 16, 16)
        p.setClipPath(clip)
        cell = 12
        dark, light = QColor("#151b18"), QColor("#1b2420")
        for y in range(0, self.height(), cell):
            for x in range(0, self.width(), cell):
                p.fillRect(x, y, cell, cell, light if (x // cell + y // cell) % 2 else dark)
        if self.picture is not None and not self.picture.isNull():
            room = rect.adjusted(18, 14, -18, -14)
            pw, ph = self.picture.width(), self.picture.height()
            k = min(1.0, room.width() / max(1, pw), room.height() / max(1, ph))
            w, h = pw * k, ph * k
            target = QRectF(room.center().x() - w / 2, room.center().y() - h / 2, w, h)
            p.drawImage(target, self.picture)
        if self.missing:
            p.setPen(QColor(theme.WARN))
            p.drawText(rect.adjusted(14, 8, -14, -8), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                       "File not found")
        p.setClipping(False)
        p.setPen(QColor(theme.EDGE_STRONG))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 16, 16)
        p.end()


class TextLeafDialog(CardDialog):
    """New or change a text leaf. values() gives its settings."""

    def __init__(self, title: str, settings: dict[str, Any], parent: QWidget | None = None, primary: str = "Save"):
        super().__init__(title, parent, width=640)
        self.setStyleSheet(FORM_STYLE)
        s = {**DEFAULTS, **{k: v for k, v in settings.items() if k in DEFAULTS}}
        self._opened_with = dict(s)  # for the log line on Save
        self.switches: dict[str, Switch] = {}
        self.rows: dict[str, QWidget] = {}

        self.preview = Preview()
        self.body.addWidget(self.preview)

        self.pages = QStackedWidget()
        self.page_picker = Choice(tuple((i, name) for i, name in enumerate(PAGES)), 0)
        self.page_picker.group.idClicked.connect(self.pages.setCurrentIndex)
        self.body.addWidget(self.page_picker)
        self.body.addWidget(self.pages)
        self.pages.addWidget(self._words_page(s))
        self.pages.addWidget(self._style_page(s))
        self.pages.addWidget(self._layout_page(s))

        self.ok = self.add_buttons(primary)
        self._redraw = QTimer(self)
        self._redraw.setSingleShot(True)
        self._redraw.setInterval(120)  # typing redraws after a short pause, not on every key
        self._redraw.timeout.connect(self._draw_preview)
        self._follow = QTimer(self)
        self._follow.setInterval(250)  # a followed file changing shows here too, as it will on the canvas
        self._follow.timeout.connect(self._check_file)
        self._source = None
        self._fit_rows()
        self._draw_preview()
        self._follow.start()
        self.text.setFocus()
        self.text.selectAll()

    # Pages ----------------------------------------------------------------------------

    def _words_page(self, s: dict[str, Any]) -> QWidget:
        page, layout = self._page()
        card, box = _section("Text")
        self.text = QPlainTextEdit(str(s["text"]))
        self.text.setFixedHeight(84)
        self.text.setTabChangesFocus(True)
        self.text.textChanged.connect(self._changed)
        box.addWidget(self.text)
        tokens = QHBoxLayout()
        tokens.setSpacing(6)
        for name, token in TOKENS:
            chip = QPushButton(name)
            chip.setStyleSheet(CHIP)
            chip.setAutoDefault(False)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _=False, t=token: self._insert(t))
            tokens.addWidget(chip)
        tokens.addStretch(1)
        box.addLayout(tokens)
        self.text_card = card
        layout.addWidget(card)

        card, box = _section("Looks")
        looks = QHBoxLayout()
        looks.setSpacing(6)
        for name, look in LOOKS:
            chip = QPushButton(name)
            chip.setStyleSheet(CHIP)
            chip.setAutoDefault(False)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)  # share the width evenly
            chip.setMinimumWidth(chip.sizeHint().width())
            chip.clicked.connect(lambda _=False, v=look: self.set_values(v))
            looks.addWidget(chip, 1)
        box.addLayout(looks)
        layout.addWidget(card)

        card, box = _section("Live file")
        self.read_file = self._switch("read_file", bool(s["read_file"]) and bool(str(s["file"]).strip()))
        _row(box, "Read from a file", self.read_file)
        self.file = QLineEdit(str(s["file"] or ""))
        self.file.setPlaceholderText("deaths.txt")
        self.file.setMinimumHeight(BOX_HEIGHT)
        self.file.setCursorPosition(0)
        self.file.textChanged.connect(self._changed)
        browse = QPushButton("Choose…")
        browse.setStyleSheet(CHIP)
        browse.setAutoDefault(False)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self._pick_file)
        self.rows["file"] = _row(box, "File", self.file, browse, stretch_last=False)
        self.file_lines = Choice((("all", "All"), ("first", "First line"), ("last", "Last line")),
                                 str(s["file_lines"]))
        self.file_lines.group.idClicked.connect(self._changed)
        self.rows["file_lines"] = _row(box, "Show", self.file_lines)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _style_page(self, s: dict[str, Any]) -> QWidget:
        from sprigcast.sources import fonts

        page, layout = self._page()
        card, box = _section("Font")
        self.font_box = combo(16)
        self.font_box.setEditable(True)
        self.font_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        current = str(s["font"])
        current_family = fonts.family_of(current)
        for family, styles in fonts.families().items():
            self.font_box.addItem(family, styles["regular"])
        index = self.font_box.findText(current_family) if current_family else -1
        if index < 0:
            self.font_box.insertItem(0, current, current)
            index = 0
        self.font_box.setCurrentIndex(index)
        self._opened_font_index = index
        completer = QCompleter([self.font_box.itemText(i) for i in range(self.font_box.count())], self.font_box)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)  # "mono" finds every mono font
        self.font_box.setCompleter(completer)
        self.font_box.currentIndexChanged.connect(self._changed)
        _row(box, "Family", self.font_box)
        self.size = _spin(s["size"], 4, 2000)
        self.size.valueChanged.connect(self._changed)
        self.bold = self._toggle("Bold", bool(s["bold"]))
        self.italic = self._toggle("Italic", bool(s["italic"]))
        _row(box, "Size", self.size, self.bold, self.italic, stretch_last=False)
        self.letter_case = Choice((("as_typed", "As typed"), ("lower", "abc"), ("upper", "ABC")),
                                  str(s["letter_case"]))
        self.letter_case.group.idClicked.connect(self._changed)
        _row(box, "Letters", self.letter_case)
        layout.addWidget(card)

        card, box = _section("Fill")
        self.color = Swatch(str(s["color"]), self._color_picked)
        self.opacity = self._slider(round(_rgba(str(s["color"])).alpha() * 100 / 255), 0, 100)
        self.opacity_note = self._note()
        self.opacity.valueChanged.connect(self._opacity_moved)
        _row(box, "Color", self.color, stretch_last=False)
        _row(box, "Opacity", self.opacity, self.opacity_note, stretch_last=False)
        self.gradient = self._switch("gradient", bool(s["gradient"]))
        _row(box, "Gradient", self.gradient)
        self.gradient_color = Swatch(str(s["gradient_color"]), self._changed)
        self.gradient_direction = Choice((("down", "Down"), ("across", "Across")), str(s["gradient_direction"]))
        self.gradient_direction.group.idClicked.connect(self._changed)
        self.rows["gradient"] = _row(box, "Fades to", self.gradient_color, self.gradient_direction)
        layout.addWidget(card)

        card, box = _section("Outline")
        self.outline_width = self._slider(int(s["outline_width"] or 0), 0, 24)
        self.outline_note = self._note()
        self.outline_width.valueChanged.connect(self._changed)
        _row(box, "Width", self.outline_width, self.outline_note, stretch_last=False)
        self.outline_color = Swatch(str(s["outline_color"]), self._changed)
        self.rows["outline"] = _row(box, "Color", self.outline_color, stretch_last=False)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    def _layout_page(self, s: dict[str, Any]) -> QWidget:
        page, layout = self._page()
        card, box = _section("Lines")
        self.align = Choice((("left", "Left"), ("center", "Center"), ("right", "Right")), str(s["align"]))
        self.align.group.idClicked.connect(self._changed)
        _row(box, "Alignment", self.align)
        self.wrap = self._switch("wrap", int(s["wrap_width"] or 0) > 0)
        self.wrap_width = _spin(int(s["wrap_width"] or 0) or 600, 40, 8000)
        self.wrap_width.valueChanged.connect(self._changed)
        _row(box, "Wrap", self.wrap, self.wrap_width, stretch_last=False)
        self.line_spacing = _spin(s["line_spacing"], -100, 400)
        self.line_spacing.valueChanged.connect(self._changed)
        _row(box, "Line spacing", self.line_spacing, stretch_last=False)
        layout.addWidget(card)

        card, box = _section("Shadow")
        self.shadow = self._switch("shadow", bool(s["shadow"]))
        self.shadow_color = Swatch(str(s["shadow_color"]), self._changed)
        _row(box, "Shadow", self.shadow, self.shadow_color, stretch_last=False)
        self.shadow_distance = _spin(s["shadow_distance"], 0, 200)
        self.shadow_blur = _spin(s["shadow_blur"], 0, 100)
        for spin in (self.shadow_distance, self.shadow_blur):
            spin.valueChanged.connect(self._changed)
        blur_label = QLabel("Blur")
        blur_label.setStyleSheet(f"color: {theme.LABEL}; background: transparent; padding-left: 8px;")
        self.rows["shadow"] = _row(box, "Distance", self.shadow_distance, blur_label, self.shadow_blur,
                                   stretch_last=False)
        layout.addWidget(card)

        card, box = _section("Backing card")
        self.box = self._switch("box", bool(s["box"]))
        self.box_color = Swatch(str(s["box_color"]), self._changed)
        _row(box, "Card", self.box, self.box_color, stretch_last=False)
        self.box_padding = _spin(s["box_padding"], 0, 400)
        self.box_corners = _spin(s["box_corners"], 0, 400)
        for spin in (self.box_padding, self.box_corners):
            spin.valueChanged.connect(self._changed)
        corners_label = QLabel("Corners")
        corners_label.setStyleSheet(f"color: {theme.LABEL}; background: transparent; padding-left: 8px;")
        self.rows["box"] = _row(box, "Padding", self.box_padding, corners_label, self.box_corners,
                                stretch_last=False)
        layout.addWidget(card)
        layout.addStretch(1)
        return page

    # Pieces ---------------------------------------------------------------------------

    @staticmethod
    def _page() -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        return page, layout

    def _switch(self, key: str, on: bool) -> Switch:
        switch = Switch()
        switch.setChecked(on)
        switch.setCursor(Qt.CursorShape.PointingHandCursor)
        switch.toggled.connect(self._changed)
        self.switches[key] = switch
        return switch

    def _toggle(self, text: str, on: bool) -> QPushButton:
        chip = QPushButton(text)
        chip.setCheckable(True)
        chip.setChecked(on)
        chip.setStyleSheet(CHIP)
        chip.setAutoDefault(False)
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        chip.toggled.connect(self._changed)
        return chip

    @staticmethod
    def _slider(value: int, low: int, high: int) -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.setValue(max(low, min(high, value)))
        slider.setCursor(Qt.CursorShape.PointingHandCursor)
        return slider

    @staticmethod
    def _note() -> QLabel:
        label = QLabel()
        label.setFixedWidth(52)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        label.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        return label

    def _insert(self, token: str) -> None:
        self.text.insertPlainText(token)
        self.text.setFocus()

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a text file", self.file.text(), FILE_TYPES)
        if path:
            self.file.setText(path)
            self.file.setCursorPosition(0)
            self.read_file.setChecked(True)

    def _color_picked(self) -> None:
        self.opacity.blockSignals(True)
        self.opacity.setValue(round(self.color.color.alpha() * 100 / 255))
        self.opacity.blockSignals(False)
        self._changed()

    def _opacity_moved(self, value: int) -> None:
        self.color.set_alpha(round(value * 255 / 100))
        self._changed()

    def _fit_rows(self) -> None:
        """Rows that only mean something while their switch is on are dimmed while it's off."""
        reading = self.read_file.isChecked()
        self.rows["file_lines"].setEnabled(reading)
        self.text_card.setEnabled(not reading)  # a file's words replace the text
        self.rows["gradient"].setEnabled(self.gradient.isChecked())
        self.rows["outline"].setEnabled(self.outline_width.value() > 0)
        self.wrap_width.setEnabled(self.wrap.isChecked())
        self.shadow_color.setEnabled(self.shadow.isChecked())
        self.rows["shadow"].setEnabled(self.shadow.isChecked())
        self.box_color.setEnabled(self.box.isChecked())
        self.rows["box"].setEnabled(self.box.isChecked())
        self.opacity_note.setText(f"{self.opacity.value()}%")
        self.outline_note.setText(f"{self.outline_width.value()} px" if self.outline_width.value() else "Off")

    def _changed(self, *_args) -> None:
        self._fit_rows()
        self._redraw.start()

    # Preview --------------------------------------------------------------------------

    def _preview_source(self):
        if self._source is None:
            from sprigcast.sources.text import TextSource

            self._source = TextSource("preview", self.values())
        return self._source

    def _draw_preview(self) -> None:
        source = self._preview_source()
        source.update(self.values())
        source._follow_file()
        try:
            with source._lock:
                source._render()
            frame = source._frame
        except Exception:  # a font that can't be drawn: the canvas falls back the same way; keep the last preview
            return
        self.preview.show_frame(frame, source.status == "file not found")

    def _check_file(self) -> None:
        source = self._source
        if source is None or not self.read_file.isChecked():
            return
        before = source._file_version
        source._follow_file()
        if source._file_version != before:
            self._draw_preview()

    def accept(self) -> None:
        changed = sorted(k for k, v in self.values().items() if v != self._opened_with.get(k))
        log.info("text leaf %r: Save, changed %s", self.windowTitle(), ", ".join(changed) or "nothing")
        super().accept()

    def done(self, result: int) -> None:  # noqa: D401 (Qt naming)
        self._follow.stop()
        self._redraw.stop()
        if self._source is not None:
            self._source.stop()  # stops following the file for the preview
        super().done(result)

    # Result ---------------------------------------------------------------------------

    def set_values(self, values: dict[str, Any]) -> None:
        """Show these settings in the form (a look, say); the preview follows."""
        switches = {"gradient": self.gradient, "shadow": self.shadow, "box": self.box, "read_file": self.read_file}
        swatches = {"color": self.color, "gradient_color": self.gradient_color, "outline_color": self.outline_color,
                    "shadow_color": self.shadow_color, "box_color": self.box_color}
        spins = {"size": self.size, "line_spacing": self.line_spacing, "shadow_distance": self.shadow_distance,
                 "shadow_blur": self.shadow_blur, "box_padding": self.box_padding, "box_corners": self.box_corners}
        choices = {"letter_case": self.letter_case, "gradient_direction": self.gradient_direction,
                   "align": self.align, "file_lines": self.file_lines}
        for key, value in values.items():
            if key in switches:
                switches[key].setChecked(bool(value))
            elif key in swatches:
                swatches[key].set_value(str(value))
            elif key in spins:
                spins[key].setValue(int(value))
            elif key in choices:
                choices[key].set_value(value)
            elif key == "bold":
                self.bold.setChecked(bool(value))
            elif key == "italic":
                self.italic.setChecked(bool(value))
            elif key == "outline_width":
                self.outline_width.setValue(int(value))
        if "color" in values:
            self._color_picked()
        self._changed()

    def values(self) -> dict[str, Any]:
        font = self.font_box.currentData()
        typed = self.font_box.currentText().strip()
        if self.font_box.findText(typed) < 0 and typed:  # a font file typed in by name
            font = typed
        opened = str(self._opened_with["font"])
        if self.font_box.currentIndex() == self._opened_font_index and typed == self.font_box.itemText(
                self._opened_font_index):
            font = opened  # the same family as it opened with: its file stays as it was written
        return {
            "text": self.text.toPlainText(),
            "font": str(font or DEFAULTS["font"]),
            "size": self.size.value(),
            "bold": self.bold.isChecked(),
            "italic": self.italic.isChecked(),
            "letter_case": self.letter_case.value(),
            "color": self.color.value(),
            "gradient": self.gradient.isChecked(),
            "gradient_color": self.gradient_color.value(),
            "gradient_direction": self.gradient_direction.value(),
            "outline_width": self.outline_width.value(),
            "outline_color": self.outline_color.value(),
            "align": self.align.value(),
            "wrap_width": self.wrap_width.value() if self.wrap.isChecked() else 0,
            "line_spacing": self.line_spacing.value(),
            "shadow": self.shadow.isChecked(),
            "shadow_color": self.shadow_color.value(),
            "shadow_distance": self.shadow_distance.value(),
            "shadow_blur": self.shadow_blur.value(),
            "box": self.box.isChecked(),
            "box_color": self.box_color.value(),
            "box_padding": self.box_padding.value(),
            "box_corners": self.box_corners.value(),
            "file": self.file.text().strip(),
            "read_file": self.read_file.isChecked(),
            "file_lines": self.file_lines.value(),
        }

