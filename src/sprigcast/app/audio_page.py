"""Settings → Audio, in tabs: Devices, Sounds (which go on each track) and Tracks.

- Global devices: the Desktop audio and Mic every setup starts with. Switched off, they are not opened
  at all and leave the Sound list, so only the mics and desktop sounds you add yourself play.
- Tracks: a recording can keep up to five sound tracks. 1 is everything, 2 is VOD-safe (everything
  but the sounds you keep out of it, music by default), 3 is your voice, 4 and 5 are yours to fill.
- Sounds: one row per sound: volume, balance, mono, delay, who hears it and its tracks (the numbered
  pills). The Sound card's ⚙ opens it. Ducking's two everyday picks sit under the table.

Sound bitrate sits with the rest of each encoder (Stream → Quality and Recording, expert rows).
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.live_stage import Switch
from sprigcast.app.names import sound_name_is_ours
from sprigcast.app.segmented import Segmented
from sprigcast.audio.mixer import is_monitor_only, is_music, planned_tracks, source_tracks
from sprigcast.core.scene import AUDIO_DEVICE_KINDS, AUDIO_TRACKS, TRACK_NAMES, SceneCollection, SourceConfig
from sprigcast.sources.audio import device_label as nice_device_name
from sprigcast.sources.audio import is_mixer_app_device

log = logging.getLogger(__name__)

TRACK_TIPS = (
    "Everything on the stream. Players play this one.",
    "Everything except sounds kept out of it (music, by default): upload this one to avoid copyright strikes.",
    "Your voice on its own, so you can fix it apart when editing.",
    "Only the sounds you tick for it.",
    "Only the sounds you tick for it.",
)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.MUTED}")
    return label


def has_sound(source: SourceConfig) -> bool:
    """Sounds that reach the mixer: mics, desktop sounds, videos and sound files, browser pages sending sound."""
    if source.kind in AUDIO_DEVICE_KINDS or source.kind in ("app_audio", "media"):
        return True
    if source.kind == "browser":
        from sprigcast.sources.browser import sound_to_mixer

        return sound_to_mixer(source.settings)
    if source.kind == "android_screen":
        from sprigcast.sources.android_screen import sound_on

        return sound_on(source.settings)
    return False


_CENTRE = Qt.AlignmentFlag.AlignCenter  # looked up once: each look-up of a Qt enum is Python work


def on_track(source: SourceConfig, number: int) -> bool:
    return AUDIO_TRACKS[number - 1] in source_tracks(source)


def put_on_track(source: SourceConfig, number: int, on: bool) -> None:
    """Put a sound on track 1-5 or take it off, keeping its other tracks as they are."""
    if on_track(source, number) == on:
        return
    track = AUDIO_TRACKS[number - 1]
    settings = dict(source.settings)
    if track in ("recording", "voice"):  # these live in the "tracks" list the Sound card also sets
        current = [t for t in source_tracks(source) if t in ("stream", "recording", "voice", "clips")]
        settings["tracks"] = [t for t in current if t != track] + ([track] if on else [])
    else:  # "vod", "track4", "track5" have their own switches
        settings[track] = on
    source.settings = settings


AUDIO_KBPS = (96, 128, 160, 192, 256, 320)


PUSH_RELEASE_MS = (0, 100, 250, 500)
DUCK_PICKS = ((-6.0, "-6 dB"), (-12.0, "-12 dB"), (-18.0, "-18 dB"))
DEVICES_RETRY_MS = 250  # how often a Devices tab waiting for the device scan looks again
METER_MS = 80  # how often the Devices meters move, and only while that tab is on screen
STALE_S = 0.5  # levels older than this are silence (the mixer stopped)
CHIPS_SHOWN = 3  # sounds named on a track's row before "+2 more" (4 made Tracks wider than Settings)
CHIP_TEXT_PX = 104  # a long sound name is cut short on its chip; the tip has all of it


def page_style() -> str:
    """One style sheet for the whole page (a sheet per pill or chip was Qt work for every row)."""
    t = theme
    return (
        f"QFrame#audioTable {{ background: transparent; border: 1px solid {t.SECTION_EDGE}; border-radius: 14px; }}"
        f"QFrame#audioLine {{ background: {t.SECTION_EDGE}; border: none; max-height: 1px; min-height: 1px; }}"
        f"QLabel#audioHead {{ color: {t.MUTED}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}"
        f"QLabel#audioName {{ color: {t.TEXT_BRIGHT}; font-weight: 600; }}"
        f"QLabel#audioDb {{ color: {t.LABEL}; }}"
        f"QLabel#audioChip {{ background: {t.SELECT_BG}; color: {t.SELECT_TEXT}; border-radius: 11px;"
        f" padding: 3px 10px; font-weight: 600; }}"
        f"QPushButton#trackPill {{ background: {t.RAISED}; color: {t.LABEL}; border: none; border-radius: 11px;"
        f" min-width: 22px; max-width: 22px; min-height: 22px; max-height: 22px; padding: 0; font-weight: 600; }}"
        f"QPushButton#trackPill:checked {{ background: {t.SELECT_BG}; color: {t.SELECT_TEXT}; }}"
        f"QPushButton#trackPill:disabled {{ color: {t.EDGE_STRONG}; }}"
        f"QPushButton#mixPill {{ background: {t.RAISED}; color: {t.LABEL}; border: none; border-radius: 11px;"
        f" min-height: 22px; max-height: 22px; padding: 0px 9px; font-weight: 600; }}"
        f"QPushButton#mixPill:checked {{ background: {t.SELECT_BG}; color: {t.SELECT_TEXT}; }}"
        f"QPushButton#mixPill:disabled {{ color: {t.EDGE_STRONG}; }}"
        + _pick_style()
        + f"QSlider#balance::sub-page:horizontal {{ background: {t.EDGE_STRONG}; }}"
        f"QSpinBox#audioDelay {{ background: transparent; border: 1px solid transparent; padding: 4px 0px;"
        f" min-height: 18px; }}"
        # No arrows (type, scroll or the arrow keys). NoButtons can't be used: with the app's style sheet it
        # shrinks the number to 1 px wide (see theme).
        f"QSpinBox#audioDelay::up-button, QSpinBox#audioDelay::down-button {{ subcontrol-origin: margin;"
        f" width: 0px; height: 0px; border: none; }}"
        f"QSpinBox#audioDelay:hover, QSpinBox#audioDelay:focus {{ background: {t.FIELD};"
        f" border: 1px solid {t.FIELD_EDGE}; }}"
    )


def _pick_style(name: str = "audioPick") -> str:
    """Drop-downs on this page (Hear it, Music drops when) as the same rounded pill as the Type button beside
    Find, with the soft chevron: the plain boxes looked like an older app (user 2026-11-20). name: the combo
    boxes' object name (the Filters window uses the same look)."""
    t = theme
    try:
        arrow = (f"QComboBox#{name}::down-arrow {{ image: url({t.nature_pictures()['chevron']});"
                 " width: 10px; height: 10px; }")
    except OSError:
        arrow = ""
    return (
        f"QComboBox#{name} {{ background: {t.BUTTON}; color: {t.TEXT}; border: 1px solid {t.BUTTON_EDGE};"
        f" border-top-color: {t.BUTTON_TOP}; border-radius: 14px; padding: 3px 12px; min-height: 22px;"
        " font-weight: 600; }"
        f"QComboBox#{name}:hover, QComboBox#{name}:focus {{ background: {t.BUTTON_HOVER};"
        f" border-color: {t.ACCENT}; }}"
        f"QComboBox#{name}:disabled {{ color: {t.MUTED}; background: transparent; border-color: {t.EDGE}; }}"
        f"QComboBox#{name}::drop-down {{ border: none; background: transparent; width: 24px; }}" + arrow
    )


def _head(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("audioHead")
    return label


def _line() -> QFrame:
    line = QFrame()
    line.setObjectName("audioLine")
    return line


NO_DEVICE = "__none__"  # a device list's "None": remove that sound

class LevelDots(QWidget):
    """A row of small bars that fill with a device's level: green, yellow when loud, pink close to clipping."""

    DOTS = 20

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.level = 0.0  # 0..1 of -60..0 dBFS
        self.setFixedSize(self.DOTS * 6, 12)

    def set_db(self, rms: float) -> None:
        target = (min(0.0, max(-60.0, rms)) + 60.0) / 60.0
        level = target if target > self.level else max(target, self.level - 0.06)  # falls back, not drops
        if round(level * self.DOTS) != round(self.level * self.DOTS):
            self.update()
        self.level = level

    def lit(self) -> int:
        return round(self.level * self.DOTS)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        lit = self.lit() if self.isEnabled() else 0
        for i in range(self.DOTS):
            where = (i + 1) / self.DOTS
            colour = theme.TRACK if i >= lit else (
                theme.ACCENT if where <= 2 / 3 else theme.WARN if where <= 0.9 else theme.LIVE)
            p.setBrush(QColor(colour))
            p.drawRoundedRect(QRectF(i * 6, 0, 4, 12), 2, 2)
        p.end()


class DbSlider(QWidget):
    """A sound's volume: a slider (-60 to +12 dB, half a dB a step) with what it's set to beside it."""

    def __init__(self, db: float = 0.0, parent: QWidget | None = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(-120, 24)  # half dBs
        self.slider.setPageStep(6)
        self.slider.setFixedWidth(100)
        self.text = QLabel()
        self.text.setObjectName("audioDb")
        self.text.setFixedWidth(56)
        self.text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.slider)
        row.addWidget(self.text)
        self._db = None
        self.setValue(db)
        self.slider.valueChanged.connect(self._moved)

    def value(self) -> float:
        return self._db

    def setValue(self, db: float) -> None:  # noqa: N802 (as a spin box's)
        """Any tenth of a dB is kept as given (set by hand on the Sound card); the slider sits at the nearest half."""
        self._db = round(min(12.0, max(-60.0, float(db))), 1)
        self.slider.blockSignals(True)
        self.slider.setValue(round(self._db * 2))
        self.slider.blockSignals(False)
        self._show()

    def _moved(self, halves: int) -> None:
        self._db = halves / 2
        self._show()

    def _show(self) -> None:
        self.text.setText(f"{self._db:+.1f} dB")


class AudioPage(QWidget):
    TABS = ("Devices", "Sounds", "Tracks")

    def __init__(self, collection: SceneCollection, audio_devices, recording: bool = False,
                 streaming: bool = False, first: str | None = None, levels=None, parent: QWidget | None = None):
        """audio_devices: a dict, or something that gives one when called (Settings: waits for the scan).
        first: make only this tab now; the others are listed in `later` (Settings makes them a tick apart, see
        SettingsWindow._slice) and made by finish() if still waiting. None: every tab now.
        levels: gives (when heard, {sound id: (peak, rms dB)}) for the Devices meters; None: no meters."""
        super().__init__(parent)
        self.setStyleSheet(page_style())
        self._levels = levels
        self.meters: dict[str, LevelDots] = {}
        self._finding: dict[str, QWidget] = {}  # tabs waiting for the sound devices to be found: their "Finding" line
        self._parts_waiting: list = []  # (tab's layout, place, makes a card): cards still to make (build_tab)
        self._meter_timer = QTimer(self)
        self._meter_timer.setInterval(METER_MS)
        self._meter_timer.timeout.connect(self._move_meters)
        self.collection = collection
        self.streaming, self.recording = streaming, recording
        self._audio_devices = audio_devices
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.device_combos: dict[str, QComboBox] = {}
        self.on_ducking = None  # set by the main window: opens the Ducking window
        self.tabs = QTabWidget()
        self._holders: dict[str, QVBoxLayout] = {}
        for name in self.TABS:
            tab = QWidget()
            box = QVBoxLayout(tab)
            box.setContentsMargins(0, 4, 0, 0)
            self._holders[name] = box
            self.tabs.addTab(tab, name)
        layout.addWidget(self.tabs)
        names = list(self.TABS) if first not in self.TABS else [first]
        self.later = [n for n in self.TABS if n not in names]
        for name in names:
            self.build_tab(name)
        if first in self.TABS:
            self.show_tab(first)
        self.tabs.currentChanged.connect(self._tab_shown)
        self._fit_tabs()

    def _fit_tabs(self) -> None:
        """The page is as wide as the tab showing, not the widest tab: Tracks' 922 px made Sounds wider than
        Settings, cutting off its right side (PC 2026-11-20, Settings at 1076 px)."""
        now = self.tabs.currentIndex()
        if now == getattr(self, "_fitted", None):
            return  # already fitted to this tab: every showing of the page (showEvent) asked again, and each
            # asking laid out the whole page anew (377 ms in one on the PC as Settings opened, 2026-11-20)
        self._fitted = now
        for i in range(self.tabs.count()):
            policy = QSizePolicy.Policy.Preferred if i == now else QSizePolicy.Policy.Ignored
            if self.tabs.widget(i).sizePolicy().horizontalPolicy() != policy:
                self.tabs.widget(i).setSizePolicy(policy, policy)
        self.tabs.updateGeometry()

    def build_tab(self, name: str, later=None, wait: bool = False) -> None:
        """Make one tab's contents (once). later(call): the tab's first card is made now and each further card
        handed to it, to be made a tick apart (Settings' _slice): Devices' three cards in one go held the window
        63 ms on the PC while live (184 ms with the old engine, 2026-11-19). None: all of it now."""
        ready = getattr(self._audio_devices, "ready", None)
        if name == "Devices" and not wait and ready is not None and not ready() and name in self._holders:
            # The sound devices are still being found: the tab says so and is made once they are. Waiting for them
            # here held the window 9.5-12.8 s on a light PC while live (Mikedog151, 2026-11-23).
            if name not in self._finding:
                self._finding[name] = _hint("Finding sound devices…")
                self._holders[name].addWidget(self._finding[name], 0, Qt.AlignmentFlag.AlignTop)
            QTimer.singleShot(DEVICES_RETRY_MS, self, lambda: self.build_tab(name, later))
            return
        finding = self._finding.pop(name, None)
        if finding is not None:
            finding.deleteLater()
        box = self._holders.pop(name, None)
        if box is None:
            return
        if name in self.later:
            self.later.remove(name)
        devices = None
        if name == "Devices":
            devices = self._audio_devices
            devices = devices() if callable(devices) else devices
            card = self._devices(devices)
        elif name == "Tracks":
            card = self._tracks(self.recording)
        else:
            card = self._grid()
        box.setSpacing(14)
        box.addWidget(card)
        box.addStretch(1)
        more = []
        if name == "Devices":
            more = [lambda: self._hearing(devices.get("audio_output", [])), self._push]
        if name == "Sounds":
            more = [self._ducking]
        for at, make in enumerate(more, 1):
            part = (box, at, make)
            self._parts_waiting.append(part)
            if later is None:
                self._make_part(part)
            else:
                later(lambda part=part: self._make_part(part))
        self._tab_shown()

    def _make_part(self, part) -> None:
        """One more card of a tab, under the ones made before it (once: finish() may have made it already)."""
        if part not in self._parts_waiting:
            return
        self._parts_waiting.remove(part)
        box, at, make = part
        box.insertWidget(min(at, box.count() - 1), make())  # in its place, whichever is made first; above the stretch

    def finish(self) -> None:
        """Make every tab, card and sound row still waiting (before reading what's picked)."""
        for name in [*self.later, *self._finding]:
            self.build_tab(name, wait=True)
        for part in list(self._parts_waiting):
            self._make_part(part)
        while getattr(self, "_rows_waiting", None):
            self._sound_row(*self._rows_waiting.pop(0))
        table = getattr(self, "_sound_table", None)
        if table is not None:
            table.setVisible(True)

    def _ducking(self) -> QWidget:
        """Ducking's everyday picks, under the sounds it lowers. More… opens the rest (voices, timing)."""
        from sprigcast.app.card import field_label

        group = QGroupBox("Ducking")
        # Each pick under its own small label, side by side (as the New type window): a label column on the left
        # sat off-line with the pills and read as an older form (user 2026-11-20).
        grid = QGridLayout(group)
        grid.setContentsMargins(4, 6, 4, 4)
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(8)
        self.duck_when = QComboBox()
        self.duck_when.setObjectName("audioPick")
        self.duck_when.setAccessibleName("Music drops when")
        self.duck_when.setCursor(Qt.CursorShape.PointingHandCursor)
        self.duck_when.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.duck_when.addItem("Never", False)
        self.duck_when.addItem("Mic is talking", True)
        grid.addWidget(field_label("Music drops when"), 0, 0)
        grid.addWidget(self.duck_when, 1, 0, Qt.AlignmentFlag.AlignVCenter)  # in line with the pills beside it
        self.duck_by = Segmented(list(DUCK_PICKS))
        self.duck_by.setAccessibleName("Lower by")
        self.ducking_button = QPushButton("More…")
        self.ducking_button.setAutoDefault(False)
        self.ducking_button.setAccessibleName("More ducking settings")
        self.ducking_button.clicked.connect(self._more_ducking)
        picks = QHBoxLayout()
        picks.setSpacing(12)
        picks.addWidget(self.duck_by)
        picks.addWidget(self.ducking_button)
        grid.addWidget(field_label("Lower by"), 0, 1)
        grid.addLayout(picks, 1, 1)
        grid.setColumnStretch(2, 1)
        self.duck_when.currentIndexChanged.connect(lambda _i: self.duck_by.setEnabled(bool(self.duck_when.currentData())))
        self._load_ducking()
        return group

    def _load_ducking(self) -> None:
        """Show what the collection has now (at first, and after More… saved its own changes)."""
        audio = self.collection.audio
        self._duck_loaded = (bool(audio.ducking), float(audio.duck_amount_db))
        self.duck_when.setCurrentIndex(1 if audio.ducking else 0)
        self.duck_by.set_value(float(audio.duck_amount_db))  # an amount set under More… lights none
        self.duck_by.setEnabled(bool(audio.ducking))

    def _more_ducking(self) -> None:
        if self.on_ducking:
            self.on_ducking(self)
            self._load_ducking()

    # Meters (Devices): only while that tab is on screen ------------------------------------

    def _tab_shown(self, *_args) -> None:
        self._fit_tabs()
        name = self.TABS[self.tabs.currentIndex()]
        want = bool(self.meters) and self._levels is not None and name == "Devices" and self.isVisible()
        if want != self._meter_timer.isActive():
            self._meter_timer.start() if want else self._meter_timer.stop()
        if name == "Tracks" and hasattr(self, "track_chips"):
            self._fill_chips()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._tab_shown()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self._meter_timer.stop()

    def _move_meters(self) -> None:
        try:
            heard_at, levels = self._levels()
        except Exception:  # noqa: BLE001  a meter is never in the way of Settings
            heard_at, levels = 0.0, {}
        fresh = time.monotonic() - heard_at < STALE_S
        for source_id, meter in self.meters.items():
            meter.set_db(levels[source_id][1] if fresh and source_id in levels else -120.0)

    def show_tab(self, name: str) -> None:
        if name in self.TABS:
            self.tabs.setCurrentIndex(self.TABS.index(name))

    # Devices -----------------------------------------------------------------------------

    def _combo(self, source: SourceConfig, names: list[str]) -> QComboBox:
        combo = QComboBox()
        combo.addItem("None", NO_DEVICE)  # takes the sound away on Save (user 2026-11-20: no way to remove it)
        combo.addItem("Default (follows Windows)", "default")
        for name in names:
            combo.addItem(nice_device_name(name), name)
        current = str(source.settings.get("device", "default"))
        if combo.findData(current) < 0:
            combo.addItem(f"{current} (not connected)", current)
        combo.setCurrentIndex(combo.findData(current))
        self.device_combos[source.id] = combo
        return combo

    def _devices(self, audio_devices: dict[str, list[str]]) -> QWidget:
        group = QGroupBox("Devices")
        box = QVBoxLayout(group)
        box.setContentsMargins(0, 0, 0, 0)  # the card's own padding (settings_window style) is enough
        box.setSpacing(12)
        c = self.collection
        self.global_on = QCheckBox("Use the same mic and desktop sound in every bloom")
        self.global_on.setToolTip("Off: they stay closed and leave the Sound list; add your own in Build → + Add")
        self.global_on.setChecked(c.audio.global_devices)
        box.addWidget(self.global_on)
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        global_ids = c.global_device_ids()
        self.global_rows: list[QWidget] = []
        for source in (s for s in c.sources if s.id in global_ids):
            combo = self._combo(source, audio_devices.get(source.kind, []))
            form.addRow("Desktop sound" if source.kind == "audio_output" else "Mic", self._with_meter(source, combo))
            self.global_rows.append(combo)
        if not global_ids:
            form.addRow(_hint("No global devices. Add a mic or desktop sound in Build → + Add."))
        box.addLayout(form)
        if any(is_mixer_app_device(n) for n in audio_devices.get("audio_output", [])):
            box.addWidget(_hint("Elgato Wave Link is here: its channels (Game, Music, System…) are at the top of "
                                "each list. A game or window capture can take its own sound too: right-click it → "
                                "Sound. If you use a Wave Link mix that holds your mic, turn the separate Mic off."))
        own = [s for s in c.sources if s.kind in AUDIO_DEVICE_KINDS and s.id not in global_ids]
        if own:
            box.addSpacing(6)
            box.addWidget(_hint("Added by you"))
            own_form = QFormLayout()
            own_form.setHorizontalSpacing(18)
            own_form.setVerticalSpacing(10)
            own_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            for source in own:
                combo = self._combo(source, audio_devices.get(source.kind, []))
                own_form.addRow(source.name, self._with_meter(source, combo))
            box.addLayout(own_form)
        self.global_on.toggled.connect(self._global_toggled)
        self._global_toggled(self.global_on.isChecked())
        return group

    def _hearing(self, speakers: list[str]) -> QWidget:
        """Hearing it yourself: where "Hear it" sounds play, and Windows left out of your mix."""
        audio = self.collection.audio
        group = QGroupBox("Hearing it yourself")
        box = QVBoxLayout(group)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.listen_through = QComboBox()
        self.listen_through.addItem("Default (follows Windows)", "")
        for name in speakers:
            self.listen_through.addItem(nice_device_name(name), name)
        if audio.monitor_device and self.listen_through.findData(audio.monitor_device) < 0:
            self.listen_through.addItem(f"{nice_device_name(audio.monitor_device)} (not connected)",
                                        audio.monitor_device)
        self.listen_through.setCurrentIndex(max(0, self.listen_through.findData(audio.monitor_device)))
        self.listen_through.setToolTip("Sounds set to Both or Just me under Sounds play here")
        form.addRow("Listen through", self.listen_through)
        box.addLayout(form)
        self.windows_ducking_off = QCheckBox("Stop Windows turning other apps down when a mic opens")
        self.windows_ducking_off.setChecked(audio.windows_ducking_off)
        box.addWidget(self.windows_ducking_off)
        return group

    def _push(self) -> QWidget:
        """Push to talk: the mic is heard only while a key is held (or silent while it is)."""
        from PySide6.QtGui import QKeySequence
        from PySide6.QtWidgets import QKeySequenceEdit

        audio = self.collection.audio
        group = QGroupBox("Push to talk")
        form = QFormLayout(group)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.push_mode = Segmented([("off", "Off"), ("talk", "Push to talk"), ("mute", "Push to mute")],
                                   audio.push_mode)
        mic = self.collection.source(audio.push_mic(self.collection)) if audio.push_mic(self.collection) else None
        form.addRow("Mic", self.push_mode)
        self.push_mode.setToolTip(f"Holds {mic.name}" if mic is not None else "Add a mic first")
        self.push_key = QKeySequenceEdit(QKeySequence(audio.push_key))
        self.push_key.setMaximumSequenceLength(1)
        self.push_key.setFixedWidth(150)
        line = self.push_key.findChild(QLineEdit)
        if line is not None:
            line.setPlaceholderText("No key")
            line.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_key = QPushButton("Set key")
        self.set_key.setAutoDefault(False)
        self.set_key.setToolTip("Then press the key to hold")
        self.set_key.clicked.connect(self._set_push_key)
        self.push_note = QLabel()
        self.push_note.setStyleSheet(f"color: {theme.WARN}")
        key_row = QHBoxLayout()
        key_row.setSpacing(10)
        key_row.addWidget(self.push_key)
        key_row.addWidget(self.set_key)
        key_row.addWidget(self.push_note, 1)
        form.addRow("Key", key_row)
        self.push_release = Segmented([(ms, f"{ms} ms") for ms in PUSH_RELEASE_MS], int(audio.push_release_ms))
        self.push_release.setToolTip("Letting go counts this much later, so the end of a word isn't cut off")
        form.addRow("Let go after", self.push_release)
        self._push_rows = (self.push_key, self.set_key, self.push_release)
        self.push_mode.picked.connect(self._push_changed)
        self.push_key.keySequenceChanged.connect(self._push_changed)
        self._push_changed()
        if mic is None:
            self.push_mode.setEnabled(False)
        return group

    def push_key_text(self) -> str:
        from PySide6.QtGui import QKeySequence

        return self.push_key.keySequence().toString(QKeySequence.SequenceFormat.PortableText)

    def _set_push_key(self) -> None:
        self.push_key.clear()
        self.push_key.setFocus()

    def _push_changed(self, *_args) -> None:
        """The key and Let go after are for when it's on; say so when it's on with no key, or a key that's taken."""
        from sprigcast.app import hotkeys

        on = self.push_mode.value() != "off"
        for widget in self._push_rows:
            widget.setEnabled(on)
        key = self.push_key_text()
        used = next((a for a, combo in (self.collection.hotkeys or {}).items() if key and combo == key), "")
        note = ""
        if on and not key:
            note = "Pick a key"
        elif key and hotkeys.taken_by(key):
            note = hotkeys.taken_by(key)
        elif key and used:
            note = "A hotkey uses this key too"
        self.push_note.setText(note)

    def _with_meter(self, source: SourceConfig, combo: QComboBox) -> QWidget:
        """The device's list with its level beside it (when Settings was given levels to show)."""
        if self._levels is None:
            return combo
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(14)
        row.addWidget(combo, 1)
        meter = LevelDots()
        self.meters[source.id] = meter
        row.addWidget(meter)
        return holder

    def _global_toggled(self, on: bool) -> None:
        for row in self.global_rows:
            row.setEnabled(on)
        global_ids = set(self.collection.global_device_ids())
        for source_id, meter in self.meters.items():
            if source_id in global_ids:
                meter.setEnabled(on)
                meter.update()

    # Tracks ------------------------------------------------------------------------------

    def _tracks(self, recording: bool) -> QWidget:
        group = QGroupBox("Tracks")
        box = QVBoxLayout(group)
        box.setContentsMargins(0, 0, 0, 0)  # the card's own padding (settings_window style) is enough
        box.setSpacing(12)
        table = QFrame()
        table.setObjectName("audioTable")
        grid = QGridLayout(table)
        grid.setContentsMargins(16, 10, 16, 10)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        for col, text in enumerate(("#", "Name", "What's on it", "In recordings")):
            grid.addWidget(_head(text), 0, col)
        audio, kept = self.collection.audio, set(self.collection.recording.audio_tracks)
        self.track_names: list[QLineEdit] = []
        self.track_kept: list[QCheckBox] = []
        self.track_chips: list[QHBoxLayout] = []
        for n in range(1, len(AUDIO_TRACKS) + 1):
            grid.addWidget(_line(), 2 * n - 1, 0, 1, 4)
            number = QLabel(str(n))
            number.setObjectName("audioName")
            grid.addWidget(number, 2 * n, 0)
            name = QLineEdit(audio.track_name(n))
            name.setPlaceholderText(TRACK_NAMES[n - 1])
            name.setMaxLength(24)
            name.setToolTip(TRACK_TIPS[n - 1])
            name.setFixedWidth(150)
            grid.addWidget(name, 2 * n, 1)
            chips = QHBoxLayout()
            chips.setSpacing(6)
            grid.addLayout(chips, 2 * n, 2)
            keep = Switch()
            keep.setChecked(n in kept or n == 1)
            keep.setEnabled(n != 1 and not recording)  # track 1 is always there: it's what players play
            keep.setToolTip(TRACK_TIPS[n - 1] if n != 1 else "Always kept: players play the first track")
            grid.addWidget(keep, 2 * n, 3, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self.track_names.append(name)
            self.track_kept.append(keep)
            self.track_chips.append(chips)
        grid.setColumnStretch(2, 1)
        box.addWidget(table)
        box.addWidget(_hint("Track 2 leaves music out, so uploads don't get copyright strikes."
                            + (" Stop the recording to change which tracks it keeps." if recording else "")))
        self._fill_chips()
        return group

    def sounds_on_track(self, number: int) -> list[str]:
        """Names of the sounds on track 1-5: as ticked in Sounds where that row is made, else as saved."""
        names = []
        for source in self.collection.sources:
            if not has_sound(source) or self.collection.device_off(source):
                continue
            ticks, row = getattr(self, "track_ticks", {}).get(source.id), getattr(self, "sound_rows", {}).get(source.id)
            if ticks is not None and row is not None:
                on = ticks[number - 1].isChecked() and row["hear"].currentData() != "only"
                on = on and (number != 2 or ticks[0].isChecked())  # VOD-safe is a part of track 1
            else:
                on = on_track(source, number)
            if on:
                names.append(source.name)
        return names

    def _fill_chips(self) -> None:
        """What's on each track, from the Sounds ticks as they stand (asked again each time Tracks is shown)."""
        wanted = [self.sounds_on_track(n) for n in range(1, len(self.track_chips) + 1)]
        if wanted == getattr(self, "_chips_shown", None):
            return
        self._chips_shown = wanted
        for chips, names in zip(self.track_chips, wanted):
            while chips.count():
                item = chips.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            shown = names[:CHIPS_SHOWN]
            for text in shown or ["Nothing yet"]:
                chip = QLabel(text, self)
                if shown:
                    cut = chip.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, CHIP_TEXT_PX)
                    if cut != text:
                        chip.setText(cut)
                        chip.setToolTip(text)
                    chip.setObjectName("audioChip")
                    chip.setTextFormat(Qt.TextFormat.PlainText)
                else:
                    chip.setStyleSheet(f"color: {theme.MUTED}")
                chips.addWidget(chip)
            if len(names) > len(shown):
                more = QLabel(f"+{len(names) - len(shown)} more", self)
                more.setToolTip(", ".join(names[len(shown):]))
                more.setStyleSheet(f"color: {theme.MUTED}")
                chips.addWidget(more)
            chips.addStretch(1)

    # Sounds: one row per sound ---------------------------------------------------------------

    HEAR = (("Stream", False), ("Stream + me", True), ("Just me", "only"))  # as the Live sound card says

    def _grid(self) -> QWidget:
        group = QGroupBox("Sounds")
        box = QVBoxLayout(group)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        sounds = [s for s in self.collection.sources if has_sound(s) and not self.collection.device_off(s)]
        self.track_ticks: dict[str, list[QCheckBox]] = {}
        self.sound_rows: dict[str, dict] = {}
        if not sounds:
            box.addWidget(_hint("No sounds yet."))
            return group
        self._sound_type = {s.id: self._type_of(s) for s in sounds}
        self._sound_name = {s.id: s.name for s in sounds}
        self._cells: dict[str, list[tuple[int, QWidget]]] = {}  # each row's boxes, with their column
        self._lines: dict[str, QFrame] = {}
        box.addLayout(self._sound_filter())
        table = QFrame()
        table.setObjectName("audioTable")
        grid = QGridLayout(table)
        grid.setContentsMargins(14, 10, 14, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        tracks = len(AUDIO_TRACKS)
        self._heads = []
        for col, text in enumerate(("Sound", "Volume", "Balance", "Mono", "Delay", "Hear it", "Goes to", "Mute")):
            head = _head(text)
            self._heads.append(head)
            grid.addWidget(head, 0, col)
        for row, source in enumerate(sounds, 1):
            line = self._lines[source.id] = _line()
            grid.addWidget(line, 2 * row - 1, 0, 1, 8)
        self._filter_sounds()
        self._rows_waiting = [(grid, row, source, tracks) for row, source in enumerate(sounds, 1)]
        for _ in range(min(self.ROWS_NOW, len(self._rows_waiting))):
            self._sound_row(*self._rows_waiting.pop(0))
        # The rest half a row a tick: a whole row was still 57-78 ms a tick on the PC while live (2026-11-18).
        self._rows_waiting = [(*args, half) for args in self._rows_waiting for half in (1, 2)]
        # The table shows once a screenful of rows is made, all at once: shown from the first row, the list was
        # seen filling in one row at a time (user 2026-11-20). The rest are made below, off screen.
        self._sound_table = table
        self._rows_to_show = min(self.SCREEN_ROWS, len(sounds)) * 2 - self.ROWS_NOW * 2
        if self._rows_waiting and self._rows_to_show > 0:
            table.setVisible(False)
        if self._rows_waiting:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(self.ROW_GAP_MS, self, self._more_rows)
        grid.setColumnStretch(0, 1)
        box.addWidget(table)
        return group

    # Sound rows made with the tab; the rest a few per tick (ROWS_PER_TICK). None now: two rows (~100 calls into Qt)
    # made with the page were most of the 61 ms the Sound gear took to open on the PC while live (2026-11-19).
    ROWS_NOW = 0
    ROWS_PER_TICK = 1  # three were 136 ms a tick on the PC while live (2026-11-18): each row is ~50 calls into Qt
    TICK_MS = 8  # more halves in a tick while they're quick; a slow PC still makes one a tick
    # Never 0 between ticks. On Windows a 0 ms timer is a posted event, handled ahead of the window's paints and
    # every real timer, so a chain of them held the whole list: Settings from the Sound gear didn't answer for
    # 285-340 ms every open (PC 2026-11-20, build 44785036541), the beat that watches the window starved with it.
    # A 1 ms timer is a real timer: paints, clicks and other timers go first, then the next tick.
    ROW_GAP_MS = 1
    SCREEN_ROWS = 12  # about what the Sounds tab shows without scrolling

    def _more_rows(self) -> None:
        """More sound rows, a little at a time with only a timer tick in between (a 15 ms gap after every half row
        made a long list take seconds; none at all froze the window while the list was made, see ROW_GAP_MS). All of them at once (6 boxes and up to 5 ticks each) held the window 263 ms on
        the PC while live (2026-11-18 pop-up sweep)."""
        import time

        waiting = self._rows_waiting
        end = time.perf_counter() + self.TICK_MS / 1000
        made = 0
        while waiting and (made < self.ROWS_PER_TICK or time.perf_counter() < end):
            self._sound_row(*waiting.pop(0))
            made += 1
        self._rows_to_show = getattr(self, "_rows_to_show", 0) - made
        table = getattr(self, "_sound_table", None)
        if table is not None and table.isHidden() and (self._rows_to_show <= 0 or not waiting):
            table.setVisible(True)
        if waiting:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(self.ROW_GAP_MS, self, self._more_rows)

    def _sound_row(self, grid: QGridLayout, row: int, source: SourceConfig, tracks: int, half: int = 0) -> None:
        """A sound's row; half 1 or 2: its first boxes (name to sync offset) or the rest (listeners, tracks)."""
        st = source.settings
        if half == 2:
            volume, mono, balance, sync = self._first_halves.pop(source.id)
            self._row_rest(grid, row, source, tracks, st, volume, mono, balance, sync)
            return
        at = 2 * row  # (odd rows are the hairlines between sounds)
        name = QLabel(source.name)
        name.setObjectName("audioName")
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setMinimumWidth(70)  # a long name is cut short, never wider than the window; the tip has all of it
        name.setToolTip(source.name)
        grid.addWidget(name, at, 0)
        self.__dict__.setdefault("name_labels", {})[source.id] = name  # renamed in place when its device changes
        volume = DbSlider(float(st.get("volume_db", 0.0) or 0.0))
        grid.addWidget(volume, at, 1)
        balance = QSlider(Qt.Orientation.Horizontal)
        balance.setObjectName("balance")
        balance.setRange(-100, 100)
        balance.setValue(round(float(st.get("balance", 0.0) or 0.0) * 100))
        balance.setFixedWidth(64)
        balance.setToolTip("Left / right")
        balance.valueChanged.connect(lambda v, b=balance: b.setToolTip(
            "Center" if not v else f"{abs(v)}% {'left' if v < 0 else 'right'}"))
        grid.addWidget(balance, at, 2)
        mono = QCheckBox()
        mono.setChecked(bool(st.get("mono", False)))
        grid.addWidget(mono, at, 3, alignment=_CENTRE)
        sync = QSpinBox()
        sync.setObjectName("audioDelay")
        sync.setRange(0, 2000)
        sync.setSingleStep(10)
        sync.setSuffix(" ms")
        sync.setValue(int(st.get("delay_ms", 0) or 0))
        sync.setFixedWidth(72)
        sync.setAlignment(Qt.AlignmentFlag.AlignHCenter)  # (the theme then gives the number room on both sides)
        sync.setToolTip("Hold this sound back to line it up with a camera that runs late")
        grid.addWidget(sync, at, 4)
        self._cells[source.id] = [(0, name), (1, volume), (2, balance), (3, mono), (4, sync)]
        self._show_row(source.id)
        if half == 1:
            self.__dict__.setdefault("_first_halves", {})[source.id] = (volume, mono, balance, sync)
            return
        self._row_rest(grid, row, source, tracks, st, volume, mono, balance, sync)

    def _row_rest(self, grid, row, source, tracks, st, volume, mono, balance, sync) -> None:
        hear = QComboBox()
        hear.setObjectName("audioPick")
        hear.setCursor(Qt.CursorShape.PointingHandCursor)
        current = st.get("monitor", False)
        for text, value in self.HEAR:
            hear.addItem(text, value)
        hear.setCurrentIndex(2 if current == "only" else 1 if current else 0)
        hear.setEnabled(source.kind != "audio_output")  # desktop sound already plays on your speakers
        hear.setFixedWidth(124)
        grid.addWidget(hear, 2 * row, 5)
        ticks = []
        have = planned_tracks(source)  # (Just me shows where it goes once it's back) once a row, not once a tick (on_track asks for all of them each time)
        names = self._track_names(tracks)
        pills = QHBoxLayout()
        pills.setSpacing(4)
        # The stream and clips are their own mixes, not recording tracks: each gets its own pill, so a sound
        # taken off every track can leave the stream too (PC 2026-11-20: Desktop stayed on stream and clips).
        mixes = {}
        for key, text, tip in (("stream", "Stream", "the stream"), ("clips", "Clips", "saved clips")):
            pill = QPushButton(text)
            pill.setObjectName("mixPill")
            pill.setCheckable(True)
            pill.setAutoDefault(False)
            pill.setChecked(key in have)
            pill.setToolTip(f"{source.name} on {tip}")
            pills.addWidget(pill)
            mixes[key] = pill
        for n in range(1, tracks + 1):
            tick = QPushButton(str(n))
            tick.setObjectName("trackPill")
            tick.setCheckable(True)
            tick.setAutoDefault(False)
            tick.setChecked(AUDIO_TRACKS[n - 1] in have)
            tick.setToolTip(f"{source.name} on {names[n - 1]} (track {n})")
            pills.addWidget(tick)
            ticks.append(tick)
        grid.addLayout(pills, 2 * row, 6)
        mute = QCheckBox()
        mute.setChecked(bool(st.get("muted", False)))
        mute.setToolTip(f"Mute {source.name}")
        grid.addWidget(mute, 2 * row, 7, alignment=_CENTRE)
        self.track_ticks[source.id] = ticks
        self._cells.setdefault(source.id, []).extend(
            [(5, hear), *((6, w) for w in (*mixes.values(), *ticks)), (7, mute)])
        self._show_row(source.id)
        self.sound_rows[source.id] = {"volume": volume, "mono": mono, "balance": balance, "sync": sync,
                                      "hear": hear, "mute": mute, **mixes,
                                      "vod": bool(st.get("vod", not is_music(source)))}
        for pill in mixes.values():
            pill.setEnabled(current != "only")
        if len(ticks) > 1:
            ticks[1].toggled.connect(lambda on, sid=source.id, t=ticks[1]: t.isEnabled() and
                                     self.sound_rows[sid].__setitem__("vod", on))
        ticks[0].toggled.connect(lambda _on, sid=source.id: self._row_ticks(sid))
        hear.currentIndexChanged.connect(lambda _i, sid=source.id: self._row_ticks(sid))
        only_me = current == "only"  # as _row_ticks would find it, without asking Qt back
        first = ticks[0].isChecked() if ticks else False
        for n, tick in enumerate(ticks, 1):
            if only_me or (n == 2 and not first):
                tick.setEnabled(False)
            if n == 2 and not first:
                tick.setChecked(False)  # VOD-safe is part of track 1: never shown on while it can't be

    # Finding a sound ----------------------------------------------------------------------
    # A long list (57 sounds on the PC, 2026-11-20) was "overwhelming": Find narrows it by name, Type to one sound
    # type, and the columns come in two views so a row always fits the page (no sideways scrolling).
    VIEWS = (("levels", "Volume and sync"), ("routing", "Where it goes"))
    VIEW_COLUMNS = {"levels": {0, 1, 2, 3, 4, 7}, "routing": {0, 5, 6, 7}}
    VIEW_KEY = "settings/audio_sounds_view"

    @staticmethod
    def _type_of(source: SourceConfig) -> str:
        from sprigcast.app.sound_card import auto_group, type_of

        st = source.settings
        return type_of(str(st.get("group") or ("Browser" if source.kind == "browser"
                                               else auto_group(source.name, source.kind))))

    def _sound_filter(self) -> QHBoxLayout:
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QMenu

        from sprigcast.app.sound_card import GROUP_COLOURS, GROUPS
        from sprigcast.app.sound_strips import _dot

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(10)
        self.sound_find = QLineEdit()
        self.sound_find.setPlaceholderText("Find a sound")
        self.sound_find.setClearButtonEnabled(True)
        self.sound_find.setAccessibleName("Find a sound")
        self.sound_find.setMinimumWidth(160)
        self.sound_find.textChanged.connect(lambda _t: self._filter_sounds())
        bar.addWidget(self.sound_find, 1)
        self.sound_type = QPushButton()
        self.sound_type.setAutoDefault(False)
        self.sound_type.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sound_type.setAccessibleName("Type")
        self.sound_type.setStyleSheet(theme.button_style(13, "0px 12px", extra="min-height: 26px;"))
        menu = QMenu(self.sound_type)
        counts = {g: sum(t == g for t in self._sound_type.values()) for g in GROUPS}
        self.sound_type_actions = {"": menu.addAction("All types", lambda: self._pick_sound_type(""))}
        for g in GROUPS:
            self.sound_type_actions[g] = menu.addAction(_dot(GROUP_COLOURS[g]), f"{g} ({counts[g]})",
                                                        lambda g=g: self._pick_sound_type(g))
        self.sound_type.setMenu(menu)
        bar.addWidget(self.sound_type)
        view = str(QSettings().value(self.VIEW_KEY, "levels"))
        self.sound_view_shown = view if view in self.VIEW_COLUMNS else "levels"
        self.sound_view = Segmented(list(self.VIEWS), self.sound_view_shown)
        self.sound_view.picked.connect(self._pick_sound_view)
        bar.addWidget(self.sound_view)
        self.sound_count = QLabel()
        self.sound_count.setObjectName("audioHead")
        bar.addWidget(self.sound_count)
        self._pick_sound_type("", filter_now=False)
        return bar

    def _pick_sound_type(self, group: str, filter_now: bool = True) -> None:
        from sprigcast.app.sound_card import GROUP_COLOURS
        from sprigcast.app.sound_strips import _dot

        self.sound_type_shown = group
        self.sound_type.setText(f"{group or 'All types'}  ▾")
        self.sound_type.setIcon(_dot(GROUP_COLOURS[group]) if group else QIcon())
        if filter_now:
            self._filter_sounds()

    def _pick_sound_view(self, view: str) -> None:
        from PySide6.QtCore import QSettings

        self.sound_view_shown = view
        QSettings().setValue(self.VIEW_KEY, view)
        self._filter_sounds()

    def sound_shown(self, source_id: str) -> bool:
        """Whether a sound's row passes Find and Type."""
        text = self.sound_find.text().strip().casefold() if hasattr(self, "sound_find") else ""
        if text and text not in self._sound_name.get(source_id, "").casefold():
            return False
        group = getattr(self, "sound_type_shown", "")
        return not group or self._sound_type.get(source_id) == group

    def _show_row(self, source_id: str) -> None:
        keep = self.sound_shown(source_id)
        columns = self.VIEW_COLUMNS[getattr(self, "sound_view_shown", "levels")]
        for col, widget in self._cells.get(source_id, ()):
            widget.setVisible(keep and col in columns)
        line = self._lines.get(source_id)
        if line is not None:
            line.setVisible(keep)

    def _filter_sounds(self) -> None:
        if not hasattr(self, "_heads"):
            return
        columns = self.VIEW_COLUMNS[self.sound_view_shown]
        for col, head in enumerate(self._heads):
            head.setVisible(col in columns)
        shown = 0
        for source_id in self._sound_name:
            self._show_row(source_id)
            shown += self.sound_shown(source_id)
        total = len(self._sound_name)
        self.sound_count.setText(f"{shown} of {total}" if shown != total else f"{total} sounds")

    def _track_names(self, tracks: int) -> list[str]:
        """The tracks' names, worked out once a build of the page (each row's ticks asked again)."""
        key = (tracks, tuple(getattr(self.collection.audio, "track_names", None) or ()))
        if getattr(self, "_names_for", None) != key:
            self._names_for = key
            self._names = [self.collection.audio.track_name(n) for n in range(1, tracks + 1)]
        return self._names

    def _row_ticks(self, source_id: str) -> None:
        """Just me: on no track. VOD-safe is a part of track 1, so it needs track 1."""
        ticks = self.track_ticks[source_id]
        row = self.sound_rows[source_id]
        only_me = row["hear"].currentData() == "only"
        for n, tick in enumerate(ticks, 1):
            tick.setEnabled(not only_me and (n != 2 or ticks[0].isChecked()))
        if len(ticks) > 1:  # VOD-safe shows what it will be: off without track 1, as it was with it
            ticks[1].setChecked(ticks[0].isChecked() and row["vod"])
        for key in ("stream", "clips"):
            row[key].setEnabled(not only_me)

    # Saving ------------------------------------------------------------------------------

    def apply_to(self, collection: SceneCollection, recording: bool = False) -> None:
        self.finish()
        audio = collection.audio
        if audio.global_sources is None:  # pin the global devices before anything else moves
            audio.global_sources = collection.global_device_ids()
        audio.global_devices = self.global_on.isChecked()
        audio.monitor_device = self.listen_through.currentData() or ""
        audio.windows_ducking_off = self.windows_ducking_off.isChecked()
        audio.push_mode = self.push_mode.value() or "off"
        audio.push_key = self.push_key_text()
        if self.push_release.value() is not None:  # (a time set by hand in the file lights none: left as it is)
            audio.push_release_ms = int(self.push_release.value())
        audio.track_names = [e.text().strip() or TRACK_NAMES[i] for i, e in enumerate(self.track_names)]
        on, by = bool(self.duck_when.currentData()), self.duck_by.value()
        if on != self._duck_loaded[0]:  # only what was changed here: More… saves its own
            audio.ducking = on
        if by is not None and by != self._duck_loaded[1]:
            audio.duck_amount_db = float(by)
        self._duck_loaded = (bool(audio.ducking), float(audio.duck_amount_db))
        if not recording:
            collection.recording.audio_tracks = [n for n, keep in enumerate(self.track_kept, 1) if keep.isChecked()]
            collection.recording.voice_track = 3 in collection.recording.audio_tracks
        gone = [s.id for s in collection.sources
                if (combo := self.device_combos.get(s.id)) is not None and combo.currentData() == NO_DEVICE]
        for source_id in gone:  # "None": the sound leaves, global or not
            audio.global_sources = [i for i in audio.global_sources if i != source_id]
            collection.remove_source_if_unused(source_id, sounds_too=True)
        for source in collection.sources:
            combo = self.device_combos.get(source.id)
            if combo is not None and combo.currentData() != source.settings.get("device", "default"):
                device = combo.currentData()
                old = str(source.settings.get("device", "default") or "default")
                source.settings = {**source.settings, "device": device}
                if sound_name_is_ours(source.name, source.kind, old):  # a name you chose stays (#805)
                    base = "Desktop audio" if source.kind == "audio_output" else "Mic"
                    source.name = base if device == "default" else f"{base} ({device})"
                    log.info("sound %s: renamed %s for its new device", source.id, source.name)
                label = getattr(self, "name_labels", {}).get(source.id)
                if label is not None:  # the Sounds row says the new name now, not after the page is made again
                    label.setText(source.name)
                    label.setToolTip(source.name)
            row = self.sound_rows.get(source.id)
            if row is not None:
                wanted = {"volume_db": round(row["volume"].value(), 1), "mono": row["mono"].isChecked(),
                          "balance": row["balance"].value() / 100, "delay_ms": row["sync"].value(),
                          "monitor": row["hear"].currentData(), "muted": row["mute"].isChecked()}
                defaults = {"volume_db": 0.0, "mono": False, "balance": 0.0, "delay_ms": 0, "monitor": False,
                            "muted": False}
                changed = {k: v for k, v in wanted.items() if source.settings.get(k, defaults[k]) != v}
                if changed:
                    source.settings = {**source.settings, **changed}
            ticks = self.track_ticks.get(source.id)
            if ticks is not None and not is_monitor_only(source):
                for n, tick in enumerate(ticks, 1):
                    if tick.isEnabled():  # (VOD-safe without track 1 keeps its own setting for when it's back)
                        put_on_track(source, n, tick.isChecked())
                if row is not None:
                    for key in ("stream", "clips"):
                        have = source_tracks(source)
                        if (key in have) != row[key].isChecked():
                            main = [t for t in have if t in ("stream", "recording", "voice", "clips")]
                            source.settings = {**source.settings, "tracks": [t for t in main if t != key]
                                               + ([key] if row[key].isChecked() else [])}
