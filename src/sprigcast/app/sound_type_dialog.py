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
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.card import CardDialog, field_label
from sprigcast.app.labels import literal
from sprigcast.app.sound_card import GROUP_COLOURS, TYPE_COLORS


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


class _SoundPill(QPushButton):
    """A sound to put in the type: tap to pick. Its dot is the type it's in now; picked, it fills."""

    def __init__(self, name: str, kind: str):
        super().__init__()
        self.full_name = name
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        metrics = QFontMetrics(self.font())
        shown = metrics.elidedText(name, Qt.TextElideMode.ElideRight, 136)
        self.setText(literal(shown))
        self.setToolTip(f"{name} (now in {kind})" if shown != name else f"Now in {kind}")
        self.setAccessibleName(name)  # screen readers and UI Automation read the whole name
        self.setAccessibleDescription(f"Now in {kind}")
        self.setIcon(_dot(GROUP_COLOURS.get(kind, theme.MUTED)))
        self.setStyleSheet(theme.button_style(14, "0px 12px", extra="font-size: 12px; min-height: 28px; text-align: left;")
                           + f"QPushButton:checked {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT};"
                             f" border: 1px solid {theme.ACCENT}; }}")
        self.setFixedHeight(30)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)  # the column sets the width  # (the min-height above too: the app style sheet sets a smaller one on show)


    def resizeEvent(self, event) -> None:  # noqa: N802
        """Cut a long name short to the pill's width (the columns set it), never more than needed."""
        super().resizeEvent(event)
        room = max(40, self.width() - 44)  # the dot, the padding and the edges
        shown = QFontMetrics(self.font()).elidedText(self.full_name, Qt.TextElideMode.ElideRight, room)
        if self.text() != literal(shown):
            self.setText(literal(shown))


def _dot(colour: str) -> QIcon:
    pix = QPixmap(20, 20)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(colour))
    p.drawEllipse(QRectF(5, 5, 10, 10))
    p.end()
    return QIcon(pix)


class SoundTypeDialog(CardDialog):
    """`types`: the garden's types ({"name", "color"}); `sounds`: (id, name, type) of every sound, A-Z."""

    def __init__(self, types: list[dict], sounds: list[tuple[str, str, str]], editing: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(f"Edit {editing}" if editing else "New type", parent, width=580)
        self.types = [dict(t) for t in types]
        self.sounds = sounds
        self.editing = editing
        self.removed = False
        mine = next((t for t in self.types if t["name"] == editing), None)

        # Two titled cards, as in Settings: the type itself, then its sounds as pills that wrap (no sideways
        # scrolling; a long list scrolls down only, and Find narrows it).
        about = QGroupBox("Name and color")
        box = QVBoxLayout(about)
        box.setContentsMargins(18, 22, 18, 18)
        box.setSpacing(10)
        box.addWidget(field_label("Name"))
        self.name = self.line_edit("Background, Friends, Ads…")
        self.name.setText(editing or "")
        self.name.setMaxLength(24)
        self.name.setAccessibleName("Name")
        box.addWidget(self.name)
        box.addSpacing(4)
        box.addWidget(field_label("Color"))
        swatches = QHBoxLayout()
        swatches.setSpacing(6)
        self.colors = QButtonGroup(self)  # one color picked at a time
        used = {t.get("color") for t in self.types if t is not mine}
        start = mine["color"] if mine else next((c for c in TYPE_COLORS if c not in used), "gray")
        names = theme.type_color_names()
        for key, colour in TYPE_COLORS.items():
            dot = _Swatch(colour)
            dot.setToolTip(names[key])
            dot.setAccessibleName(names[key])
            dot.key = key
            dot.setChecked(key == start)
            self.colors.addButton(dot)
            swatches.addWidget(dot)
        swatches.addStretch(1)
        box.addLayout(swatches)
        self.body.addWidget(about)

        sounds_card = QGroupBox("Sounds")
        box = QVBoxLayout(sounds_card)
        box.setContentsMargins(18, 22, 18, 18)
        box.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.find = self.line_edit("Find a sound")
        self.find.setClearButtonEnabled(True)
        self.find.setAccessibleName("Find a sound")
        self.find.textChanged.connect(lambda _t: self._filter())
        top.addWidget(self.find, 1)
        self.picked = QLabel()
        self.picked.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        top.addWidget(self.picked)
        box.addLayout(top)
        holder = QWidget()
        holder.setObjectName("pillHolder")
        holder.setStyleSheet("#pillHolder { background: transparent; }")
        # Two even columns (user 2026-11-20): every pill as wide as the other, rows lined up.
        stack = QVBoxLayout(holder)  # the rows at the top, the room under them empty when Find shows a few
        stack.setContentsMargins(0, 0, 0, 0)
        rows_box = QWidget()
        rows_box.setObjectName("pillRows")
        rows_box.setStyleSheet("#pillRows { background: transparent; }")
        stack.addWidget(rows_box)
        stack.addStretch(1)
        self.grid = QGridLayout(rows_box)
        self.grid.setContentsMargins(0, 0, 4, 0)
        self.grid.setHorizontalSpacing(10)
        self.grid.setVerticalSpacing(8)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        self.ticks: dict[str, QPushButton] = {}
        # The type's own sounds first (when editing), so what's in it shows without scrolling.
        for sid, name, kind in sorted(sounds, key=lambda s: bool(editing) and s[2] != editing):
            pill = _SoundPill(name, kind)
            pill.setChecked(bool(editing) and kind == editing)
            pill.toggled.connect(lambda _on: self._count())
            self.ticks[sid] = pill
        self._order = list(self.ticks.values())
        self._lay_out()
        self.none_found = QLabel("No sound matches.")
        self.none_found.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        self.none_found.hide()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # pills wrap instead
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setWidget(holder)
        rows = max(1, -(-len(sounds) // 2))
        scroll.setFixedHeight(min(5, rows) * 38)  # about five rows show; more scroll down
        box.addWidget(scroll)
        box.addWidget(self.none_found)
        self.body.addWidget(sounds_card)
        self._count()

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

    def _count(self) -> None:
        n = sum(p.isChecked() for p in self.ticks.values())
        self.picked.setText(f"{n} picked" if n else "")

    def _lay_out(self) -> None:
        """The pills Find lets through, two to a row with no gaps."""
        text = self.find.text().strip().casefold() if hasattr(self, "find") else ""
        for pill in self._order:
            self.grid.removeWidget(pill)
        shown = 0
        for pill in self._order:
            keep = not text or text in pill.full_name.casefold()
            pill.setVisible(keep)
            if keep:
                self.grid.addWidget(pill, shown // 2, shown % 2)
                shown += 1
        if hasattr(self, "none_found"):
            self.none_found.setVisible(bool(self.ticks) and not shown)

    def _filter(self) -> None:
        self._lay_out()

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
        names = [t["name"] for t in others]
        fallback = "Other" if "Other" in names else (names[-1] if names else "")
        moves: dict[str, str] = {}
        if self.removed:
            for sid, _name, kind in self.sounds:
                if kind == self.editing:
                    moves[sid] = fallback
            return others, moves
        name = self.chosen_name()
        picked = self.colors.checkedButton()
        entry = {"name": name, "color": picked.key if picked else "gray"}
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
