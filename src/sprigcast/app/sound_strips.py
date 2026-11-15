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
The name or ⋯ opens the sound's full controls on the right. Fold the row away with the heading.
"""

from __future__ import annotations

from collections.abc import Callable

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
STRIP_W, STRIP_H = 96, 220
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

    def __init__(self, sound, kind: str, on_change: Callable[[str, dict], None], on_open: Callable[[str], None]):
        super().__init__()
        self.sound_id = sound.id
        self.on_change, self.on_open = on_change, on_open
        self.setObjectName("soundStrip")
        self.setFixedSize(STRIP_W, STRIP_H)
        self.setStyleSheet(f"#soundStrip {{ background: {theme.CARD}; border-radius: 18px; }}")
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
        self.mute.clicked.connect(lambda on: self.on_change(self.sound_id, {"muted": on}))
        more = QPushButton()
        more.setIcon(icons.icon("gear", theme.MUTED, 15))
        more.setFixedSize(34, 26)
        more.setCursor(Qt.CursorShape.PointingHandCursor)
        more.setToolTip("Volume, who hears it, filters and tracks")
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

    def _volume(self, db: float) -> None:
        self.db.setText(f"{db:+.0f} dB" if db else "0 dB")
        self.on_change(self.sound_id, {"volume_db": db})

    def show_sound(self, sound) -> None:
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


class SoundStrips(QWidget):
    """The row of strips, with a heading that folds it away (remembered)."""

    def __init__(self, on_change: Callable[[str, dict], None], on_open: Callable[[str], None]):
        super().__init__()
        self.on_change, self.on_open = on_change, on_open
        self.strips: dict[str, Strip] = {}
        self._shape: tuple = ()
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        self.toggle = QPushButton()
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setStyleSheet(f"QPushButton {{ background: transparent; border: none; color: {theme.TEXT_BRIGHT};"
                                  " font-weight: 700; font-size: 14px; text-align: left; padding: 0 4px; }")
        self.toggle.clicked.connect(lambda: self.set_open(not self.is_open))
        col.addWidget(self.toggle)
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
        self.empty = QLabel("No sounds yet: + beside Sound on the left adds a mic, desktop or app sound.")
        self.empty.setStyleSheet(f"color: {theme.MUTED}; padding: 4px;")
        col.addWidget(self.empty)
        self.is_open = QSettings().value(FOLD_KEY, True, type=bool)
        self.set_open(self.is_open)

    def set_open(self, on: bool) -> None:
        self.is_open = on
        QSettings().setValue(FOLD_KEY, on)
        self.toggle.setText(("▾  " if on else "▸  ") + "Sound")
        self.scroll.setVisible(on and bool(self.strips))
        self.empty.setVisible(on and not self.strips)

    def show_sound(self, sounds: list, kinds: dict[str, str]) -> None:
        """Sound items (app/sound_card.Sound) and each one's source kind, for its icon."""
        shape = tuple(s.id for s in sounds)
        if shape != self._shape:
            self._shape = shape
            for strip in self.strips.values():
                strip.deleteLater()
            self.strips = {}
            for sound in sounds:
                strip = Strip(sound, kinds.get(sound.id, ""), self.on_change, self.on_open)
                self.row.insertWidget(self.row.count() - 1, strip)
                self.strips[sound.id] = strip
            self.set_open(self.is_open)
        for sound in sounds:
            self.strips[sound.id].show_sound(sound)

    def set_levels(self, levels: dict[str, tuple[float, float]]) -> None:
        if not self.is_open:
            return
        for sid, strip in self.strips.items():
            if sid in levels:
                strip.fader.set_db(levels[sid][1])
