"""Live's Sound card: sounds in groups, showing only what's playing, with a Tune page per sound.

    Sound                         Edit
    ● Voice            1     [Mute]
    ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬
      Mic                    −6 dB
      ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬
    ● SFX              6     [Mute]
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

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
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

from sprigcast.app import icons, theme
from sprigcast.app.card import field_label
from sprigcast.app.live_stage import Elided, Switch, ThinMeter, heading
from sprigcast.app.segmented import Segmented

# Sound types (the Sound card's groups and Build's Type filter). You can add, rename, recolor and remove them;
# they're kept in the garden (AudioSettings.sound_types). These lists change in place, so every importer sees it.
TYPE_COLORS = theme.type_colors()  # by key; the color-blind preset has its own set (see theme.TYPE_COLOR_SETS)
GROUPS: list[str] = ["Browser", "Game", "Music", "Other", "SFX", "TTS", "Voice"]  # always A to Z
GROUP_COLOURS: dict[str, str] = {name: TYPE_COLORS[key] for name, key in
                                 zip(GROUPS, ("teal", "blue", "violet", "gray", "yellow", "orange", "green"),
                                     strict=True)}


def use_types(types: list[dict] | None) -> bool:
    """Take the garden's sound types ({"name", "color"} each, in order). True when they changed."""
    names, colours = [], {}
    for t in types or ():
        name = str(t.get("name", "")).strip()
        if name and name not in colours:
            names.append(name)
            colours[name] = TYPE_COLORS.get(str(t.get("color", "")), TYPE_COLORS["gray"])
    if not names:
        return False  # a garden with none keeps what's shown
    names.sort(key=str.casefold)  # listed A to Z everywhere
    if names == GROUPS and colours == GROUP_COLOURS:
        return False
    GROUPS[:] = names
    GROUP_COLOURS.clear()
    GROUP_COLOURS.update(colours)
    return True


def type_of(group: str | None) -> str:
    """A sound's type; one whose type was removed (or never existed) shows under Other (or the last one)."""
    if group in GROUP_COLOURS:
        return group
    return "Other" if "Other" in GROUP_COLOURS else GROUPS[-1]
HEARD_DB = -42.0  # a peak above this counts as playing (hiss and room noise stay below it)
LINGER_S = 4.0  # a sound stays listed this long after it goes quiet
CHIP_STYLE = theme.button_style(11, "3px 11px", extra="min-height: 16px; font-size: 11px;")
MUTED_CHIP_STYLE = CHIP_STYLE + f"QPushButton {{ background: {theme.LIVE}; color: #ffffff; border-color: {theme.LIVE_EDGE}; }}"
_MUSIC = re.compile(r"music|spotify|song|pretzel|playlist|radio", re.IGNORECASE)
_TTS = re.compile(r"tts|text to speech|speech|speaker\.?bot", re.IGNORECASE)
_SFX = re.compile(r"sfx|alert|ad break|chat|tikfinity|streamer\.?bot|sound ?alert|donat|sub|follow|bits", re.IGNORECASE)
_GAME = re.compile(r"game|desktop|discord|steam", re.IGNORECASE)


def auto_group(name: str, kind: str) -> str:
    """Where a sound goes until you move it: mics are Voice; names hint at the rest."""
    if kind == "audio_input":
        return "Voice"
    if kind == "app_audio":  # named for its type: Music, Voice chat, SFX...
        if re.search(r"voice chat", name, re.IGNORECASE):
            return "Voice"
    if _TTS.search(name):
        return "TTS"
    if _MUSIC.search(name):
        return "Music"
    if kind == "browser" or re.search(r"browser", name, re.IGNORECASE):
        return "Browser"
    if _SFX.search(name):
        return "SFX"
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
    apps: str | None = None  # where it comes from: its apps, device or file; None when there's nothing to change
    desktop: bool = False  # a page whose sound plays on desktop audio: SprigCast can't mix or measure it


def note_text(sound: Sound) -> str:
    """A sound row's note: muted, on desktop, or its volume when not 0 dB."""
    if sound.muted:
        return "muted"
    if sound.desktop:
        return "on desktop"
    return f"{sound.volume_db:+.0f} dB" if sound.volume_db else ""


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
            if self.level >= theme.CLIP_LEVEL:
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


def _value_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.LABEL}; background: transparent; font-weight: 600;")
    return label


GOES_STYLE = (f"QPushButton#goesPill {{ background: {theme.RAISED}; color: {theme.LABEL}; border: none;"
              " border-radius: 11px; min-height: 22px; max-height: 22px; padding: 0px 10px; font-weight: 600; }"
              f"QPushButton#goesPill:hover {{ background: {theme.RAISED_HOVER}; }}"
              f"QPushButton#goesPill:checked {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}"
              f"QPushButton#goesPill:disabled {{ color: {theme.EDGE_STRONG}; }}")  # as Settings → Audio's pills


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
        col.setSpacing(10)
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
        # 8, not 16: four types fit the card under the canvas without the last one cut in half (PC 2026-11-20).
        self.body.setSpacing(8)
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
        shape = (tuple((s.id, s.name, s.group, s.pinned) for s in items), tuple(GROUP_COLOURS.items()))
        if shape != self._shape:
            self._shape = shape
            self._build()
        self._show_muted()
        for sid, sound in self.sounds.items():  # volume changes in place: a drag isn't cut off by a rebuild
            note = self.notes.get(sid)
            text = note_text(sound)
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
            # The bars follow the loudest moment since the last update, not one short slice of it: speech and
            # a short alert sound move them right away (2026-11-21: the mic and alerts read "quiet").
            peak = levels[source_id][0]
            meter.set_db(peak)
            if peak > HEARD_DB:
                self.heard[source_id] = now
            sound = self.sounds.get(source_id)
            if sound is not None and not sound.muted and peak > loudest.get(sound.group, (-61.0, ""))[0]:
                loudest[sound.group] = (peak, sound.name)
        for group, meter in self.group_meters.items():
            meter.set_db(loudest.get(group, (-60.0, ""))[0])
        if self.tuning in levels:
            self.tune.meter.set_db(levels[self.tuning][0])
        self._apply_visibility(now, {g: name for g, (_, name) in loudest.items()})

    # Building -----------------------------------------------------------------------------

    def _build(self) -> None:
        """Lay out the groups and rows. Boxes and rows already made are used again with the new sounds: making
        every one afresh after a garden switch held the window 332 ms while live (PC test, 2026-11-20)."""
        boxes: dict[str, QWidget] = getattr(self, "_boxes", {})
        self._boxes = boxes
        spare: list[QWidget] = getattr(self, "_spare_rows", [])
        self._spare_rows = spare
        while self.body.count():  # take everything out; what's still wanted goes back in below
            widget = self.body.takeAt(0).widget()
            if widget is not None:
                widget.hide()
        self.meters, self.group_meters, self.rows, self.notes = {}, {}, {}, {}
        self.names: dict[str, QLabel] = {}
        self._muted_shown: dict[str, bool] = {}  # what each row and group head shows now
        self._lit = {}
        old_parts, self.parts = self.parts, {}
        if not self.sounds:
            if getattr(self, "_empty", None) is None:
                from sprigcast.app.empty import EmptyState

                self._empty = EmptyState("No mic or desktop audio yet. Add them in Build with + Add.", scale=0.7)
            self.body.addWidget(self._empty)
            self._empty.show()
            for group, part in old_parts.items():  # unused rows wait in the spare pile
                self._park_rows(part)
            return
        for group in GROUPS:
            members = [s for s in self.sounds.values() if s.group == group]
            part = old_parts.pop(group, None)
            if not members:
                if part is not None:
                    self._park_rows(part)
                continue
            if part is None:
                box = boxes.get((group, GROUP_COLOURS[group]))  # a recolored type gets a new box
                if box is None:
                    box = boxes[(group, GROUP_COLOURS[group])] = self._group_box(group)
                part = box.part
            self._fill_group(group, part, members)
            self.parts[group] = part
            self.group_meters[group] = part["meter"]
            self.body.addWidget(part["box"])
            part["box"].show()  # a layout shows new children later; show now so visibility below sticks
        for part in old_parts.values():
            self._park_rows(part)

    def _park_rows(self, part: dict) -> None:
        for row in part["row_list"]:
            part["inner_layout"].removeWidget(row)
            row.hide()
            row.setParent(None)
            self._spare_rows.append(row)
        part["row_list"] = []

    def _fill_group(self, group: str, part: dict, members: list[Sound]) -> None:
        colour = GROUP_COLOURS[group]
        rows = part["row_list"]
        while len(rows) > len(members):
            row = rows.pop()
            part["inner_layout"].removeWidget(row)
            row.hide()
            row.setParent(None)
            self._spare_rows.append(row)
        while len(rows) < len(members):
            row = self._spare_rows.pop() if self._spare_rows else self._sound_row()
            part["inner_layout"].addWidget(row)
            rows.append(row)
        for row, sound in zip(rows, members, strict=True):
            self._show_row(row, sound, colour)
        part["ids"] = [s.id for s in members]
        part["muted"] = []
        part["count"] = len(members)

    def _group_box(self, group: str) -> QWidget:
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
        part: dict = {}

        def mute_all(_=False) -> None:
            ids = part["ids"]
            self._mute_all(ids, not all(self.sounds[i].muted for i in ids if i in self.sounds))
        mute.clicked.connect(mute_all)
        head.addWidget(dot)
        head.addWidget(name)
        head.addWidget(sub, 1)
        head.addWidget(mute)
        col.addWidget(head_row)
        meter = ThinMeter()
        meter.colour = colour
        col.addWidget(meter)
        inner_box = QWidget()
        inner = QVBoxLayout(inner_box)
        inner.setContentsMargins(20, 4, 0, 2)
        inner.setSpacing(6)
        col.addWidget(inner_box)
        part.update({"box": box, "name": name, "sub": sub, "inner": inner_box, "inner_layout": inner, "dot": dot,
                     "mute": mute, "meter": meter, "row_list": [], "ids": [], "muted": [], "count": 0})
        box.part = part
        return box

    def _sound_row(self) -> QWidget:
        """An empty row; _show_row puts a sound in it (and another one later: rows are used again)."""
        row = ClickRow()
        row.sid = ""
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setToolTip("Tune this sound: volume, left/right, mono, delay, filters…")
        row.clicked.connect(lambda: self.open_tune(row.sid))
        col = QVBoxLayout(row)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)  # the volume bar has its own breathing room (it's 14 px tall for the knob)
        line = QHBoxLayout()
        line.setSpacing(8)
        row.name = QLabel()  # its colour, and the note's, come from _show_muted
        row.name.setWordWrap(True)
        row.note = QLabel()
        for label in (row.name, row.note):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        line.addWidget(row.name, 1)
        line.addWidget(row.note)
        col.addLayout(line)
        row.meter = VolumeMeter(0.0, lambda db: self._drag_volume(row.sid, db))
        col.addWidget(row.meter)
        return row

    def _show_row(self, row: QWidget, sound: Sound, colour: str) -> None:
        row.sid = sound.id
        if row.name.text() != sound.name:
            row.name.setText(sound.name)
        note = note_text(sound)
        if row.note.text() != note:
            row.note.setText(note)
        meter = row.meter
        meter.db, meter.colour = sound.volume_db, colour
        meter.set_db(-60.0)  # the last sound's level isn't this one's
        meter.update()
        self.rows[sound.id], self.names[sound.id], self.notes[sound.id] = row, row.name, row.note
        self.meters[sound.id] = meter

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
        if note is not None and sound is not None:
            note.setText(note_text(sound))
        self.on_change(sound_id, {"volume_db": db})

    # Calm by default ----------------------------------------------------------------------

    def is_open(self, group: str) -> bool:
        return self.editing or group in self.open_groups

    def _apply_visibility(self, now: float | None = None, playing: dict[str, str] | None = None) -> None:
        """A group lights up (its colour) while one of its sounds plays, and names that sound;
        a quiet group is dimmed. Its sounds show only when you open it (or press Edit)."""
        now = time.monotonic() if now is None else now
        playing = playing or {}
        # Called with every meter update (20 a second): which rows show is worked out in Python and Qt is asked
        # only when it changes. Asking each row's visibility every time held the window up while live (100 ms
        # under load on the PC, 2026-11-18).
        shows = getattr(self, "_row_shows", None)
        if shows is None or shows[0] is not self.rows:
            shows = self._row_shows = (self.rows, {})
        for group, part in self.parts.items():
            opened = self.is_open(group)
            wanted = {sid: opened or s.pinned  # "Always show" keeps a sound listed when its group is shut
                      for sid, s in self.sounds.items() if s.group == group and sid in self.rows}
            if shows[1].get(group) != wanted:
                shows[1][group] = wanted
                for sid, show in wanted.items():
                    if self.rows[sid].isVisibleTo(self) != show:
                        self.rows[sid].setVisible(show)
                any_row = any(wanted.values())
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
                desk = sum(1 for sid in part["ids"] if sid in self.sounds and self.sounds[sid].desktop
                           and not self.sounds[sid].muted)
                quiet = part["count"] - desk
                text = f"{quiet} quiet" if quiet > 1 else "quiet" if quiet else ""
                if desk:  # its sound isn't in SprigCast's mix, so its bar can't move
                    text = ", ".join(t for t in (text, f"{desk} on desktop") if t)
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
        self.setMenu(QMenu(self))
        self.set_choices(self.choices)

    def set_choices(self, choices) -> None:
        """New choices (sound types you added or renamed); the shown one stays when it's still there."""
        self.choices = list(choices)
        menu = self.menu()
        menu.clear()
        for choice in self.choices:
            menu.addAction(choice, lambda c=choice: self.setCurrentText(c))
        if self._text not in self.choices and self.choices:
            self._text = self.choices[0]
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
        top.setSpacing(10)
        back = QPushButton()  # a drawn chevron in a round button: the "‹" letter sat low and cramped
        back.setIcon(icons.icon("chevron-left", theme.TEXT))
        back.setIconSize(QSize(18, 18))
        back.setFixedSize(30, 30)
        # Its size in the style sheet too: the sheet's own min-height (18 px) won over setFixedSize and made it
        # 30 x 20, too short for its radius, so Qt drew it square (user 2026-11-20).
        back.setStyleSheet(theme.button_style(14, "0px", extra="min-width: 28px; max-width: 28px;"
                                                             " min-height: 28px; max-height: 28px;"))
        back.setCursor(Qt.CursorShape.PointingHandCursor)
        back.setToolTip("Back to all sounds")
        back.clicked.connect(on_back)
        top.addWidget(back, 0, Qt.AlignmentFlag.AlignVCenter)
        self.title = heading("")
        self.title.setWordWrap(True)
        top.addWidget(self.title, 1, Qt.AlignmentFlag.AlignVCenter)
        self.mute = _chip("Mute", checkable=True)
        self.mute.toggled.connect(lambda on: self._send({"muted": on}))
        top.addWidget(self.mute, 0, Qt.AlignmentFlag.AlignVCenter)
        col.addLayout(top)
        self.meter = ThinMeter()
        col.addWidget(self.meter)

        # Titled section cards with a leaf, as in Settings (user 2026-11-20: the soft untitled boxes looked dated).
        # Sound per bloom (Build only): which blooms it plays in, and a way to pick them.
        self.blooms_card, box = self._section(col, "Plays in")
        self.blooms = _muted_label("")
        self.blooms.setWordWrap(True)
        box.addLayout(self._with_button(self.blooms, "Change", "Pick the blooms this sound plays in",
                                        lambda: on_blooms and on_blooms(self.sound_id)))
        self.blooms_card.setVisible(on_blooms is not None)

        # Where the sound comes from (its apps, mic, output or file), and a way to change it.
        self.apps_card, box = self._section(col, "Source")
        self.apps = _muted_label("")
        self.apps.setWordWrap(True)
        change = self._with_button(self.apps, "Change", "Change where this sound comes from",
                                   lambda: on_apps and on_apps(self.sound_id))
        change.itemAt(1).widget().setVisible(on_apps is not None)
        box.addLayout(change)
        self.apps_card.hide()

        _level, box = self._section(col, "Volume")
        self.volume_label = _value_label("")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(-60, 12)
        self.volume.setAccessibleName("Volume")
        self.volume.valueChanged.connect(self._volume_moved)
        box.addLayout(self._labelled("Loudness", self.volume_label))
        box.addWidget(self.volume)
        box.addSpacing(2)
        self.balance_label = _value_label("")
        self.balance = QSlider(Qt.Orientation.Horizontal)
        self.balance.setRange(-100, 100)
        self.balance.setPageStep(10)
        self.balance.setAccessibleName("Left and right")
        self.balance.valueChanged.connect(self._balance_moved)
        box.addLayout(self._labelled("Left · Right", self.balance_label))
        box.addWidget(self.balance)
        box.addSpacing(2)
        pair = QHBoxLayout()
        pair.setSpacing(10)
        self.mono = QCheckBox("Mono")
        self.mono.setToolTip("Both ears hear the same: for a mic that only comes out of one side")
        self.mono.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mono.toggled.connect(lambda on: self._send({"mono": on}))
        pair.addWidget(self.mono)
        pair.addStretch(1)
        pair.addWidget(field_label("Delay"))
        self.delay = QSpinBox()
        self.delay.setObjectName("tuneDelay")
        self.delay.setRange(0, 2000)
        self.delay.setSingleStep(10)
        self.delay.setSuffix(" ms")
        self.delay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.delay.setAccessibleName("Delay")
        self.delay.setToolTip("Hold this sound back to line it up with a camera that runs late")
        self.delay.setStyleSheet(
            f"QSpinBox#tuneDelay {{ background: {theme.FIELD}; border: 1px solid {theme.FIELD_EDGE};"
            f" border-radius: 12px; padding: 4px 10px; color: {theme.TEXT}; }}"
            f"QSpinBox#tuneDelay:focus {{ border-color: {theme.ACCENT}; }}"
            "QSpinBox#tuneDelay::up-button, QSpinBox#tuneDelay::down-button { subcontrol-origin: margin;"
            " width: 0px; height: 0px; border: none; }")
        self.delay.valueChanged.connect(lambda v: self._send({"delay_ms": v}))
        self.delay.setFixedWidth(84)
        pair.addWidget(self.delay)
        box.addLayout(pair)

        _route, box = self._section(col, "Who hears it")
        self.hear_pick = Segmented([(False, "Stream"), (True, "Stream + me"), ("only", "Just me")])
        self.hear_pick.setAccessibleName("Who hears it")
        self.hear_pick.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.hear: dict[object, QPushButton] = self.hear_pick.buttons
        for value, tip in ((False, "On the stream and recordings, not your headphones"),
                           (True, "On the stream and in your headphones"),
                           ("only", "Only your headphones: never on the stream")):
            button = self.hear[value]
            button.setToolTip(tip)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.hear_pick.picked.connect(lambda v: self._send({"monitor": v}))
        box.addWidget(self.hear_pick)
        box.addSpacing(4)
        box.addWidget(field_label("Goes to"))
        goes = QHBoxLayout()
        goes.setSpacing(6)
        self.goes: dict[str, QPushButton] = {}
        for track, text, tip in (("stream", "Stream", "Your live stream"),
                                 ("recording", "Recording", "The recording's main sound"),
                                 ("voice", "Voice", "The recording's voice-only track (Settings → Recording)"),
                                 ("clips", "Clips", "Saved clips")):
            b = QPushButton(text)
            b.setObjectName("goesPill")
            b.setCheckable(True)
            b.setAutoDefault(False)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            b.setStyleSheet(GOES_STYLE)
            b.clicked.connect(self._tracks_clicked)
            self.goes[track] = b
            goes.addWidget(b)
        goes.addStretch(1)
        box.addLayout(goes)

        _filters, box = self._section(col, "Filters")
        # One filter per row, in the order they run, so five to eight of them still read at a glance.
        self.filters = _muted_label("None yet")
        self.filter_rows = QVBoxLayout()
        self.filter_rows.setSpacing(4)
        edit_row = self._with_button(self.filters, "Edit", "Noise gate, compressor, EQ...",
                                     lambda: on_filters(self.sound_id))
        box.addLayout(self.filter_rows)
        box.addLayout(edit_row)

        _more, box = self._section(col, "More")
        group_row = QHBoxLayout()
        group_row.addWidget(field_label("Type"), 1)
        self.group = PillPick(GROUPS)
        self.group.setAccessibleName("Type")
        self.group.currentTextChanged.connect(lambda g: self._send({"group": g}))
        group_row.addWidget(self.group)
        box.addLayout(group_row)
        self.pin = Switch()
        self.pin.setAccessibleName("Always show")
        self.pin.toggled.connect(lambda on: self._send({"pinned": on}))
        box.addLayout(self._switch_row("Always show", self.pin))
        self.duck = Switch()
        self.duck.setAccessibleName("Lowers others when it talks")
        self.duck.toggled.connect(lambda on: self._send({"duck_voice": on}))
        box.addLayout(self._switch_row("Lowers others when it talks", self.duck))

    def show_plays_in(self, text: str) -> None:
        """Build's "Plays in" card: "Every bloom" or the bloom names."""
        self.blooms.setText(text)

    @staticmethod
    def _section(col: QVBoxLayout, title: str) -> tuple[QGroupBox, QVBoxLayout]:
        """A titled section card (the app's QGroupBox look: leaf, title, edge), padded inside."""
        frame = QGroupBox(title)
        box = QVBoxLayout(frame)
        box.setContentsMargins(4, 8, 4, 4)
        box.setSpacing(8)
        col.addWidget(frame)
        return frame, box

    @staticmethod
    def _with_button(label: QLabel, text: str, tip: str, clicked: Callable[[], None]) -> QHBoxLayout:
        """A line of text with a small pill button at its right (Change, Edit)."""
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(label, 1)
        button = QPushButton(text)
        button.setStyleSheet(theme.button_style(13, "3px 14px", extra="min-height: 22px;"))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAutoDefault(False)
        button.setToolTip(tip)
        button.clicked.connect(clicked)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)  # stays at the right when the text hides
        row.addWidget(button, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        return row

    def _labelled(self, text: str, value: QLabel) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(field_label(text), 1)
        row.addWidget(value)
        return row

    def _switch_row(self, text: str, switch: Switch) -> QHBoxLayout:
        row = QHBoxLayout()
        label = QLabel(text)
        label.setStyleSheet(f"color: {theme.LABEL}; background: transparent;")
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
        self.hear_pick.set_value("only" if sound.monitor == "only" else bool(sound.monitor))
        for value, button in self.hear.items():
            if value is not False:
                button.setEnabled(sound.can_monitor)
        for track, button in self.goes.items():
            button.setChecked(track in sound.tracks)
            button.setEnabled(sound.monitor != "only")  # "just me" goes nowhere but your speakers
        self._show_filters(sound.filters)
        if self.group.choices != GROUPS:
            self.group.set_choices(GROUPS)
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
            row.setStyleSheet(f"background: {theme.FIELD}; color: {theme.TEXT}; border: 1px solid {theme.SECTION_EDGE};"
                              " border-radius: 10px; padding: 6px 10px;")
            self.filter_rows.addWidget(row)

    def _tracks_clicked(self) -> None:
        self._send({"tracks": [t for t, b in self.goes.items() if b.isChecked()]})

    @staticmethod
    def _balance_text(balance: float) -> str:
        if abs(balance) < 0.005:
            return "Center"
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
