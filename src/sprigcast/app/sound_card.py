"""Live's Sound card: sounds in groups, showing only what's playing, with a Tune page per sound.

    Sound                         Edit
    ● Voice            1     [Mute]
    ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬
      Mic                    −6 dB
      ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬
    ● Alerts           6     [Mute]
      ...
      5 quiet ▸

A group's meter is its loudest sound. A sound shows under its group while it is playing (and for a
few seconds after), when it is pinned, or when it is muted (so a muted mic is never forgotten);
the rest fold into "N quiet". Edit shows every sound. Clicking a sound opens its Tune page:
volume, left/right, mono, delay, listeners, filters, group, pin and ducking.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.live_stage import Elided, Switch, ThinMeter, heading

GROUPS = ("Voice", "Game", "Alerts", "Music", "Other")
GROUP_COLOURS = {"Voice": theme.ACCENT, "Game": theme.INFO, "Alerts": theme.WARN, "Music": "#c79bff",
                 "Other": theme.MUTED}
HEARD_DB = -50.0  # RMS above this counts as playing
LINGER_S = 4.0  # a sound stays listed this long after it goes quiet
CHIP_STYLE = (
    f"QPushButton {{ background: {theme.RAISED}; color: {theme.TEXT}; border: none; border-radius: 11px;"
    f" padding: 3px 11px; min-height: 16px; font-size: 11px; }}"
    f"QPushButton:hover {{ background: {theme.RAISED_HOVER}; }}"
    f"QPushButton:checked {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}"
)
MUTED_CHIP_STYLE = CHIP_STYLE + f"QPushButton {{ background: {theme.LIVE}; color: #ffffff; }}"
_MUSIC = re.compile(r"music|spotify|song|pretzel|playlist|radio", re.IGNORECASE)
_ALERTS = re.compile(r"alert|tts|speech|ad break|chat|tikfinity|streamer\.?bot|speaker\.?bot|sound ?alert|browser|"
                     r"donat|sub|follow|bits", re.IGNORECASE)
_GAME = re.compile(r"game|desktop|discord|steam", re.IGNORECASE)


def auto_group(name: str, kind: str) -> str:
    """Where a sound goes until you move it: mics are Voice; names hint at the rest."""
    if kind == "audio_input":
        return "Voice"
    if kind == "app_audio":  # named for its type: Music, Voice chat, SFX...
        if re.search(r"voice chat", name, re.IGNORECASE):
            return "Voice"
        if re.search(r"text to speech|sfx", name, re.IGNORECASE):
            return "Alerts"
    if _MUSIC.search(name):
        return "Music"
    if _ALERTS.search(name):
        return "Alerts"
    if _GAME.search(name) or kind == "audio_output":
        return "Game"
    return "Other"


@dataclass
class Sound:
    id: str
    name: str
    muted: bool = False
    group: str = "Other"
    pinned: bool = False
    volume_db: float = 0.0
    monitor: bool | str = False  # False: stream only; True: me + stream; "only": just me
    mono: bool = False
    balance: float = 0.0
    delay_ms: int = 0
    voice: bool = False
    filters: list[str] = field(default_factory=list)
    can_monitor: bool = True
    tracks: tuple[str, ...] = ("stream", "recording", "clips")  # the named tracks it goes to
    apps: str | None = None  # an app sound: what it catches ("Spotify, Apple Music…"); None for other sounds


class VolumeMeter(ThinMeter):
    """A sound row's level bar with a volume knob on it: drag the knob (or anywhere on the bar) to set
    the volume right there, double-click for 0 dB. The bar still shows how loud it is."""

    LOW, HIGH = -60, 12  # the Tune page's range

    def __init__(self, db: float, changed: Callable[[float], None]):
        super().__init__()
        self.db = db
        self.changed = changed
        self.setFixedHeight(14)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setToolTip("Drag to set the volume (double-click: 0 dB)")

    def _x(self) -> float:
        span = self.width() - 12
        return 6 + span * (self.db - self.LOW) / (self.HIGH - self.LOW)

    def _set_from(self, x: float) -> None:
        span = max(1, self.width() - 12)
        db = round(self.LOW + (x - 6) / span * (self.HIGH - self.LOW))
        db = max(self.LOW, min(self.HIGH, 0 if abs(db) <= 1 else db))  # 0 dB is easy to land on
        if db != self.db:
            self.db = db
            self.update()
            self.changed(float(db))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._set_from(event.position().x())
        event.accept()  # not the row's click (that opens Tune)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._set_from(event.position().x())
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self.db != 0:
            self.db = 0
            self.update()
            self.changed(0.0)
        event.accept()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        bar = QRectF(0, self.height() / 2 - 2, self.width(), 4)
        p.setBrush(QColor(theme.TRACK))
        p.drawRoundedRect(bar, 2, 2)
        if self.level > 0 and not self.muted:
            colour = self.colour or theme.ACCENT
            if self.level >= 0.85:
                colour = theme.LIVE
            p.setBrush(QColor(colour))
            p.drawRoundedRect(QRectF(0, bar.top(), max(4.0, bar.width() * self.level), 4), 2, 2)
        p.setBrush(QColor(theme.TEXT_BRIGHT if not self.muted else theme.MUTED))
        p.setPen(QPen(QColor(self.colour or theme.ACCENT), 2))
        p.drawEllipse(QPointF(self._x(), self.height() / 2), 5, 5)
        p.end()


class ClickRow(QWidget):
    """A sound's row: name, volume and a thin meter; a click opens its Tune page."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def click(self) -> None:
        self.clicked.emit()


def _chip(text: str, style: str = CHIP_STYLE, checkable: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setStyleSheet(style)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setCheckable(checkable)
    return b


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.TEXT}; background: transparent; font-weight: 600;")
    return label


def _muted_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
    return label


class SoundCard(QWidget):
    """on_change(source_id, {setting: value}); on_filters(source_id) opens that sound's filters."""

    def __init__(self, on_change: Callable[[str, dict], None], on_filters: Callable[[str], None],
                 on_mic_first: Callable[[bool], None] | None = None):
        super().__init__()
        self.on_change, self.on_filters = on_change, on_filters
        self.on_settings: Callable[[], None] | None = None  # opens Settings → Audio → Sounds
        self.sounds: dict[str, Sound] = {}
        self.editing = False
        self.heard: dict[str, float] = {}
        self.open_groups: set[str] = set()  # groups showing their sounds
        self.meters: dict[str, ThinMeter] = {}
        self.notes: dict[str, QLabel] = {}  # each sound row's "+3 dB" / "muted"
        self.group_meters: dict[str, ThinMeter] = {}
        self.rows: dict[str, QWidget] = {}
        self.parts: dict[str, dict] = {}
        self.tuning: str | None = None
        self.names: dict[str, QLabel] = {}
        self._muted_shown: dict[str, bool] = {}
        self._shape: tuple | None = None
        self._lit: dict[str, tuple] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.pages = QStackedWidget()
        root.addWidget(self.pages)

        # Page 0: the groups.
        overview = QWidget()
        col = QVBoxLayout(overview)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        top = QHBoxLayout()
        top.addWidget(heading("Sound"), 1)
        self.edit = QPushButton("Edit")
        self.edit.setStyleSheet(theme.LINK_STYLE)
        self.edit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit.setToolTip("Show every sound, ready to tune")
        self.edit.clicked.connect(lambda: self.set_editing(not self.editing))
        top.addWidget(self.edit)
        self.settings = QPushButton("⚙")
        self.settings.setStyleSheet(theme.LINK_STYLE)
        self.settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings.setToolTip("Every sound in one table: volume, mono, balance, sync offset, tracks")
        self.settings.clicked.connect(lambda: self.on_settings and self.on_settings())
        top.addWidget(self.settings)
        col.addLayout(top)
        first = QHBoxLayout()
        first.setSpacing(10)
        label = QLabel("Mic first")
        label.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        hint = Elided("others dip when you talk")  # a narrow card shortens it rather than cutting the switch off
        hint.setMinimumWidth(0)
        hint.setStyleSheet(f"color: {theme.MUTED}; background: transparent; font-size: 12px;")
        self.mic_first = Switch()
        self.mic_first.setToolTip("Ducking: while your mic is talking, every other sound glides down, "
                                  "then back up when you stop")
        self.mic_first.toggled.connect(lambda on: (on_mic_first or (lambda _on: None))(on))
        first.addWidget(label)
        first.addWidget(hint, 1)
        first.addWidget(self.mic_first)
        col.addLayout(first)
        self.body = QVBoxLayout()
        self.body.setSpacing(16)
        col.addLayout(self.body)
        col.addStretch(1)  # spare height goes below the groups, never between them
        self.pages.addWidget(overview)

        # Page 1: tune one sound.
        self.tune = TunePage(self._tune_changed, lambda sid: self.on_filters(sid), self.close_tune)
        self.pages.addWidget(self.tune)
        self.pages.currentChanged.connect(self._fit_page)
        self._fit_page(0)

    def _fit_page(self, index: int) -> None:
        """The card is as tall as the page showing, not the tallest page, so panels below get the rest."""
        for i in range(self.pages.count()):
            policy = QSizePolicy.Policy.Preferred if i == index else QSizePolicy.Policy.Ignored
            self.pages.widget(i).setSizePolicy(policy, policy)  # a hidden page doesn't widen the card either
        self.pages.updateGeometry()

    def set_mic_first(self, on: bool) -> None:
        self.mic_first.blockSignals(True)
        self.mic_first.setChecked(on)
        self.mic_first.blockSignals(False)

    # Data ---------------------------------------------------------------------------------

    def show_sound(self, sounds: list) -> None:
        """Sound items (or old-style (id, name, muted) tuples)."""
        items = [s if isinstance(s, Sound) else Sound(s[0], s[1], bool(s[2])) for s in sounds]
        self.sounds = {s.id: s for s in items}
        # Muting changes colours in place (_show_muted): rebuilding every row for it froze the window.
        shape = tuple((s.id, s.name, s.group, s.pinned) for s in items)
        if shape != self._shape:
            self._shape = shape
            self._build()
        self._show_muted()
        for sid, sound in self.sounds.items():  # volume changes in place: a drag isn't cut off by a rebuild
            note = self.notes.get(sid)
            text = "muted" if sound.muted else f"{sound.volume_db:+.0f} dB" if sound.volume_db else ""
            if note is not None and note.text() != text:
                note.setText(text)
            meter = self.meters.get(sid)
            if isinstance(meter, VolumeMeter) and meter.db != sound.volume_db:
                meter.db = sound.volume_db
                meter.update()
        if self.tuning in self.sounds:
            self.tune.show_sound(self.sounds[self.tuning])
        elif self.tuning is not None:
            self.close_tune()
        self._apply_visibility()

    def set_levels(self, levels: dict[str, tuple[float, float]], now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        loudest: dict[str, tuple[float, str]] = {}
        for source_id, meter in self.meters.items():
            if source_id not in levels:
                continue
            rms = levels[source_id][1]
            meter.set_db(rms)
            if rms > HEARD_DB:
                self.heard[source_id] = now
            sound = self.sounds.get(source_id)
            if sound is not None and not sound.muted and rms > loudest.get(sound.group, (-61.0, ""))[0]:
                loudest[sound.group] = (rms, sound.name)
        for group, meter in self.group_meters.items():
            meter.set_db(loudest.get(group, (-60.0, ""))[0])
        if self.tuning in levels:
            self.tune.meter.set_db(levels[self.tuning][1])
        self._apply_visibility(now, {g: name for g, (_, name) in loudest.items()})

    # Building -----------------------------------------------------------------------------

    def _build(self) -> None:
        while self.body.count():
            widget = self.body.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.meters, self.group_meters, self.rows, self.parts, self.notes = {}, {}, {}, {}, {}
        self.names: dict[str, QLabel] = {}
        self._muted_shown: dict[str, bool] = {}  # what each row and group head shows now
        self._lit = {}
        if not self.sounds:
            from sprigcast.app.empty import EmptyState

            self.body.addWidget(EmptyState("No mic or desktop audio yet. Add them in Build with + Add.", scale=0.7))
            return
        for group in GROUPS:
            members = [s for s in self.sounds.values() if s.group == group]
            if members:
                box = self._group_box(group, members)
                self.body.addWidget(box)
                box.show()  # a layout shows new children later; show now so visibility below sticks

    def _group_box(self, group: str, members: list[Sound]) -> QWidget:
        colour = GROUP_COLOURS[group]
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)
        head_row = ClickRow()
        head_row.setCursor(Qt.CursorShape.PointingHandCursor)
        head_row.setToolTip(f"Show the {group} sounds")
        head_row.clicked.connect(lambda g=group: self.toggle_group(g))
        head = QHBoxLayout(head_row)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(10)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {colour}; background: transparent;")
        name = QLabel(group)
        font = name.font()
        font.setWeight(font.Weight.DemiBold)
        name.setFont(font)
        sub = Elided("")
        sub.setMinimumWidth(0)
        sub.setStyleSheet(f"color: {theme.MUTED}; background: transparent; font-size: 12px;")
        for label in (dot, name, sub):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        mute = _chip("Mute")  # its words and colour come from _show_muted
        ids = [s.id for s in members]
        mute.clicked.connect(lambda _=False, ids=ids: self._mute_all(
            ids, not all(self.sounds[i].muted for i in ids if i in self.sounds)))
        head.addWidget(dot)
        head.addWidget(name)
        head.addWidget(sub, 1)
        head.addWidget(mute)
        col.addWidget(head_row)
        meter = ThinMeter()
        meter.colour = colour
        self.group_meters[group] = meter
        col.addWidget(meter)
        inner_box = QWidget()
        inner = QVBoxLayout(inner_box)
        inner.setContentsMargins(20, 4, 0, 2)
        inner.setSpacing(6)
        for sound in members:
            row = self._sound_row(sound, colour)
            self.rows[sound.id] = row
            inner.addWidget(row)
        col.addWidget(inner_box)
        self.parts[group] = {"name": name, "sub": sub, "inner": inner_box, "dot": dot, "mute": mute, "ids": ids,
                             "muted": [], "count": len(members)}
        return box

    def _sound_row(self, sound: Sound, colour: str) -> QWidget:
        row = ClickRow()
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setToolTip("Tune this sound: volume, left/right, mono, delay, filters…")
        row.clicked.connect(lambda sid=sound.id: self.open_tune(sid))
        col = QVBoxLayout(row)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)  # the volume bar has its own breathing room (it's 14 px tall for the knob)
        line = QHBoxLayout()
        line.setSpacing(8)
        name = QLabel(sound.name)  # its colour, and the note's, come from _show_muted
        name.setWordWrap(True)
        self.names[sound.id] = name
        note = "muted" if sound.muted else f"{sound.volume_db:+.0f} dB" if sound.volume_db else ""
        right = QLabel(note)
        for label in (name, right):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        line.addWidget(name, 1)
        line.addWidget(right)
        col.addLayout(line)
        self.notes[sound.id] = right
        meter = VolumeMeter(sound.volume_db, lambda db, sid=sound.id: self._drag_volume(sid, db))
        meter.colour = colour
        self.meters[sound.id] = meter
        col.addWidget(meter)
        return row

    def _show_muted(self) -> None:
        """Muted or not, in place: each row's name, note and bar, and each group's Mute chip and bar."""
        for sid, sound in self.sounds.items():
            name = self.names.get(sid)
            if name is None or self._muted_shown.get(sid) == sound.muted:
                continue
            self._muted_shown[sid] = sound.muted
            name.setStyleSheet(f"color: {theme.MUTED if sound.muted else theme.TEXT}; background: transparent;")
            self.notes[sid].setStyleSheet(f"color: {theme.LIVE_TEXT if sound.muted else theme.MUTED};"
                                          " background: transparent; font-size: 12px;")
            self.meters[sid].muted = sound.muted
            self.meters[sid].update()
        for group, part in self.parts.items():
            members = [self.sounds[i] for i in part["ids"] if i in self.sounds]
            muted = [s.name for s in members if s.muted]
            if muted != part["muted"]:
                part["muted"] = muted
                self._lit.pop(group, None)  # its "N muted" line redraws
            all_muted = bool(members) and all(s.muted for s in members)
            key = "group:" + group
            if self._muted_shown.get(key) == all_muted:
                continue
            self._muted_shown[key] = all_muted
            chip = part["mute"]
            chip.setText("Muted" if all_muted else "Mute")
            chip.setStyleSheet(MUTED_CHIP_STYLE if all_muted else CHIP_STYLE)
            chip.setToolTip(f"Unmute every {group} sound" if all_muted else f"Mute every {group} sound")
            self.group_meters[group].muted = all_muted
            self.group_meters[group].update()

    def _drag_volume(self, sound_id: str, db: float) -> None:
        sound = self.sounds.get(sound_id)
        if sound is not None:
            sound.volume_db = db
        note = self.notes.get(sound_id)
        if note is not None and not (sound and sound.muted):
            note.setText(f"{db:+.0f} dB" if db else "")
        self.on_change(sound_id, {"volume_db": db})

    # Calm by default ----------------------------------------------------------------------

    def is_open(self, group: str) -> bool:
        return self.editing or group in self.open_groups

    def _apply_visibility(self, now: float | None = None, playing: dict[str, str] | None = None) -> None:
        """A group lights up (its colour) while one of its sounds plays, and names that sound;
        a quiet group is dimmed. Its sounds show only when you open it (or press Edit)."""
        now = time.monotonic() if now is None else now
        playing = playing or {}
        for group, part in self.parts.items():
            opened = self.is_open(group)
            any_row = False
            for sid, s in self.sounds.items():
                if s.group == group and sid in self.rows:
                    show = opened or s.pinned  # "Always show" keeps a sound listed when its group is shut
                    any_row |= show
                    if self.rows[sid].isVisibleTo(self) != show:
                        self.rows[sid].setVisible(show)
            if part["inner"].isVisibleTo(self) != any_row:
                part["inner"].setVisible(any_row)
            heard = [sid for sid, s in self.sounds.items()
                     if s.group == group and not s.muted and now - self.heard.get(sid, -1e9) < LINGER_S]
            lit = bool(heard)
            if part["muted"] and not lit:
                text = f"{len(part['muted'])} muted" if len(part["muted"]) > 1 else f"{part['muted'][0]} muted"
                style = f"color: {theme.LIVE_TEXT}; background: transparent; font-size: 12px;"
            elif lit:
                text = playing.get(group) or self.sounds[heard[0]].name
                style = f"color: {theme.TEXT}; background: transparent; font-size: 12px;"
            else:
                text = f"{part['count']} quiet" if part["count"] > 1 else "quiet"
                style = f"color: {theme.MUTED}; background: transparent; font-size: 12px;"
            key = (lit, text, opened)
            if self._lit.get(group) == key:
                continue
            self._lit[group] = key
            part["sub"].setText(text)
            part["sub"].setStyleSheet(style)
            part["name"].setStyleSheet(
                f"color: {GROUP_COLOURS[group] if lit else theme.MUTED}; background: transparent;")
            part["dot"].setText("▾" if opened else "●")

    def toggle_group(self, group: str) -> None:
        self.open_groups ^= {group}
        self._apply_visibility()

    def set_editing(self, on: bool) -> None:
        self.editing = on
        self.edit.setText("Done" if on else "Edit")
        self._apply_visibility()

    def _mute_all(self, ids: list[str], on: bool) -> None:
        for sound_id in ids:  # shown at once here; the rest of the window catches up when changes settle
            if sound_id in self.sounds:
                self.sounds[sound_id].muted = on
        self._show_muted()
        self._apply_visibility()
        for sound_id in ids:
            self.on_change(sound_id, {"muted": on})

    # Tune page ----------------------------------------------------------------------------

    def open_tune(self, sound_id: str) -> None:
        if sound_id not in self.sounds:
            return
        self.tuning = sound_id
        self.tune.show_sound(self.sounds[sound_id])
        self.pages.setCurrentIndex(1)

    def close_tune(self) -> None:
        self.tuning = None
        self.pages.setCurrentIndex(0)

    def _tune_changed(self, sound_id: str, changes: dict) -> None:
        sound = self.sounds.get(sound_id)
        if sound is not None:
            for key, value in changes.items():
                attr = {"volume_db": "volume_db", "muted": "muted", "monitor": "monitor", "mono": "mono",
                        "balance": "balance", "delay_ms": "delay_ms", "duck_voice": "voice",
                        "group": "group", "pinned": "pinned", "tracks": "tracks"}.get(key)
                if attr:
                    setattr(sound, attr, tuple(value) if attr == "tracks" else value)
        self.on_change(sound_id, changes)


class PillPick(QPushButton):
    """A pill showing one choice; clicking it lists the others. Speaks QComboBox's text API."""

    currentTextChanged = Signal(str)  # noqa: N815 (Qt naming)

    def __init__(self, choices: tuple[str, ...] | list[str]):
        super().__init__()
        self.choices = list(choices)
        self._text = self.choices[0]
        self.setStyleSheet(CHIP_STYLE + "QPushButton::menu-indicator { width: 0; image: none; }")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        menu = QMenu(self)
        for choice in self.choices:
            menu.addAction(choice, lambda c=choice: self.setCurrentText(c))
        self.setMenu(menu)
        self._show()

    def currentText(self) -> str:  # noqa: N802
        return self._text

    def setCurrentText(self, text: str) -> None:  # noqa: N802
        if text not in self.choices or text == self._text:
            return
        self._text = text
        self._show()
        self.currentTextChanged.emit(text)

    def _show(self) -> None:
        self.setText(f"{self._text}  ▾")


class TunePage(QWidget):
    """Everything about one sound, inside the card. Changes apply at once."""

    def __init__(self, on_change: Callable[[str, dict], None], on_filters: Callable[[str], None],
                 on_back: Callable[[], None], on_apps: Callable[[str], None] | None = None,
                 on_blooms: Callable[[str], None] | None = None):
        super().__init__()
        self.on_change = on_change
        self.sound_id = ""
        self._loading = False
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)

        top = QHBoxLayout()
        back = QPushButton("‹")
        back.setStyleSheet(theme.LINK_STYLE + "QPushButton { font-size: 18px; padding: 0px 6px 0px 0px; }")
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.setToolTip("Back to all sounds")
        back.clicked.connect(on_back)
        top.addWidget(back)
        self.title = heading("")
        self.title.setWordWrap(True)
        top.addWidget(self.title, 1)
        self.mute = _chip("Mute", checkable=True)
        self.mute.toggled.connect(lambda on: self._send({"muted": on}))
        top.addWidget(self.mute)
        col.addLayout(top)
        self.meter = ThinMeter()
        col.addWidget(self.meter)

        # Sound per bloom (Build only): which blooms it plays in, and a way to pick them.
        self.blooms_card, box = self._section(col)
        blooms_top = QHBoxLayout()
        blooms_top.addWidget(_section_title("Plays in"), 1)
        pick = QPushButton("Change")
        pick.setStyleSheet(theme.LINK_STYLE)
        pick.setCursor(Qt.CursorShape.PointingHandCursor)
        pick.setToolTip("Pick the blooms this sound plays in")
        pick.clicked.connect(lambda: on_blooms and on_blooms(self.sound_id))
        blooms_top.addWidget(pick)
        box.addLayout(blooms_top)
        self.blooms = _muted_label("")
        self.blooms.setWordWrap(True)
        box.addWidget(self.blooms)
        self.blooms_card.setVisible(on_blooms is not None)

        # An app sound (Games, Music, Voice chat...): which apps it catches, and a way to change them.
        self.apps_card, box = self._section(col)
        apps_top = QHBoxLayout()
        apps_top.addWidget(_section_title("Comes from"), 1)
        change = QPushButton("Change")
        change.setStyleSheet(theme.LINK_STYLE)
        change.setCursor(Qt.CursorShape.PointingHandCursor)
        change.setToolTip("Pick the apps this sound catches")
        change.clicked.connect(lambda: on_apps and on_apps(self.sound_id))
        change.setVisible(on_apps is not None)
        apps_top.addWidget(change)
        box.addLayout(apps_top)
        self.apps = _muted_label("")
        self.apps.setWordWrap(True)
        box.addWidget(self.apps)
        self.apps_card.hide()

        # Four calm sections, each its own soft card with room inside: how loud, where it goes,
        # filters, and the rest. Nothing touches an edge.
        level, box = self._section(col)
        self.volume_label = _muted_label("")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(-60, 12)
        self.volume.valueChanged.connect(self._volume_moved)
        box.addLayout(self._labelled("Volume", self.volume_label))
        box.addWidget(self.volume)
        box.addSpacing(4)
        self.balance_label = _muted_label("")
        self.balance = QSlider(Qt.Orientation.Horizontal)
        self.balance.setRange(-100, 100)
        self.balance.setPageStep(10)
        self.balance.valueChanged.connect(self._balance_moved)
        box.addLayout(self._labelled("Left · Right", self.balance_label))
        box.addWidget(self.balance)
        box.addSpacing(4)
        pair = QHBoxLayout()
        pair.setSpacing(10)
        self.mono = _chip("Mono", checkable=True)
        self.mono.setToolTip("Both ears hear the same: for a mic that only comes out of one side")
        self.mono.toggled.connect(lambda on: self._send({"mono": on}))
        self.mono.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)  # shares a narrow card
        self.mono.setMinimumWidth(56)
        pair.addWidget(self.mono, 1)
        pair.addWidget(_muted_label("Delay"))
        self.delay = QSpinBox()
        self.delay.setRange(0, 2000)
        self.delay.setSingleStep(10)
        self.delay.setSuffix(" ms")
        self.delay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.delay.setToolTip("Hold this sound back to line it up with a camera that runs late")
        self.delay.setStyleSheet(
            f"QSpinBox {{ background: {theme.RAISED}; border: none; border-radius: 13px; padding: 6px 12px;"
            f" color: {theme.TEXT}; }} QSpinBox:hover, QSpinBox:focus {{ background: {theme.RAISED_HOVER}; }}"
            "QSpinBox::up-button, QSpinBox::down-button { subcontrol-origin: margin; width: 0px; height: 0px;"
            " border: none; }")
        self.delay.valueChanged.connect(lambda v: self._send({"delay_ms": v}))
        self.delay.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.delay.setMinimumWidth(76)
        pair.addWidget(self.delay, 1)
        box.addLayout(pair)

        _route, box = self._section(col, "Listeners")
        hear = QHBoxLayout()
        hear.setSpacing(6)
        self.hear: dict[object, QPushButton] = {}
        for value, text, tip in ((False, "Stream", "On the stream and recordings, not your speakers"),
                                 (True, "Both", "On the stream and your speakers"),
                                 ("only", "Just me", "Only your speakers: never on the stream")):
            b = _chip(text, checkable=True)
            b.setToolTip(tip)
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)  # share the width, never overflow
            b.clicked.connect(lambda _=False, v=value: self._send({"monitor": v}))
            self.hear[value] = b
            hear.addWidget(b, 1)
        box.addLayout(hear)
        box.addSpacing(6)
        box.addWidget(_section_title("Goes to"))
        goes = QGridLayout()
        goes.setSpacing(6)
        self.goes: dict[str, QPushButton] = {}
        for i, (track, text, tip) in enumerate((("stream", "Stream", "Your live stream"),
                                                ("recording", "Recording", "The recording's main sound"),
                                                ("voice", "Voice", "The recording's voice-only track (Settings → Output → Recording)"),
                                                ("clips", "Clips", "Saved clips"))):
            b = _chip(text, checkable=True)
            b.setToolTip(tip)
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            b.clicked.connect(self._tracks_clicked)
            self.goes[track] = b
            goes.addWidget(b, i // 2, i % 2)
        box.addLayout(goes)

        _filters, box = self._section(col)
        filters_top = QHBoxLayout()
        filters_top.addWidget(_section_title("Filters"), 1)
        edit_filters = QPushButton("Edit")
        edit_filters.setStyleSheet(theme.LINK_STYLE)
        edit_filters.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_filters.setToolTip("Noise gate, compressor, EQ...")
        edit_filters.clicked.connect(lambda: on_filters(self.sound_id))
        filters_top.addWidget(edit_filters)
        box.addLayout(filters_top)
        # One filter per row, in the order they run, so five to eight of them still read at a glance.
        self.filters = QLabel("None yet")
        self.filters.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        box.addWidget(self.filters)
        self.filter_rows = QVBoxLayout()
        self.filter_rows.setSpacing(4)
        box.addLayout(self.filter_rows)

        _more, box = self._section(col)
        group_row = QHBoxLayout()
        group_row.addWidget(_muted_label("Group"), 1)
        self.group = PillPick(GROUPS)
        self.group.currentTextChanged.connect(lambda g: self._send({"group": g}))
        group_row.addWidget(self.group)
        box.addLayout(group_row)
        self.pin = Switch()
        self.pin.toggled.connect(lambda on: self._send({"pinned": on}))
        box.addLayout(self._switch_row("Always show", self.pin))
        self.duck = Switch()
        self.duck.toggled.connect(lambda on: self._send({"duck_voice": on}))
        box.addLayout(self._switch_row("Lowers others when it talks", self.duck))

    def show_plays_in(self, text: str) -> None:
        """Build's "Plays in" card: "Every bloom" or the bloom names."""
        self.blooms.setText(text)

    @staticmethod
    def _section(col: QVBoxLayout, title: str = "") -> tuple[QFrame, QVBoxLayout]:
        """A soft card inside the Sound card, padded so nothing touches its edges."""
        frame = QFrame()
        frame.setObjectName("tuneSection")
        frame.setStyleSheet(f"#tuneSection {{ background: {theme.CARD}; border-radius: 14px; }}")
        box = QVBoxLayout(frame)
        box.setContentsMargins(14, 12, 14, 14)
        box.setSpacing(8)
        if title:
            box.addWidget(_section_title(title))
        col.addWidget(frame)
        return frame, box

    def _labelled(self, text: str, value: QLabel) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(_muted_label(text), 1)
        row.addWidget(value)
        return row

    def _switch_row(self, text: str, switch: Switch) -> QHBoxLayout:
        row = QHBoxLayout()
        label = _muted_label(text)
        label.setWordWrap(True)
        row.addWidget(label, 1)
        row.addWidget(switch)
        return row

    def show_sound(self, sound: Sound) -> None:
        self._loading = True
        self.sound_id = sound.id
        self.title.setText(sound.name)
        self.apps_card.setVisible(sound.apps is not None)
        self.apps.setText(sound.apps or "")
        self.mute.setChecked(sound.muted)
        self.mute.setText("Muted" if sound.muted else "Mute")
        self.mute.setStyleSheet(MUTED_CHIP_STYLE if sound.muted else CHIP_STYLE)
        self.volume.setValue(round(sound.volume_db))
        self.volume_label.setText(f"{sound.volume_db:+.0f} dB" if sound.volume_db else "0 dB")
        self.balance.setValue(round(sound.balance * 100))
        self.balance_label.setText(self._balance_text(sound.balance))
        self.mono.setChecked(sound.mono)
        self.delay.setValue(int(sound.delay_ms))
        for value, button in self.hear.items():
            button.setChecked(sound.monitor == value or (value is True and sound.monitor is True))
            if value is not False:
                button.setEnabled(sound.can_monitor)
        for track, button in self.goes.items():
            button.setChecked(track in sound.tracks)
            button.setEnabled(sound.monitor != "only")  # "just me" goes nowhere but your speakers
        self._show_filters(sound.filters)
        self.group.setCurrentText(sound.group)
        self.pin.setChecked(sound.pinned)
        self.duck.setChecked(sound.voice)
        self._loading = False

    def _show_filters(self, names: list[str]) -> None:
        while self.filter_rows.count():
            widget = self.filter_rows.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.filters.setVisible(not names)
        for i, name in enumerate(names, 1):
            row = QLabel(f"{i}   {name}")
            row.setToolTip("Filters run top to bottom")
            row.setStyleSheet(f"background: {theme.RAISED}; color: {theme.TEXT}; border-radius: 10px;"
                              " padding: 6px 10px;")
            self.filter_rows.addWidget(row)

    def _tracks_clicked(self) -> None:
        self._send({"tracks": [t for t, b in self.goes.items() if b.isChecked()]})

    @staticmethod
    def _balance_text(balance: float) -> str:
        if abs(balance) < 0.005:
            return "Centre"
        return f"{abs(balance) * 100:.0f}% {'left' if balance < 0 else 'right'}"

    def _volume_moved(self, value: int) -> None:
        self.volume_label.setText(f"{value:+d} dB" if value else "0 dB")
        self._send({"volume_db": float(value)})

    def _balance_moved(self, value: int) -> None:
        self.balance_label.setText(self._balance_text(value / 100))
        self._send({"balance": value / 100})

    def _send(self, changes: dict) -> None:
        if self._loading or not self.sound_id:
            return
        if "muted" in changes:
            self.mute.setText("Muted" if changes["muted"] else "Mute")
            self.mute.setStyleSheet(MUTED_CHIP_STYLE if changes["muted"] else CHIP_STYLE)
        if "monitor" in changes:
            for value, button in self.hear.items():
                button.setChecked(value == changes["monitor"] and type(value) is type(changes["monitor"]))
            for button in self.goes.values():
                button.setEnabled(changes["monitor"] != "only")
        self.on_change(self.sound_id, changes)
