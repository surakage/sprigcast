"""Build's sound strips: every sound as a small upright strip under the canvas.

    Sound ▾
    ╭────────╮ ╭────────╮ ╭────────╮
    │ 🎙 Mic │ │ Music  │ │ Game   │
    │  ▮ ●   │ │  ▮ ●   │ │  ▮ ●   │   level bar with a seed-shaped knob: drag to set the volume
    │  ▮ │   │ │  ▮ │   │ │  ▮ │   │
    │ -3 dB  │ │  0 dB  │ │ muted  │
    │ (🔊) ⋯ │ │ (🔊) ⋯ │ │ (🔇) ⋯ │
    ╰────────╯ ╰────────╯ ╰────────╯

Sound plays in every bloom, so it sits with the canvas rather than in the list of a bloom's leaves.
The name or the gear opens the sound's full controls on the right; right-click a strip to rename it, pick
the blooms it plays in, open its filters or remove it. + beside the heading adds a sound. Fold the row away
with the heading.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import icons, theme
from sprigcast.app.sound_card import (  # the garden's sound types: these change in place
    GROUP_COLOURS,
    GROUPS,
    type_of,
)

LOW, HIGH = -60.0, 12.0  # the fader's range, as on the Tune page
STRIP_W, STRIP_H = 104, 220
FOLD_KEY = "build/sound_strips_open"
SHOW_KEY = "build/sound_strips_show"  # "all" or "here" (this bloom): the chip you last picked
TYPE_KEY = "build/sound_strips_type"  # "" (all types) or a group: the type you last picked


def group_colour(group: str) -> str:
    """The same colour a type has on Live's Sound card."""
    return GROUP_COLOURS.get(group, theme.MUTED)


class Fader(QWidget):
    """An upright level bar with a round knob for the volume: drag it (or click the bar) to set it,
    double-click for 0 dB. The bar fills from the bottom with how loud the sound is."""

    changed = Signal(float)

    def __init__(self, db: float = 0.0):
        super().__init__()
        self.db = db
        self.level = 0.0  # 0..1 of -60..0 dBFS
        self.muted = False
        self.setFixedWidth(34)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip("Drag to set the volume (double-click: 0 dB)")

    def set_db(self, rms: float) -> None:
        target = (min(0.0, max(-60.0, rms)) + 60.0) / 60.0
        level = target if target > self.level else max(target, self.level - 0.02)  # falls back slowly
        if abs(level - self.level) > 0.002:
            self.level = level
            self.update()

    def _span(self) -> tuple[float, float]:
        return 8.0, max(9.0, self.height() - 8.0)  # top, bottom of the travel

    def _y(self, db: float) -> float:
        top, bottom = self._span()
        return bottom - (bottom - top) * (db - LOW) / (HIGH - LOW)

    def _set_from(self, y: float) -> None:
        top, bottom = self._span()
        db = round(LOW + (bottom - y) / max(1.0, bottom - top) * (HIGH - LOW))
        db = max(LOW, min(HIGH, 0.0 if abs(db) <= 1 else float(db)))  # 0 dB is easy to land on
        if db != self.db:
            self.db = db
            self.update()
            self.changed.emit(db)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._set_from(event.position().y())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._set_from(event.position().y())

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self.db != 0:
            self.db = 0.0
            self.update()
            self.changed.emit(0.0)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        top, bottom = self._span()
        cx = self.width() / 2
        bar = QRectF(cx - 3, top, 6, bottom - top)
        p.setBrush(QColor(theme.RAISED))
        p.drawRoundedRect(bar, 3, 3)
        if self.level > 0 and not self.muted:
            colour = theme.ACCENT if self.level < 2 / 3 else theme.WARN if self.level < 0.85 else theme.LIVE
            p.setBrush(QColor(colour))
            h = max(6.0, bar.height() * self.level)
            p.drawRoundedRect(QRectF(bar.x(), bar.bottom() - h, 6, h), 3, 3)
        zero = self._y(0.0)  # a quiet tick at 0 dB
        p.setPen(QPen(QColor(theme.EDGE_STRONG), 1.5))
        p.drawLine(QPointF(cx + 7, zero), QPointF(cx + 11, zero))
        p.setPen(QPen(QColor(theme.ACCENT if not self.muted else theme.MUTED), 2))
        p.setBrush(QColor(theme.TEXT_BRIGHT if not self.muted else theme.RAISED_HOVER))
        p.drawEllipse(QPointF(cx, self._y(self.db)), 7, 7)
        p.end()


class Strip(QFrame):
    """One sound: its name, fader, volume and mute, on a rounded card."""

    def __init__(self, sound, kind: str, on_change: Callable[[str, dict], None], on_open: Callable[[str], None],
                 on_menu: Callable[[str | None, object], None] | None = None):
        super().__init__()
        self.sound_id = sound.id
        self.group = getattr(sound, "group", "Other") or "Other"
        self.on_change, self.on_open = on_change, on_open
        self.on_menu = on_menu or (lambda _sid, _at: None)
        self.setObjectName("soundStrip")
        self.setFixedSize(STRIP_W, STRIP_H)
        self.set_selected(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: self.on_menu(self.sound_id, self.mapToGlobal(pos)))
        col = QVBoxLayout(self)
        col.setContentsMargins(8, 10, 8, 10)
        col.setSpacing(4)
        self.name = QPushButton()
        self.name.setCursor(Qt.CursorShape.PointingHandCursor)
        self.name.setIcon(icons.icon(_kind_icon(kind), group_colour(self.group), 14))  # coloured by its group
        self.name.setStyleSheet(f"QPushButton {{ background: transparent; border: none; color: {theme.TEXT};"
                                " font-weight: 600; font-size: 12px; padding: 0; text-align: center; }"
                                f"QPushButton:hover {{ color: {theme.TEXT_BRIGHT}; }}")
        self.name.clicked.connect(lambda: self.on_open(self.sound_id))
        col.addWidget(self.name)
        self.tag = QLabel()
        self.tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tag.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        small = self.tag.font()
        small.setPixelSize(10)  # on the font, not in the style sheet, so measuring it gives the size it's shown at
        self.tag.setFont(small)
        self.tag_text: str | None = None  # set_tag fills it
        self.tag.setFixedHeight(QFontMetrics(self.tag.font()).height())  # kept when empty, so strips line up
        col.addWidget(self.tag)
        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.addStretch(1)
        self.fader = Fader(sound.volume_db)
        self.fader.changed.connect(self._volume)
        middle.addWidget(self.fader)
        middle.addStretch(1)
        col.addLayout(middle, 1)
        self.db = QLabel()
        self.db.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.db.setStyleSheet(f"color: {theme.MUTED}; font-size: 11px; background: transparent;")
        col.addWidget(self.db)
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.mute = QPushButton()
        self.mute.setCheckable(True)
        self.mute.setFixedSize(34, 26)
        self.mute.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mute.setStyleSheet(
            theme.button_style(12, "0"))
        self.mute.clicked.connect(self._mute_clicked)
        more = QPushButton()
        more.setIcon(icons.icon("gear", theme.MUTED, 15))
        more.setFixedSize(34, 26)
        more.setCursor(Qt.CursorShape.PointingHandCursor)
        more.setToolTip("Volume, listeners, filters and tracks")
        more.setStyleSheet(theme.button_style(12, "0"))
        more.clicked.connect(lambda: self.on_open(self.sound_id))
        buttons.addStretch(1)
        buttons.addWidget(self.mute)
        buttons.addWidget(more)
        buttons.addStretch(1)
        col.addLayout(buttons)
        self.show_sound(sound)

    def set_selected(self, on: bool) -> None:
        if getattr(self, "_selected", None) == on:
            return  # a new stylesheet re-polishes the whole strip: only when the ring really changes
        self._selected = on
        edge = theme.ACCENT if on else theme.CARD
        self.setStyleSheet(f"#soundStrip {{ background: {theme.SELECT_BG if on else theme.CARD};"
                           f" border: 1px solid {edge}; border-top: 3px solid {group_colour(self.group)};"
                           " border-radius: 18px; }")

    def set_tag(self, text: str) -> None:
        """Where it plays, when that isn't every bloom ("Gaming", "2 blooms")."""
        if getattr(self, "tag_text", None) == text:
            return
        self.tag_text = text
        metrics = QFontMetrics(self.tag.font())
        self.tag.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, STRIP_W - 20))
        self.tag.setToolTip(f"Plays in {text}" if text else "Plays in every bloom")

    def _mute_clicked(self, on: bool) -> None:
        """Show it at once on this strip; the rest of the window catches up when changes settle."""
        name, db = (getattr(self, "_shown", None) or (self.full_name, self.fader.db))[:2]
        self.show_sound(SimpleNamespace(name=name, volume_db=db, muted=on, group=self.group))
        self.on_change(self.sound_id, {"muted": on})

    def _volume(self, db: float) -> None:
        self.db.setText(f"{db:+.0f} dB" if db else "0 dB")
        self.on_change(self.sound_id, {"volume_db": db})

    def show_sound(self, sound) -> None:
        shown = (sound.name, sound.volume_db, sound.muted, getattr(sound, "group", ""))
        if getattr(self, "_shown", None) == shown:
            return  # nothing to redo: every change used to reset every strip's labels and icons
        self._shown = shown
        self.full_name = sound.name
        metrics = QFontMetrics(_bold(self.name.font()))
        self.name.setText(metrics.elidedText(sound.name, Qt.TextElideMode.ElideRight, STRIP_W - 36))
        self.name.setToolTip(f"{sound.name} ({type_of(sound.group)}): open its controls")  # the type in words too
        if self.fader.db != sound.volume_db:
            self.fader.db = sound.volume_db
        self.fader.muted = sound.muted
        self.fader.update()
        self.db.setText("muted" if sound.muted else f"{sound.volume_db:+.0f} dB" if sound.volume_db else "0 dB")
        self.mute.setChecked(sound.muted)
        self.mute.setIcon(icons.icon("speaker-off" if sound.muted else "speaker",
                                     theme.SELECT_TEXT if sound.muted else theme.TEXT, 16))
        self.mute.setToolTip("Unmute" if sound.muted else "Mute")


def _dot(colour: str):
    """A small round dot in a group's colour, for its chip."""
    from PySide6.QtGui import QIcon, QPixmap

    pix = QPixmap(16, 16)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(colour))
    p.drawEllipse(QRectF(4, 4, 8, 8))
    p.end()
    return QIcon(pix)


def _bold(font: QFont) -> QFont:
    font = QFont(font)
    font.setWeight(QFont.Weight.DemiBold)
    font.setPixelSize(12)
    return font


def _kind_icon(kind: str) -> str:
    return {"audio_input": "mic", "browser": "monitor", "media": "disk"}.get(kind, "speaker")


class _Fade(QWidget):
    """The right edge of the strips melts into the card, so it's clear there are more to scroll to."""

    def paintEvent(self, _event) -> None:  # noqa: N802
        from PySide6.QtGui import QLinearGradient, QPainter

        p = QPainter(self)
        grad = QLinearGradient(0, 0, self.width(), 0)
        edge = QColor(theme.PANEL)
        clear = QColor(edge)
        clear.setAlpha(0)
        grad.setColorAt(0, clear)
        grad.setColorAt(1, edge)
        p.fillRect(self.rect(), grad)
        p.end()


class SoundStrips(QFrame):
    """The Sound card under the canvas: a row of strips that scrolls sideways, with a heading that folds it
    away (remembered), where they play, and +."""

    def __init__(self, on_change: Callable[[str, dict], None], on_open: Callable[[str], None],
                 on_menu: Callable[[str | None, object], None] | None = None,
                 on_types: Callable[[str | None], None] | None = None):
        super().__init__()
        self.on_types = on_types or (lambda _name: None)  # None: New type; a name: Edit that type
        self.setObjectName("soundCard")
        self.setStyleSheet(f"#soundCard {{ background: {theme.PANEL}; border-radius: 24px; }}")
        self.on_change, self.on_open = on_change, on_open
        self.on_menu = on_menu or (lambda _sid, _at: None)  # (sound id, or None for + Add; where)
        self.selected: str | None = None
        self.strips: dict[str, Strip] = {}
        self._shape: tuple = ()
        col = QVBoxLayout(self)
        col.setContentsMargins(18, 12, 14, 14)
        col.setSpacing(10)
        self.toggle = QPushButton()
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setStyleSheet(f"QPushButton {{ background: transparent; border: none; color: {theme.TEXT_BRIGHT};"
                                  " font-weight: 700; font-size: 14px; text-align: left; padding: 0 4px; }")
        self.toggle.clicked.connect(lambda: self.set_open(not self.is_open))
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.toggle)
        head.addStretch(1)
        self.where = QLabel("")
        self.where.setStyleSheet(f"color: {theme.MUTED}; font-size: 12px; background: transparent;")
        head.addWidget(self.where)
        head.addSpacing(8)
        self.add = QPushButton()
        self.add.setIcon(icons.icon("plus", theme.TEXT, 14))
        self.add.setFixedSize(26, 26)
        self.add.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add.setToolTip("Add a mic, desktop sound, app sound or sound file")
        self.add.setStyleSheet(theme.button_style(12, "0"))
        self.add.clicked.connect(lambda: self.on_menu(None, self.add.mapToGlobal(self.add.rect().bottomLeft())))
        head.addWidget(self.add)
        col.addLayout(head)
        # A long list: find one by name, show this bloom's sounds only, or one type (coloured as on Live).
        self.finder = QWidget()
        find = QHBoxLayout(self.finder)
        find.setContentsMargins(0, 0, 0, 0)
        find.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a sound")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedSize(180, 26)
        self.search.setStyleSheet(f"QLineEdit {{ background: {theme.CARD}; border: 1px solid {theme.EDGE_STRONG};"
                                  f" border-radius: 13px; padding: 0px 12px; color: {theme.TEXT}; }}"
                                  f"QLineEdit:focus {{ border-color: {theme.ACCENT}; }}")
        self.search.textChanged.connect(lambda _t: self._filter())
        find.addWidget(self.search)
        self.chips: dict[str, QPushButton] = {}
        for key, text in (("all", "All"), ("here", "This bloom")):
            chip = QPushButton(text)
            chip.setCheckable(True)
            chip.setAutoExclusive(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setFixedHeight(26)  # every chip as tall, so each keeps its round ends
            chip.setStyleSheet(theme.button_style(12, "0px 11px", extra="font-size: 12px;"))
            chip.clicked.connect(lambda _on=False, k=key: self._pick(k))
            self.chips[key] = chip
            find.addWidget(chip)
        self.kind = QPushButton()  # the sound's type: a pill that lists Voice, Game, Alerts, Music and Other
        self.kind.setFixedHeight(26)
        self.kind.setCursor(Qt.CursorShape.PointingHandCursor)
        self.kind.setToolTip("Show one type")
        self.kind.setStyleSheet(theme.button_style(12, "0px 11px", extra="font-size: 12px;")
                                + "QPushButton::menu-indicator { width: 0; image: none; }")
        self.kind_menu = QMenu(self.kind)
        theme.rounded_menus(self.kind_menu)
        self.kind_actions: dict[str, object] = {}
        self.kind.setMenu(self.kind_menu)
        find.addSpacing(4)
        find.addWidget(self.kind)
        find.addStretch(1)
        col.addWidget(self.finder)
        self.showing = str(QSettings().value(SHOW_KEY, "all"))
        if self.showing not in self.chips:
            self.showing = "all"
        self.chips[self.showing].setChecked(True)
        self.type_shown = str(QSettings().value(TYPE_KEY, ""))
        self._types_shown: tuple = ()
        self.here: set[str] | None = None  # the sounds heard in the bloom you're building ("This bloom")
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFixedHeight(STRIP_H + 12)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        holder = QWidget()
        self.row = QHBoxLayout(holder)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(10)
        self.row.addStretch(1)
        self.scroll.setWidget(holder)
        col.addWidget(self.scroll)
        self.fade = _Fade(self.scroll)
        self.fade.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.scroll.horizontalScrollBar().rangeChanged.connect(lambda *_: self._place_fade())
        self.scroll.horizontalScrollBar().valueChanged.connect(lambda *_: self._place_fade())
        self.none_found = QLabel("No sound matches.")
        self.none_found.setStyleSheet(f"color: {theme.MUTED}; padding: 4px;")
        col.addWidget(self.none_found)
        self.empty = QLabel("No sounds yet: + adds a mic, desktop sound or app sound.")
        self.empty.setStyleSheet(f"color: {theme.MUTED}; padding: 4px;")
        col.addWidget(self.empty)
        self.is_open = QSettings().value(FOLD_KEY, True, type=bool)
        self.set_open(self.is_open)

    def set_where(self, bloom: str) -> None:
        """"Plays in <bloom>": whose sound you're hearing while you build."""
        self.where.setText(f"Plays in {bloom}" if bloom else "")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_fade()

    def _place_fade(self) -> None:
        bar = self.scroll.horizontalScrollBar()
        more = bar.maximum() > 0 and bar.value() < bar.maximum()
        self.fade.setGeometry(self.scroll.width() - 56, 0, 56, self.scroll.height())
        self.fade.setVisible(more)
        self.fade.raise_()

    def set_open(self, on: bool) -> None:
        self.is_open = on
        QSettings().setValue(FOLD_KEY, on)
        self.toggle.setText("Sound" + ("" if on else "  ▸"))
        self.scroll.setVisible(on and bool(self.strips))
        self.empty.setVisible(on and not self.strips)
        self.finder.setVisible(on and bool(self.strips))
        self._filter()

    def _pick(self, key: str) -> None:
        self.showing = key
        QSettings().setValue(SHOW_KEY, key)
        self._filter()

    def _fill_types(self) -> None:
        """The Type pill's list: every type (with how many sounds it has), then New type and Edit."""
        counts = {g: sum(s.group == g for s in self.strips.values()) for g in GROUPS}
        if self.type_shown and self.type_shown not in GROUPS:
            self.type_shown = ""  # it was removed or renamed
        key = (tuple((g, group_colour(g), counts[g]) for g in GROUPS), self.type_shown)
        if key == self._types_shown:
            return
        self._types_shown = key
        menu = self.kind_menu
        menu.clear()
        self.kind_actions = {"": menu.addAction("All types", lambda: self._pick_type(""))}
        for g in GROUPS:
            self.kind_actions[g] = menu.addAction(_dot(group_colour(g)), f"{g} ({counts[g]})",
                                                  lambda g=g: self._pick_type(g))
        menu.addSeparator()
        # Opened after the menu has closed, as right-click → Type does: a window opened from inside the button's
        # menu was missing for Windows' UI Automation (PC 2026-11-20) and could swallow the click.
        later = lambda name: QTimer.singleShot(0, self, lambda: self.on_types(name))  # noqa: E731
        menu.addAction("New type…", lambda: later(None))
        if self.type_shown:
            menu.addAction(f"Edit {self.type_shown}…", lambda g=self.type_shown: later(g))
        self.kind.setText(f"{self.type_shown or 'All types'}  ▾")
        self.kind.setIcon(_dot(group_colour(self.type_shown)) if self.type_shown else QIcon())

    def _pick_type(self, group: str) -> None:
        self.type_shown = group
        QSettings().setValue(TYPE_KEY, group)
        self._filter()

    def set_here(self, ids: set[str] | None) -> None:
        """The sounds heard in the bloom you're building (for "This bloom")."""
        self.here = set(ids) if ids is not None else None
        self._filter()

    def matches(self, sid: str, strip: "Strip") -> bool:
        text = self.search.text().strip().casefold() if hasattr(self, "search") else ""
        if text and text not in strip.full_name.casefold():
            return False
        group = getattr(self, "type_shown", "")
        if group and group in GROUPS and strip.group != group:
            return False
        return getattr(self, "showing", "all") != "here" or self.here is None or sid in self.here

    def _filter(self) -> None:
        """Show the strips the search, chip and type let through; each type says how many it has."""
        if not hasattr(self, "chips"):
            return
        shown = 0
        for sid, strip in self.strips.items():
            keep = self.matches(sid, strip)
            strip.setVisible(keep)
            shown += keep
        self._fill_types()
        self.chips["all"].setText(f"All {len(self.strips)}")
        open_ = getattr(self, "is_open", True)
        self.none_found.setVisible(open_ and bool(self.strips) and not shown)

    def show_sound(self, sounds: list, kinds: dict[str, str], tags: dict[str, str] | None = None) -> None:
        """Sound items (app/sound_card.Sound), each one's source kind (for its icon) and where it plays."""
        shape = tuple(s.id for s in sounds)
        if shape != self._shape:
            # Only the sounds that came or went: making every strip again held the window ~0.3 s while live
            # (PC test, 2026-11-17). The ones kept move into the new order.
            self._shape = shape
            kinds_now = getattr(self, "_kinds", {})
            kept = {sid: strip for sid, strip in self.strips.items()
                    if sid in shape and kinds_now.get(sid) == kinds.get(sid)}
            for sid, strip in self.strips.items():
                if sid not in kept:
                    self.row.removeWidget(strip)
                    strip.deleteLater()
            self.strips = {}
            for at, sound in enumerate(sounds):
                strip = kept.get(sound.id)
                if strip is None:
                    strip = Strip(sound, kinds.get(sound.id, ""), self.on_change, self.on_open, self.on_menu)
                else:
                    self.row.removeWidget(strip)
                self.row.insertWidget(at, strip)
                self.strips[sound.id] = strip
            self.set_open(self.is_open)
        self._kinds = dict(kinds)
        for sound in sounds:
            strip = self.strips[sound.id]
            group = getattr(sound, "group", "Other") or "Other"
            if (group, group_colour(group)) != (strip.group, getattr(strip, "shade", None)):  # follows its type
                strip.group = group
                strip.shade = group_colour(group)
                strip.name.setIcon(icons.icon(_kind_icon(kinds.get(sound.id, "")), group_colour(group), 14))
                strip._selected = None
            strip.show_sound(sound)
            strip.set_tag((tags or {}).get(sound.id, ""))
        self.set_selected(self.selected)
        self._filter()

    def set_selected(self, source_id: str | None) -> None:
        """Ring the strip whose controls are open on the right."""
        self.selected = source_id
        for sid, strip in self.strips.items():
            strip.set_selected(sid == source_id)

    def set_levels(self, levels: dict[str, tuple[float, float]]) -> None:
        if not self.is_open:
            return
        for sid, strip in self.strips.items():
            if sid in levels:
                strip.fader.set_db(levels[sid][1])
