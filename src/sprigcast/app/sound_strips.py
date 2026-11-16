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

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import icons, theme

LOW, HIGH = -60.0, 12.0  # the fader's range, as on the Tune page
STRIP_W, STRIP_H = 104, 220
FOLD_KEY = "build/sound_strips_open"


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
        self.name.setIcon(icons.icon(_kind_icon(kind), theme.MUTED, 14))
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
            f"QPushButton {{ background: {theme.RAISED}; border: none; border-radius: 13px; }}"
            f"QPushButton:hover {{ background: {theme.RAISED_HOVER}; }}"
            f"QPushButton:checked {{ background: {theme.SELECT_BG}; }}")
        self.mute.clicked.connect(self._mute_clicked)
        more = QPushButton()
        more.setIcon(icons.icon("gear", theme.MUTED, 15))
        more.setFixedSize(34, 26)
        more.setCursor(Qt.CursorShape.PointingHandCursor)
        more.setToolTip("Volume, listeners, filters and tracks")
        more.setStyleSheet(f"QPushButton {{ background: transparent; border: none; border-radius: 13px;"
                           f" color: {theme.MUTED}; font-size: 16px; }}"
                           f"QPushButton:hover {{ background: {theme.RAISED}; color: {theme.TEXT}; }}")
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
                           f" border: 1px solid {edge}; border-radius: 18px; }}")

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
        name, db, _muted = getattr(self, "_shown", None) or (self.full_name, self.fader.db, not on)
        self.show_sound(SimpleNamespace(name=name, volume_db=db, muted=on))
        self.on_change(self.sound_id, {"muted": on})

    def _volume(self, db: float) -> None:
        self.db.setText(f"{db:+.0f} dB" if db else "0 dB")
        self.on_change(self.sound_id, {"volume_db": db})

    def show_sound(self, sound) -> None:
        shown = (sound.name, sound.volume_db, sound.muted)
        if getattr(self, "_shown", None) == shown:
            return  # nothing to redo: every change used to reset every strip's labels and icons
        self._shown = shown
        self.full_name = sound.name
        metrics = QFontMetrics(_bold(self.name.font()))
        self.name.setText(metrics.elidedText(sound.name, Qt.TextElideMode.ElideRight, STRIP_W - 36))
        self.name.setToolTip(f"{sound.name}: open its controls")
        if self.fader.db != sound.volume_db:
            self.fader.db = sound.volume_db
        self.fader.muted = sound.muted
        self.fader.update()
        self.db.setText("muted" if sound.muted else f"{sound.volume_db:+.0f} dB" if sound.volume_db else "0 dB")
        self.mute.setChecked(sound.muted)
        self.mute.setIcon(icons.icon("speaker-off" if sound.muted else "speaker",
                                     theme.SELECT_TEXT if sound.muted else theme.TEXT, 16))
        self.mute.setToolTip("Unmute" if sound.muted else "Mute")


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
                 on_menu: Callable[[str | None, object], None] | None = None):
        super().__init__()
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
        self.add.setStyleSheet(f"QPushButton {{ background: {theme.RAISED}; border: none; border-radius: 13px; }}"
                               f"QPushButton:hover {{ background: {theme.RAISED_HOVER}; }}")
        self.add.clicked.connect(lambda: self.on_menu(None, self.add.mapToGlobal(self.add.rect().bottomLeft())))
        head.addWidget(self.add)
        col.addLayout(head)
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

    def show_sound(self, sounds: list, kinds: dict[str, str], tags: dict[str, str] | None = None) -> None:
        """Sound items (app/sound_card.Sound), each one's source kind (for its icon) and where it plays."""
        shape = tuple(s.id for s in sounds)
        if shape != self._shape:
            self._shape = shape
            for strip in self.strips.values():
                strip.deleteLater()
            self.strips = {}
            for sound in sounds:
                strip = Strip(sound, kinds.get(sound.id, ""), self.on_change, self.on_open, self.on_menu)
                self.row.insertWidget(self.row.count() - 1, strip)
                self.strips[sound.id] = strip
            self.set_open(self.is_open)
        for sound in sounds:
            self.strips[sound.id].show_sound(sound)
            self.strips[sound.id].set_tag((tags or {}).get(sound.id, ""))
        self.set_selected(self.selected)

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
