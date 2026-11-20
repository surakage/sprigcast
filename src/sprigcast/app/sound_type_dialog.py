"""New type / Edit type: a sound type's name and color, and the sounds in it.

    ╭──────────────────────────────╮
    │ New type                     │
    │ Name   [ Background       ]  │
    │ Color  ● ● ● ● ● ● ● ● ●     │
    │ Sounds                       │
    │ ☐ Game   ☑ Music   ☐ TTS ... │
    │ Remove type   Cancel  [Save] │
    ╰──────────────────────────────╯

A sound sits in one type. Ticking it here moves it in; unticking one that was in it moves it to the last other
type. Removing a type moves its sounds the same way.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.card import CardDialog, field_label
from sprigcast.app.labels import literal
from sprigcast.app.sound_card import TYPE_COLORS


class _Swatch(QAbstractButton):
    """A round color to pick, ringed when picked (drawn: a styled button came out squashed)."""

    def __init__(self, colour: str):
        super().__init__()
        self.colour = colour
        self.setCheckable(True)
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isChecked():
            p.setPen(QPen(QColor(theme.TEXT_BRIGHT), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(1, 1, 26, 26))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self.colour))
        p.drawEllipse(QRectF(5, 5, 18, 18))


class SoundTypeDialog(CardDialog):
    """`types`: the garden's types ({"name", "color"}); `sounds`: (id, name, type) of every sound, A-Z."""

    def __init__(self, types: list[dict], sounds: list[tuple[str, str, str]], editing: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(f"Edit {editing}" if editing else "New type", parent, width=520)
        self.types = [dict(t) for t in types]
        self.sounds = sounds
        self.editing = editing
        self.removed = False
        mine = next((t for t in self.types if t["name"] == editing), None)

        self.body.addWidget(field_label("Name"))
        self.name = self.line_edit("Background, Friends, Ads…")
        self.name.setText(editing or "")
        self.name.setMaxLength(24)
        self.body.addWidget(self.name)

        self.body.addWidget(field_label("Color"))
        swatches = QHBoxLayout()
        swatches.setSpacing(8)
        self.colors = QButtonGroup(self)
        used = {t.get("color") for t in self.types if t is not mine}
        start = mine["color"] if mine else next((c for c in TYPE_COLORS if c not in used), "grey")
        for key, colour in TYPE_COLORS.items():
            dot = _Swatch(colour)
            dot.setToolTip(key.capitalize())
            dot.key = key
            dot.setChecked(key == start)
            self.colors.addButton(dot)
            swatches.addWidget(dot)
        swatches.addStretch(1)
        self.body.addLayout(swatches)

        self.body.addWidget(field_label("Sounds"))
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        self.ticks: dict[str, QCheckBox] = {}
        for n, (sid, name, kind) in enumerate(sounds):
            tick = QCheckBox(literal(name))
            tick.setChecked(bool(editing) and kind == editing)
            tick.setToolTip(f"Now in {kind}")
            self.ticks[sid] = tick
            grid.addWidget(tick, n // 2, n % 2)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setWidget(holder)
        scroll.setFixedHeight(min(240, max(40, 30 * ((len(sounds) + 1) // 2))))
        self.body.addWidget(scroll)

        self.ok = self.add_buttons("Save" if editing else "Add")
        if editing:
            remove = QPushButton("Remove type")
            remove.setStyleSheet(theme.LINK_STYLE)
            remove.setCursor(Qt.CursorShape.PointingHandCursor)
            remove.setEnabled(len(self.types) > 1)  # there's always one type to put sounds in
            remove.clicked.connect(self._remove)
            self.body.itemAt(self.body.count() - 1).layout().insertWidget(0, remove)
        self.name.textChanged.connect(lambda _t: self._check())
        self._check()

    def _remove(self) -> None:
        self.removed = True
        self.accept()

    def chosen_name(self) -> str:
        return " ".join(self.name.text().split())

    def _check(self) -> None:
        name = self.chosen_name()
        taken = {t["name"].casefold() for t in self.types if t["name"] != self.editing}
        self.ok.setEnabled(bool(name) and name.casefold() not in taken)
        self.ok.setToolTip("A type has that name" if name and name.casefold() in taken else "")

    def result_types(self) -> tuple[list[dict], dict[str, str]]:
        """The new list of types, and {sound id: its new type} for the sounds that move."""
        others = [t for t in self.types if t["name"] != self.editing]
        fallback = others[-1]["name"] if others else ""
        moves: dict[str, str] = {}
        if self.removed:
            for sid, _name, kind in self.sounds:
                if kind == self.editing:
                    moves[sid] = fallback
            return others, moves
        name = self.chosen_name()
        picked = self.colors.checkedButton()
        entry = {"name": name, "color": picked.key if picked else "grey"}
        if self.editing:
            types = [entry if t["name"] == self.editing else t for t in self.types]
        else:
            types = self.types + [entry]
        if not fallback:
            fallback = name
        for sid, _name, kind in self.sounds:
            ticked = self.ticks[sid].isChecked()
            if ticked and kind != name:
                moves[sid] = name
            elif not ticked and self.editing and kind == self.editing:
                moves[sid] = fallback  # taken out: to the last other type
        return types, moves
