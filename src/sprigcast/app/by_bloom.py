"""Change by bloom: a video, image or colour that shows another file or colour in some blooms.

    ╭ File per bloom ──────────────────────────────╮
    │ Change by bloom                         (●)  │
    │ Starting Soon   starting.webm      Choose… × │
    │ Just Chatting   chatting.webm      Choose… × │
    │ [ + Add bloom ]                              │
    ╰──────────────────────────────────────────────╯

values() gives settings["per_bloom_on"] and settings["per_bloom"] ({bloom id: {"path"| "color": ...}}), the keys
SceneCollection.bloom_settings reads. A bloom left out shows the source's own file or colour.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.card import FIELD_STYLE, CardDialog
from sprigcast.app.live_stage import Switch
from sprigcast.core.scene import PER_BLOOM, PER_BLOOM_KEYS, PER_BLOOM_ON

CHIP = theme.button_style(15, "7px 14px")
LABEL_WIDTH = 132
IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"


def section(title: str) -> tuple[QFrame, QVBoxLayout]:
    """A titled card like the video settings' sections."""
    frame = QFrame()
    frame.setObjectName("bloomSection")
    frame.setStyleSheet(f"#bloomSection {{ background: {theme.CARD}; border-radius: 16px; }}")
    box = QVBoxLayout(frame)
    box.setContentsMargins(18, 14, 18, 16)
    box.setSpacing(12)
    heading = QLabel(title)
    heading.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-weight: 600; background: transparent;")
    box.addWidget(heading)
    return frame, box


def name_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
    label.setFixedWidth(LABEL_WIDTH)
    return label


def swatch_style(colour: str) -> str:
    return (f"QPushButton {{ background: {colour[:7]}; border: 1px solid {theme.EDGE_STRONG}; border-radius: 10px;"
            " min-height: 30px; }")


def pick_colour(parent: QWidget, current: str) -> str | None:
    """The colour picker: "#rrggbbaa", or None when closed."""
    colour = QColorDialog.getColor(QColor(str(current or "#000000")[:7]), parent, "Color",
                                   QColorDialog.ColorDialogOption.ShowAlphaChannel)
    if not colour.isValid():
        return None
    return f"#{colour.red():02x}{colour.green():02x}{colour.blue():02x}{colour.alpha():02x}"


class ByBloom(QFrame):
    """The File per bloom (or Color per bloom) card. `kind`: "media", "image" or "color"."""

    def __init__(self, kind: str, blooms: list[tuple[str, str]], settings: dict[str, Any],
                 file_filter: str = IMAGE_FILTER, parent: QWidget | None = None):
        super().__init__(parent)
        self.kind, self.key = kind, PER_BLOOM_KEYS[kind]
        self.blooms = dict(blooms)  # id -> name, in the garden's order
        self.order = [b for b, _ in blooms]
        self.file_filter = file_filter
        self.setObjectName("bloomSection")
        self.setStyleSheet(f"#bloomSection {{ background: {theme.CARD}; border-radius: 16px; }}")
        box = QVBoxLayout(self)
        box.setContentsMargins(18, 14, 18, 16)
        box.setSpacing(12)
        heading = QLabel("Color per bloom" if kind == "color" else "File per bloom")
        heading.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-weight: 600; background: transparent;")
        box.addWidget(heading)
        line = QHBoxLayout()
        line.setSpacing(14)
        line.addWidget(name_label("Change by bloom"))
        self.on = Switch()
        self.on.setChecked(bool(settings.get(PER_BLOOM_ON)))
        self.on.setCursor(Qt.CursorShape.PointingHandCursor)
        self.on.toggled.connect(self._show)
        line.addWidget(self.on)
        line.addStretch(1)
        box.addLayout(line)
        self.list = QVBoxLayout()
        self.list.setSpacing(8)
        box.addLayout(self.list)
        self.add = QPushButton("+ Add bloom")
        self.add.setStyleSheet(CHIP)
        self.add.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add.clicked.connect(self._add_menu)
        box.addWidget(self.add)
        self.rows: dict[str, tuple[QWidget, Callable[[], str]]] = {}
        per = settings.get(PER_BLOOM) or {}
        for bloom in self.order:  # the garden's order, blooms since deleted left out
            value = (per.get(bloom) or {}).get(self.key)
            if value not in (None, ""):
                self._add_row(bloom, str(value))
        self._show()

    def _show(self) -> None:
        on = self.on.isChecked()
        for row, _ in self.rows.values():
            row.setVisible(on)
        self.add.setVisible(on and len(self.rows) < len(self.order))

    def _add_menu(self) -> None:
        menu = QMenu(self)
        for bloom in self.order:
            if bloom not in self.rows:
                menu.addAction(self.blooms[bloom], lambda b=bloom: self._add_row(b, "", pick=True))
        menu.exec(self.add.mapToGlobal(self.add.rect().bottomLeft()))

    def _add_row(self, bloom: str, value: str, pick: bool = False) -> None:
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        line = QHBoxLayout(row)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(8)
        line.addWidget(name_label(self.blooms.get(bloom, bloom)))
        line.addSpacing(6)  # lines up with the rows below (14 between a name and its setting)
        if self.kind == "color":
            held = [value or "#000000ff"]
            swatch = QPushButton()
            swatch.setStyleSheet(swatch_style(held[0]))
            swatch.setCursor(Qt.CursorShape.PointingHandCursor)

            def choose_colour() -> None:
                picked = pick_colour(self, held[0])
                if picked:
                    held[0] = picked
                    swatch.setStyleSheet(swatch_style(picked))

            swatch.clicked.connect(choose_colour)
            line.addWidget(swatch, 1)
            read = lambda: held[0]  # noqa: E731
            choose = choose_colour
        else:
            path = QLineEdit(value)
            path.setPlaceholderText("Main file")
            path.setStyleSheet(FIELD_STYLE)
            path.setMinimumHeight(34)
            path.setCursorPosition(0)
            button = QPushButton("Choose…")
            button.setStyleSheet(CHIP)
            button.setCursor(Qt.CursorShape.PointingHandCursor)

            def choose_file() -> None:
                picked, _ = QFileDialog.getOpenFileName(self, "Choose a file", path.text(), self.file_filter)
                if picked:
                    path.setText(picked)
                    path.setCursorPosition(0)

            button.clicked.connect(choose_file)
            line.addWidget(path, 1)
            line.addWidget(button)
            read = lambda: path.text().strip()  # noqa: E731
            choose = choose_file
        remove = QPushButton("×")
        remove.setStyleSheet(CHIP)
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.setToolTip("Use the main one here")
        remove.clicked.connect(lambda: self._remove(bloom))
        line.addWidget(remove)
        self.list.addWidget(row)
        self.rows[bloom] = (row, read)
        self._show()
        if pick:
            choose()

    def _remove(self, bloom: str) -> None:
        row, _ = self.rows.pop(bloom)
        row.hide()
        row.deleteLater()
        self._show()

    def values(self) -> dict[str, Any]:
        per = {bloom: {self.key: read()} for bloom, (_, read) in self.rows.items() if read()}
        return {PER_BLOOM_ON: bool(self.on.isChecked() and per), PER_BLOOM: per}


def bloom_list(collection) -> list[tuple[str, str]]:
    """The blooms a file or colour can be set for: (id, name), twins of another left out."""
    return [(s.id, s.name) for s in collection.bloom_choices()] if collection is not None else []


class PictureCard:
    """An image's or colour's settings as one card: its file or colour, then File (Color) per bloom."""

    def __new__(cls, kind: str, title: str, settings: dict[str, Any], blooms: list[tuple[str, str]],
                parent: QWidget | None = None):
        dialog = CardDialog(title, parent, width=540)
        main, box = section("Color" if kind == "color" else "File")
        row = QHBoxLayout()
        row.setSpacing(8)
        if kind == "color":
            held = [str(settings.get("color", "#000000ff"))]
            swatch = QPushButton()
            swatch.setStyleSheet(swatch_style(held[0]))
            swatch.setCursor(Qt.CursorShape.PointingHandCursor)

            def choose() -> None:
                picked = pick_colour(dialog, held[0])
                if picked:
                    held[0] = picked
                    swatch.setStyleSheet(swatch_style(picked))

            swatch.clicked.connect(choose)
            row.addWidget(swatch, 1)
            read = lambda: {"color": held[0]}  # noqa: E731
        else:
            path = dialog.line_edit("Pick an image")
            path.setText(str(settings.get("path", "") or ""))
            path.setCursorPosition(0)
            button = QPushButton("Choose…")
            button.setStyleSheet(CHIP)
            button.setCursor(Qt.CursorShape.PointingHandCursor)

            def choose() -> None:
                picked, _ = QFileDialog.getOpenFileName(dialog, "Choose image", path.text(), IMAGE_FILTER)
                if picked:
                    path.setText(picked)
                    path.setCursorPosition(0)

            button.clicked.connect(choose)
            row.addWidget(path, 1)
            row.addWidget(button)
            read = lambda: {"path": path.text().strip()}  # noqa: E731
        box.addLayout(row)
        dialog.body.addWidget(main)
        per = ByBloom(kind, blooms, settings)
        dialog.body.addWidget(per)
        dialog.yes = dialog.add_buttons("Save")
        dialog.values = lambda: {**read(), **per.values()}
        dialog.by_bloom = per
        return dialog
