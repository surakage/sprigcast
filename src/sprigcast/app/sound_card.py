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
volume, left/right, mono, delay, who hears it, filters, group, pin and ducking.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.live_stage import Switch, ThinMeter, heading

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
        self.sounds: dict[str, Sound] = {}
        self.editing = False
        self.heard: dict[str, float] = {}
        self.open_groups: set[str] = set()  # groups showing their sounds
        self.meters: dict[str, ThinMeter] = {}
        self.group_meters: dict[str, ThinMeter] = {}
        self.rows: dict[str, QWidget] = {}
        self.parts: dict[str, dict] = {}
        self.tuning: str | None = None
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
        col.addLayout(top)
        first = QHBoxLayout()
        first.setSpacing(10)
        label = QLabel("Mic first")
        label.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        hint = _muted_label("others dip when you talk")
        hint.setStyleSheet(hint.styleSheet() + " font-size: 12px;")
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
            self.pages.widget(i).setSizePolicy(QSizePolicy.Policy.Preferred, policy)
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
        shape = tuple((s.id, s.name, s.group, s.muted, s.pinned, s.volume_db) for s in items)
        if shape != self._shape:
            self._shape = shape
            self._build()
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
        self.meters, self.group_meters, self.rows, self.parts = {}, {}, {}, {}
        self._lit = {}
        if not self.sounds:
            self.body.addWidget(_muted_label("No mic or desktop audio yet"))
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
        sub = QLabel("")
        sub.setStyleSheet(f"color: {theme.MUTED}; background: transparent; font-size: 12px;")
        for label in (dot, name, sub):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        all_muted = all(s.muted for s in members)
        mute = _chip("Muted" if all_muted else "Mute", MUTED_CHIP_STYLE if all_muted else CHIP_STYLE)
        mute.setToolTip(f"Unmute every {group} sound" if all_muted else f"Mute every {group} sound")
        mute.clicked.connect(lambda _=False, ids=[s.id for s in members], on=not all_muted: self._mute_all(ids, on))
        head.addWidget(dot)
        head.addWidget(name)
        head.addWidget(sub, 1)
        head.addWidget(mute)
        col.addWidget(head_row)
        meter = ThinMeter()
        meter.colour = colour
        meter.muted = all_muted
        self.group_meters[group] = meter
        col.addWidget(meter)
        inner_box = QWidget()
        inner = QVBoxLayout(inner_box)
        inner.setContentsMargins(20, 6, 0, 2)
        inner.setSpacing(12)
        for sound in members:
            row = self._sound_row(sound, colour)
            self.rows[sound.id] = row
            inner.addWidget(row)
        col.addWidget(inner_box)
        self.parts[group] = {"name": name, "sub": sub, "inner": inner_box, "dot": dot,
                             "muted": [s.name for s in members if s.muted], "count": len(members)}
        return box

    def _sound_row(self, sound: Sound, colour: str) -> QWidget:
        row = ClickRow()
        row.setCursor(Qt.CursorShape.PointingHandCursor)
        row.setToolTip("Tune this sound: volume, left/right, mono, delay, filters…")
        row.clicked.connect(lambda sid=sound.id: self.open_tune(sid))
        col = QVBoxLayout(row)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        line = QHBoxLayout()
        line.setSpacing(8)
        name = QLabel(sound.name)
        name.setStyleSheet(f"color: {theme.MUTED if sound.muted else theme.TEXT}; background: transparent;")
        name.setWordWrap(True)
        note = "muted" if sound.muted else f"{sound.volume_db:+.0f} dB" if sound.volume_db else ""
        right = QLabel(note)
        right.setStyleSheet(f"color: {theme.LIVE_TEXT if sound.muted else theme.MUTED}; background: transparent;"
                            " font-size: 12px;")
        for label in (name, right):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        line.addWidget(name, 1)
        line.addWidget(right)
        col.addLayout(line)
        meter = ThinMeter()
        meter.setFixedHeight(3)
        meter.colour = colour
        meter.muted = sound.muted
        meter.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.meters[sound.id] = meter
        col.addWidget(meter)
        return row

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


class TunePage(QWidget):
    """Everything about one sound, inside the card. Changes apply at once."""

    def __init__(self, on_change: Callable[[str, dict], None], on_filters: Callable[[str], None],
                 on_back: Callable[[], None]):
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

        self.volume_label = _muted_label("")
        self.volume = QSlider(Qt.Orientation.Horizontal)
        self.volume.setRange(-60, 12)
        self.volume.valueChanged.connect(self._volume_moved)
        col.addLayout(self._labelled("Volume", self.volume_label))
        col.addWidget(self.volume)

        self.balance_label = _muted_label("")
        self.balance = QSlider(Qt.Orientation.Horizontal)
        self.balance.setRange(-100, 100)
        self.balance.setPageStep(10)
        self.balance.valueChanged.connect(self._balance_moved)
        col.addLayout(self._labelled("Left · Right", self.balance_label))
        col.addWidget(self.balance)

        pair = QHBoxLayout()
        pair.setSpacing(8)
        self.mono = _chip("Mono", checkable=True)
        self.mono.setToolTip("Both ears hear the same: for a mic that only comes out of one side")
        self.mono.toggled.connect(lambda on: self._send({"mono": on}))
        pair.addWidget(self.mono, 1)
        delay_box = QHBoxLayout()
        delay_box.setSpacing(6)
        delay_box.addWidget(_muted_label("Delay"))
        self.delay = QSpinBox()
        self.delay.setRange(0, 2000)
        self.delay.setSingleStep(10)
        self.delay.setSuffix(" ms")
        self.delay.setToolTip("Hold this sound back to line it up with a camera that runs late")
        self.delay.setStyleSheet(
            f"QSpinBox {{ background: {theme.RAISED}; border: none; border-radius: 11px; padding: 4px 8px;"
            f" color: {theme.TEXT}; }} QSpinBox::up-button, QSpinBox::down-button {{ width: 0px; border: none; }}")
        self.delay.valueChanged.connect(lambda v: self._send({"delay_ms": v}))
        delay_box.addWidget(self.delay, 1)
        pair.addLayout(delay_box, 1)
        col.addLayout(pair)

        col.addWidget(_muted_label("Who hears it"))
        hear = QHBoxLayout()
        hear.setSpacing(6)
        self.hear: dict[object, QPushButton] = {}
        for value, text, tip in ((False, "Stream", "On the stream and recordings, not your speakers"),
                                 (True, "Me + stream", "On the stream and your speakers"),
                                 ("only", "Just me", "Only your speakers: never on the stream")):
            b = _chip(text, checkable=True)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, v=value: self._send({"monitor": v}))
            self.hear[value] = b
            hear.addWidget(b, 1)
        col.addLayout(hear)

        col.addWidget(_muted_label("Goes to"))
        goes = QHBoxLayout()
        goes.setSpacing(6)
        self.goes: dict[str, QPushButton] = {}
        for track, text, tip in (("stream", "Stream", "Your live stream"),
                                 ("recording", "Recording", "The recording's main sound"),
                                 ("voice", "Voice", "The recording's voice-only track (Settings → Output → Recording)"),
                                 ("clips", "Clips", "Saved clips")):
            b = _chip(text, checkable=True)
            b.setToolTip(tip)
            b.clicked.connect(self._tracks_clicked)
            self.goes[track] = b
            goes.addWidget(b, 1)
        col.addLayout(goes)

        filters_top = QHBoxLayout()
        filters_top.addWidget(_muted_label("Filters"), 1)
        edit_filters = QPushButton("Edit filters")
        edit_filters.setStyleSheet(theme.LINK_STYLE)
        edit_filters.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_filters.clicked.connect(lambda: on_filters(self.sound_id))
        filters_top.addWidget(edit_filters)
        col.addLayout(filters_top)
        self.filters = QLabel("")
        self.filters.setWordWrap(True)
        self.filters.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        col.addWidget(self.filters)

        group_row = QHBoxLayout()
        group_row.addWidget(_muted_label("Group"), 1)
        self.group = QComboBox()
        self.group.addItems(list(GROUPS))
        self.group.currentTextChanged.connect(lambda g: self._send({"group": g}))
        group_row.addWidget(self.group)
        col.addLayout(group_row)

        self.pin = Switch()
        self.pin.toggled.connect(lambda on: self._send({"pinned": on}))
        col.addLayout(self._switch_row("Always show", self.pin))
        self.duck = Switch()
        self.duck.toggled.connect(lambda on: self._send({"duck_voice": on}))
        col.addLayout(self._switch_row("Lower other sounds when this talks", self.duck))

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
        self.filters.setText(", ".join(sound.filters) if sound.filters else "None yet")
        self.group.setCurrentText(sound.group)
        self.pin.setChecked(sound.pinned)
        self.duck.setChecked(sound.voice)
        self._loading = False

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
