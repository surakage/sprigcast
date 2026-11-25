"""The PNGtuber's settings, as one rounded card with three parts (user 2026-11-25):

    ╭ PNGtuber ──────────────────────────────────────╮
    │ Pictures                                       │
    │  Folder [ Sloth (built in)          ] [Browse] │
    │  [Idle] [Talk] [Half] [Yell] [Blink] [Sleep]   │  pictures, click one to pick your own
    │  Outfits  (Sloth) (Cozy)  [+ Save as outfit]   │
    │ Voice                                          │
    │  Mic [ Auto (your mic)                 ⌄ ]     │
    │  ▕████████▏····|·········|···▕  live level     │  the two marks are Talks and Yells
    │  Talks above ───●──── -38 dB                   │
    │  Yells above ──────●─ -12 dB                   │
    │  Hold, Naps after                              │
    │ Motion                                         │
    │  Bounce (Hop) (Squash) (Off)   Strength ──●──  │
    │  Idle sway ○   Shake on yell ●   Blink ●       │
    │  Try  (Hop) (Sparkle) (Shake)                  │
    │                              Cancel   [Save]   │
    ╰────────────────────────────────────────────────╯

values() gives the settings, the same keys as store/pngtuber.py PngtuberSource.defaults.
The plugin's own module is used (tuber_module), so its rules for pictures are the ones the card shows.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.card import FIELD_STYLE, CardDialog, combo
from sprigcast.app.card import section as card_section
from sprigcast.app.live_stage import Switch

CHIP = theme.button_style(15, "7px 14px")
CHIP_ON = (f"QPushButton {{ background: {theme.SELECT_BG}; color: {theme.TEXT_BRIGHT}; border: 1px solid "
           f"{theme.ACCENT}; border-radius: 15px; padding: 7px 14px; }}")
LABEL_W = 118
PICTURE_FILTER = "Pictures (*.png *.gif *.webp *.apng)"
# The pictures you can give it, in the order they're shown. (state, label)
TILES = (("idle", "Idle"), ("talk", "Talk"), ("half", "Half open"), ("yell", "Yell"), ("blink", "Blink"),
         ("sleep", "Sleep"))
METER_LOW, METER_HIGH = -60.0, 0.0  # the meter's dB range
NAPS = (0, 30, 60, 120, 300, 600)  # Naps after: off, then seconds


def tuber_module():
    """The PNGtuber plugin as it runs (from the plugins folder), never a second copy of it."""
    import sys

    from sprigcast.core.source import source_class

    cls = source_class("pngtuber")
    if cls is None:
        from sprigcast.store import pngtuber  # not added (a test): the copy that ships with SprigCast

        return pngtuber
    return sys.modules[cls.__module__]


def _section(title: str) -> tuple[QFrame, QVBoxLayout]:
    return card_section(title, 12)


def _label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
    label.setFixedWidth(LABEL_W)
    return label


def _row(label: str, *widgets, stretch: bool = False) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(10)
    row.addWidget(_label(label))
    for w in widgets:
        row.addWidget(w, 1 if isinstance(w, (QSlider, QLineEdit, QComboBox)) else 0)
    if stretch:
        row.addStretch(1)
    return row


class ValueSlider(QWidget):
    """A slider with its value written beside it (no number box to type in)."""

    changed = Signal()

    def __init__(self, low: float, high: float, value: float, step: float, show: Callable[[float], str]):
        super().__init__()
        self.step, self.show = step, show
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(round(low / step), round(high / step))
        self.slider.setValue(round(float(value) / step))
        self.text = QLabel()
        self.text.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; background: transparent;")
        self.text.setFixedWidth(64)
        self.text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.text)
        self.slider.valueChanged.connect(self._moved)
        self._moved()

    def _moved(self, *_) -> None:
        self.text.setText(self.show(self.value()))
        self.changed.emit()

    def value(self) -> float:
        return round(self.slider.value() * self.step, 4)


class Meter(QWidget):
    """The voice's level now, with Talks above and Yells above marked on it: green past Talks, gold past Yells."""

    def __init__(self, level: Callable[[], float | None]):
        super().__init__()
        self.level_now = level
        self.talk_at, self.yell_at = -38.0, -12.0
        self.db: float | None = None
        self.setFixedHeight(26)
        self.timer = QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._tick)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(event)

    def _tick(self) -> None:
        try:
            db = self.level_now()
        except Exception:  # noqa: BLE001 - the meter just rests
            db = None
        if db is not None and self.db is not None and db < self.db:
            db = max(db, self.db - 1.5)  # falls back gently, like a meter, instead of flickering
        if db != self.db:
            self.db = db
            self.update()

    def _x(self, db: float, width: float) -> float:
        return width * (min(METER_HIGH, max(METER_LOW, db)) - METER_LOW) / (METER_HIGH - METER_LOW)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0, 7, self.width(), 12)
        path = QPainterPath()
        path.addRoundedRect(r, 6, 6)
        p.fillPath(path, QColor(theme.BG))
        if self.db is not None:
            w = self._x(self.db, r.width())
            colour = theme.WARN if self.db >= self.yell_at else theme.ACCENT if self.db >= self.talk_at \
                else theme.MUTED
            p.save()
            p.setClipPath(path)
            p.fillRect(QRectF(0, r.top(), w, r.height()), QColor(colour))
            p.restore()
        for db, colour in ((self.talk_at, theme.ACCENT), (self.yell_at, theme.WARN)):
            x = self._x(db, r.width())
            p.fillRect(QRectF(x - 1, 2, 2, self.height() - 4), QColor(colour))
        p.end()


class Tile(QAbstractButton):
    """One picture of the model: what it shows now (yours or the folder's), click to pick another."""

    def __init__(self, state: str, label: str):
        super().__init__()
        self.state = state
        self.path: Path | None = None  # picked here
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(78, 100)
        self.setToolTip(f"{label} picture: click to pick one")
        self.setAccessibleName(f"{label} picture")
        self.name = label
        self.picture: QPixmap | None = None
        self.borrowed = False

    def show_picture(self, path: Path | None, borrowed: bool) -> None:
        self.picture = None
        if path is not None and path.is_file():
            image = QImage(str(path))
            if not image.isNull():
                self.picture = QPixmap.fromImage(image.scaled(66, 66, Qt.AspectRatioMode.KeepAspectRatio,
                                                              Qt.TransformationMode.SmoothTransformation))
        self.borrowed = borrowed
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(1, 1, self.width() - 2, 74)
        path = QPainterPath()
        path.addRoundedRect(box, 14, 14)
        p.fillPath(path, QColor(theme.BG))
        hover = self.underMouse()
        p.setPen(QColor(theme.ACCENT if self.path is not None else theme.EDGE_STRONG if not hover else theme.MUTED))
        p.drawPath(path)
        if self.picture is not None:
            if self.borrowed:
                p.setOpacity(0.35)
            p.drawPixmap(int(box.center().x() - self.picture.width() / 2),
                         int(box.center().y() - self.picture.height() / 2), self.picture)
            p.setOpacity(1.0)
        else:
            p.setPen(QColor(theme.MUTED))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, "+")
        p.setPen(QColor(theme.TEXT if not self.borrowed else theme.MUTED))
        p.drawText(QRectF(0, 78, self.width(), 20), Qt.AlignmentFlag.AlignCenter, self.name)
        p.end()


class PngtuberDialog(CardDialog):
    """New or change a PNGtuber. level() (when set) is the followed sound's level in dB now, or None;
    react(name) (when set) plays a reaction on the one on screen; ask_name(title, label, suggested) asks for the
    name of a new outfit or expression."""

    def __init__(self, title: str, settings: dict[str, Any], parent: QWidget | None = None,
                 listen_choices: list[tuple[str, str]] | None = None, level: Callable[[], float | None] | None = None,
                 react: Callable[[str], None] | None = None, primary: str = "Save",
                 ask_name: Callable[[str, str, str], str | None] | None = None):
        pngtuber = tuber_module()

        super().__init__(title, parent, width=640)
        self.s = {**pngtuber.PngtuberSource.defaults, **settings}
        self._ask_name = ask_name
        pngtuber = tuber_module()
        self.default = pngtuber.DEFAULT
        self.picks: dict[str, dict[str, Path]] = {self.default: pngtuber.picked_pictures(self.s)}
        self.picks.update(pngtuber.own_expressions(self.s))  # name -> state -> picture picked here
        self.editing = self.default
        self.outfits: list[dict[str, Any]] = [dict(o) for o in (self.s.get("outfits") or []) if isinstance(o, dict)]
        self.outfit = str(self.s.get("outfit") or "")

        # Pictures -------------------------------------------------------------------------------
        pictures, box = _section("Pictures")
        self.folder = QLineEdit(str(self.s.get("folder") or ""))
        self.folder.setPlaceholderText("Sloth (built in)")
        self.folder.setStyleSheet(FIELD_STYLE)
        self.folder.setMinimumHeight(34)
        self.folder.editingFinished.connect(self._show_pictures)
        browse = QPushButton("Browse…")
        browse.setStyleSheet(CHIP)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self._browse_folder)
        box.addLayout(_row("Folder", self.folder, browse))
        self.face_row = QHBoxLayout()
        self.face_row.setSpacing(8)
        box.addLayout(self.face_row)
        tiles = QGridLayout()
        tiles.setHorizontalSpacing(10)
        self.tiles: dict[str, Tile] = {}
        for i, (state, label) in enumerate(TILES):
            tile = Tile(state, label)
            tile.clicked.connect(lambda _=False, t=tile: self._pick_picture(t))
            tile.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            tile.customContextMenuRequested.connect(lambda _p, t=tile: self._clear_picture(t))
            tiles.addWidget(tile, 0, i)
            self.tiles[state] = tile
        box.addLayout(tiles)
        self.outfit_row = QHBoxLayout()
        self.outfit_row.setSpacing(8)
        box.addLayout(self.outfit_row)
        self.body.addWidget(pictures)

        # Voice ------------------------------------------------------------------------------------
        voice, box = _section("Voice")
        self.listen = combo()
        for value, label in listen_choices or [("auto", "Auto (your mic)")]:
            self.listen.addItem(label, value)
        at = self.listen.findData(self.s.get("listen_to", "auto"))
        self.listen.setCurrentIndex(max(0, at))
        box.addLayout(_row("Mic", self.listen))
        self.meter = Meter(level or (lambda: None))
        box.addLayout(_row("Level", self.meter))
        self.talk_at = ValueSlider(-60, -5, float(self.s.get("talk_at", -38.0)), 1, lambda v: f"{v:.0f} dB")
        self.yell_at = ValueSlider(-40, 0, float(self.s.get("yell_at", -12.0)), 1, lambda v: f"{v:.0f} dB")
        self.hold = ValueSlider(0, 0.5, float(self.s.get("hold", 0.08)), 0.01, lambda v: f"{v:.2f} s")
        nap = int(self.s.get("sleep_after", 120) or 0)
        self.nap = ValueSlider(0, len(NAPS) - 1, min(range(len(NAPS)), key=lambda i: abs(NAPS[i] - nap)), 1,
                               lambda v: _nap_text(NAPS[int(v)]))
        for slider in (self.talk_at, self.yell_at):
            slider.changed.connect(self._marks)
        box.addLayout(_row("Talks above", self.talk_at))
        box.addLayout(_row("Yells above", self.yell_at))
        box.addLayout(_row("Hold", self.hold))
        box.addLayout(_row("Naps after", self.nap))
        self._marks()
        self.body.addWidget(voice)

        # Motion -----------------------------------------------------------------------------------
        motion, box = _section("Motion")
        style = str(self.s.get("bounce_style") or ("hop" if self.s.get("bounce", True) else "off"))
        self.style_buttons: dict[str, QPushButton] = {}
        chips = []
        for key, label in (("hop", "Hop"), ("squash", "Squash"), ("off", "Off")):
            chip = QPushButton(label)
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda _=False, k=key: self._pick_style(k))
            self.style_buttons[key] = chip
            chips.append(chip)
        box.addLayout(_row("Bounce", *chips, stretch=True))
        self.strength = ValueSlider(25, 200, round(float(self.s.get("bounce_strength", 1.0)) * 100), 5,
                                    lambda v: f"{v:.0f}%")
        box.addLayout(_row("Strength", self.strength))
        self._pick_style(style if style in self.style_buttons else "hop")
        switches = QHBoxLayout()
        switches.setSpacing(10)
        self.sway, self.shake, self.blink = Switch(), Switch(), Switch()
        self.sway.setChecked(bool(self.s.get("sway", False)))
        self.shake.setChecked(bool(self.s.get("shake", True)))
        self.blink.setChecked(bool(self.s.get("blink", True)))
        for text, switch in (("Idle sway", self.sway), ("Shake on yell", self.shake), ("Blink", self.blink)):
            switch.setAccessibleName(text)
            label = QLabel(text)
            label.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
            switches.addWidget(label)
            switches.addWidget(switch)
            switches.addSpacing(14)
        switches.addStretch(1)
        holder = QHBoxLayout()
        holder.addWidget(_label("Moves"))
        holder.addLayout(switches, 1)
        box.addLayout(holder)
        if react is not None:
            tries = []
            for key, label in (("hop", "Hop"), ("sparkle", "Sparkle"), ("shake", "Shake")):
                b = QPushButton(label)
                b.setStyleSheet(CHIP)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.clicked.connect(lambda _=False, k=key: react(k))
                tries.append(b)
            box.addLayout(_row("Try", *tries, stretch=True))
        self.body.addWidget(motion)

        self.ok = self.add_buttons(primary)
        self._show_pictures()
        self._show_outfits()

    # Pictures ---------------------------------------------------------------------------------------

    def _browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Your PNGtuber's folder", self.folder.text())
        if path:
            self.folder.setText(path)
            self._show_pictures()

    def _pick_picture(self, tile: Tile) -> None:
        start = str(tile.path.parent) if tile.path else self.folder.text()
        name = tile.name if self.editing == self.default else f"{self.editing.title()} {tile.name.lower()}"
        path, _ = QFileDialog.getOpenFileName(self, f"{name} picture", start, PICTURE_FILTER)
        if path:
            self.picks.setdefault(self.editing, {})[tile.state] = Path(path)
            self._show_pictures()

    def _clear_picture(self, tile: Tile) -> None:
        if self.picks.get(self.editing, {}).pop(tile.state, None) is not None:
            self._show_pictures()

    def _found(self) -> dict:
        pngtuber = tuber_module()
        v = self._pictures_now()
        return pngtuber.scan(pngtuber.model_folder(v), pngtuber.picked_pictures(v), pngtuber.own_expressions(v))

    def _show_pictures(self) -> None:
        """The expression being edited: each tile shows the picture picked here, else the folder's, else the one it
        borrows (dimmed)."""
        pngtuber = tuber_module()
        found = self._found()
        picked = self.picks.get(self.editing, {})
        states = {**found.get(self.editing, {}), **{k: v for k, v in picked.items() if v.is_file()}}
        if self.editing == self.default and not states and found:
            states = next(iter(found.values()))
        for state, tile in self.tiles.items():
            tile.path = picked.get(state)
            if tile.path is not None:
                tile.show_picture(tile.path, False)
                continue
            use = pngtuber.pick_state(states, state)
            tile.show_picture(states.get(use) if use else None, use != state)
        self._show_faces(found)

    # Expressions ------------------------------------------------------------------------------------

    def _show_faces(self, found: dict) -> None:
        """Default, then each expression (the folder's and those made here), then + Add expression."""
        pngtuber = tuber_module()
        _clear(self.face_row)
        self.face_row.addWidget(_label("Expressions"))
        names = pngtuber.expression_order(set(found) | {n for n in self.picks if n != self.default} | {self.default})
        self.face_chips: dict[str, QPushButton] = {}
        for name in names:
            chip = QPushButton(name.title())
            chip.setStyleSheet(CHIP_ON if name == self.editing else CHIP)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            mine = name != self.default and name in self.picks
            chip.setToolTip("Its pictures · right-click to remove" if mine else "Its pictures")
            chip.clicked.connect(lambda _=False, n=name: self._edit(n))
            if mine:
                chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                chip.customContextMenuRequested.connect(lambda _p, n=name: self._remove_face(n))
            self.face_row.addWidget(chip)
            self.face_chips[name] = chip
        add = QPushButton("+ Add expression")
        add.setStyleSheet(CHIP)
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(self._add_face)
        self.face_row.addWidget(add)
        self.face_row.addStretch(1)

    def _edit(self, name: str) -> None:
        self.editing = name
        self._show_pictures()

    def _add_face(self) -> None:
        name = self._ask_name("Add expression", "Expression name", "") if self._ask_name else None
        name = (name or "").strip().lower()
        if not name:
            return
        self.picks.setdefault(name, {})
        self.editing = name
        self._show_pictures()

    def _remove_face(self, name: str) -> None:
        self.picks.pop(name, None)
        if self.editing == name:
            self.editing = self.default
        self._show_pictures()

    # Outfits ----------------------------------------------------------------------------------------

    def _show_outfits(self) -> None:
        _clear(self.outfit_row)
        self.outfit_row.addWidget(_label("Outfits"))
        for outfit in self.outfits:
            name = str(outfit.get("name", ""))
            chip = QPushButton(name)
            chip.setStyleSheet(CHIP_ON if name == self.outfit else CHIP)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip("Wear this outfit · right-click to remove it")
            chip.clicked.connect(lambda _=False, o=outfit: self._wear(o))
            chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            chip.customContextMenuRequested.connect(lambda _p, o=outfit: self._forget(o))
            self.outfit_row.addWidget(chip)
        save = QPushButton("+ Save as outfit")
        save.setStyleSheet(CHIP)
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.clicked.connect(self._save_outfit)
        self.outfit_row.addWidget(save)
        self.outfit_row.addStretch(1)

    def _pictures_now(self) -> dict[str, Any]:
        """The folder and every picture picked here: the default's at the top, each expression's in
        expression_pictures (an expression with none picked yet isn't kept)."""
        out: dict[str, Any] = {"folder": self.folder.text().strip()}
        mine = self.picks.get(self.default, {})
        for state, _label in TILES:
            out[f"{state}_picture"] = str(mine[state]) if state in mine else ""
        out["expression_pictures"] = {
            name: {f"{state}_picture": str(path) for state, path in states.items()}
            for name, states in sorted(self.picks.items()) if name != self.default and states}
        return out

    def _save_outfit(self) -> None:
        pngtuber = tuber_module()

        name = self._ask_name("Save as outfit", "Outfit name", pngtuber.outfit_name_for(self._pictures_now())) \
            if self._ask_name else None
        if not name:
            return
        self.outfits = [o for o in self.outfits if str(o.get("name", "")).lower() != name.lower()]
        self.outfits.append({"name": name, **self._pictures_now()})
        self.outfits.sort(key=lambda o: str(o.get("name", "")).lower())
        self.outfit = name
        self._show_outfits()

    def _wear(self, outfit: dict[str, Any]) -> None:
        pngtuber = tuber_module()
        self.folder.setText(str(outfit.get("folder", "")))
        self.picks = {self.default: pngtuber.picked_pictures(outfit), **pngtuber.own_expressions(outfit)}
        self.editing = self.default
        self.outfit = str(outfit.get("name", ""))
        self._show_pictures()
        self._show_outfits()

    def _forget(self, outfit: dict[str, Any]) -> None:
        self.outfits = [o for o in self.outfits if o is not outfit]
        if self.outfit == outfit.get("name"):
            self.outfit = ""
        self._show_outfits()

    # Voice and motion -------------------------------------------------------------------------------

    def _marks(self) -> None:
        self.meter.talk_at, self.meter.yell_at = self.talk_at.value(), self.yell_at.value()
        self.meter.update()

    def _pick_style(self, key: str) -> None:
        self.style = key
        for k, chip in self.style_buttons.items():
            chip.setChecked(k == key)
            chip.setStyleSheet(CHIP_ON if k == key else CHIP)
        self.strength.setEnabled(key != "off")

    def values(self) -> dict[str, Any]:
        out = {**self.s, **self._pictures_now()} if hasattr(self, "tiles") else {**self.s}
        if not hasattr(self, "style"):
            return out
        out.update({
            "listen_to": self.listen.currentData() or "auto",
            "talk_at": self.talk_at.value(), "yell_at": self.yell_at.value(), "hold": self.hold.value(),
            "sleep_after": NAPS[int(self.nap.value())],
            "bounce_style": self.style, "bounce": self.style != "off",
            "bounce_strength": round(self.strength.value() / 100, 2),
            "sway": self.sway.isChecked(), "shake": self.shake.isChecked(), "blink": self.blink.isChecked(),
            "outfits": self.outfits, "outfit": self.outfit,
        })
        return out


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()


def _nap_text(seconds: int) -> str:
    if not seconds:
        return "Never"
    return f"{seconds} s" if seconds < 60 else f"{seconds // 60} min"
