"""Settings → Audio, in tabs: Devices, Tracks and Sounds (which go on each track).

- Global devices: the Desktop audio and Mic every setup starts with. Switched off, they are not opened
  at all and leave the Sound list, so only the mics and desktop sounds you add yourself play.
- Tracks: a recording can keep up to five sound tracks. 1 is everything, 2 is VOD-safe (everything
  but the sounds you keep out of it, music by default), 3 is your voice, 4 and 5 are yours to fill.
- Sounds: one row per sound, like OBS's Advanced Audio Properties: volume, mono, balance, sync
  offset, listeners and its tracks. The Sound card's ⚙ opens it.

Sound bitrate sits with the rest of each encoder (Stream → Quality and Recording, expert rows).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.audio.mixer import is_monitor_only, source_tracks
from sprigcast.core.scene import AUDIO_DEVICE_KINDS, AUDIO_TRACKS, TRACK_NAMES, SceneCollection, SourceConfig
from sprigcast.sources.audio import device_label as nice_device_name
from sprigcast.sources.audio import is_mixer_app_device

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


def device_label(kind: str) -> str:
    return "Desktop audio" if kind == "audio_output" else "Mic"


class AudioPage(QWidget):
    TABS = ("Devices", "Tracks", "Sounds")

    def __init__(self, collection: SceneCollection, audio_devices, recording: bool = False,
                 streaming: bool = False, first: str | None = None):
        """audio_devices: a dict, or something that gives one when called (Settings: waits for the scan).
        first: make only this tab now; the others are listed in `later` (Settings makes them a tick apart, see
        SettingsWindow._slice) and made by finish() if still waiting. None: every tab now."""
        super().__init__()
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

    def build_tab(self, name: str) -> None:
        """Make one tab's contents (once)."""
        box = self._holders.pop(name, None)
        if box is None:
            return
        if name in self.later:
            self.later.remove(name)
        if name == "Devices":
            devices = self._audio_devices
            card = self._devices(devices() if callable(devices) else devices)
        elif name == "Tracks":
            card = self._tracks(self.recording)
        else:
            card = self._grid()
        box.addWidget(card)
        if name == "Sounds":
            box.addLayout(self._ducking_row())
        box.addStretch(1)

    def finish(self) -> None:
        """Make every tab and sound row still waiting (before reading what's picked)."""
        for name in list(self.later):
            self.build_tab(name)
        while getattr(self, "_rows_waiting", None):
            self._sound_row(*self._rows_waiting.pop(0))

    def _ducking_row(self) -> QHBoxLayout:
        """Ducking's amounts, next to the sounds it lowers (Live's Mic first switch turns it on and off)."""
        row = QHBoxLayout()
        self.ducking_button = QPushButton("Ducking…")
        self.ducking_button.setToolTip("How much other sounds dip while you talk (Mic first on Live)")
        self.ducking_button.clicked.connect(lambda: self.on_ducking and self.on_ducking(self))
        row.addWidget(self.ducking_button)
        row.addStretch(1)
        return row

    def show_tab(self, name: str) -> None:
        if name in self.TABS:
            self.tabs.setCurrentIndex(self.TABS.index(name))

    # Devices -----------------------------------------------------------------------------

    def _combo(self, source: SourceConfig, names: list[str]) -> QComboBox:
        combo = QComboBox()
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
        self.global_on = QCheckBox("Use global devices")
        self.global_on.setToolTip("On: the Desktop audio and Mic below play in every bloom.\n"
                                  "Off: they stay closed and leave the Sound list; add your own in Build → + Add.")
        self.global_on.setChecked(c.audio.global_devices)
        box.addWidget(self.global_on)
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        global_ids = c.global_device_ids()
        self.global_rows: list[QWidget] = []
        for source in (s for s in c.sources if s.id in global_ids):
            combo = self._combo(source, audio_devices.get(source.kind, []))
            form.addRow(device_label(source.kind), combo)
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
            for source in own:
                own_form.addRow(source.name, self._combo(source, audio_devices.get(source.kind, [])))
            box.addLayout(own_form)
        self.global_on.toggled.connect(self._global_toggled)
        self._global_toggled(self.global_on.isChecked())
        return group

    def _global_toggled(self, on: bool) -> None:
        for row in self.global_rows:
            row.setEnabled(on)

    # Tracks ------------------------------------------------------------------------------

    def _tracks(self, recording: bool) -> QWidget:
        group = QGroupBox("Tracks")
        box = QVBoxLayout(group)
        box.setContentsMargins(0, 0, 0, 0)  # the card's own padding (settings_window style) is enough
        box.setSpacing(12)
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        grid.addWidget(_hint("Track"), 0, 0)
        grid.addWidget(_hint("Name"), 0, 1)
        grid.addWidget(_hint("Recordings keep it"), 0, 2)
        audio, kept = self.collection.audio, set(self.collection.recording.audio_tracks)
        self.track_names: list[QLineEdit] = []
        self.track_kept: list[QCheckBox] = []
        for n in range(1, len(AUDIO_TRACKS) + 1):
            number = QLabel(str(n))
            number.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(number, n, 0)
            name = QLineEdit(audio.track_name(n))
            name.setPlaceholderText(TRACK_NAMES[n - 1])
            name.setMaxLength(24)
            name.setToolTip(TRACK_TIPS[n - 1])
            name.setFixedWidth(280)
            grid.addWidget(name, n, 1)
            keep = QCheckBox()
            keep.setChecked(n in kept or n == 1)
            keep.setEnabled(n != 1 and not recording)  # track 1 is always there: it's what players play
            keep.setToolTip(TRACK_TIPS[n - 1] if n != 1 else "Always kept: players play the first track")
            grid.addWidget(keep, n, 2, alignment=Qt.AlignmentFlag.AlignCenter)
            self.track_names.append(name)
            self.track_kept.append(keep)
        grid.setColumnStretch(3, 1)  # spare width goes to the right, so names and ticks sit together
        box.addLayout(grid)
        box.addWidget(_hint("Track 2 leaves out music, so you can upload it without copyright strikes. "
                            "Pick which sounds go on each track in Sounds."
                            + (" Stop the recording to change which tracks it keeps." if recording else "")))
        return group

    # Sounds: one row per sound, like OBS's Advanced Audio Properties -------------------------

    HEAR = (("Stream", False), ("Both", True), ("Just me", "only"))

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
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        tracks = len(AUDIO_TRACKS)
        heads = ["Sound", "Volume", "Mono", "Balance", "Sync offset", "Listeners",
                 *(str(n) for n in range(1, tracks + 1))]
        for col, text in enumerate(heads):
            head = QLabel(text)
            head.setStyleSheet(f"color: {theme.MUTED}")
            if col >= 6:
                head.setAlignment(Qt.AlignmentFlag.AlignCenter)
                head.setToolTip(f"Track {text}: {self.collection.audio.track_name(int(text))}")
            grid.addWidget(head, 0, col)
        self._rows_waiting = [(grid, row, source, tracks) for row, source in enumerate(sounds, 1)]
        for _ in range(min(self.ROWS_NOW, len(self._rows_waiting))):
            self._sound_row(*self._rows_waiting.pop(0))
        # The rest half a row a tick: a whole row was still 57-78 ms a tick on the PC while live (2026-11-18).
        self._rows_waiting = [(*args, half) for args in self._rows_waiting for half in (1, 2)]
        if self._rows_waiting:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(self.ROW_GAP_MS, self, self._more_rows)
        grid.setColumnStretch(0, 1)
        box.addLayout(grid)
        legend = " · ".join(f"{n} {self.collection.audio.track_name(n)}" for n in range(1, tracks + 1))
        box.addWidget(_hint(f"Tracks: {legend}"))
        return group

    ROWS_NOW = 2  # sound rows made with the tab; the rest a few per tick (ROWS_PER_TICK)
    ROWS_PER_TICK = 1  # three were 136 ms a tick on the PC while live (2026-11-18): each row is ~50 calls into Qt
    ROW_GAP_MS = 15

    def _more_rows(self) -> None:
        """A few more sound rows. All of them at once (6 boxes and up to 5 ticks each) held the window 263 ms on
        the PC while live (2026-11-18 pop-up sweep)."""
        waiting = self._rows_waiting
        for _ in range(self.ROWS_PER_TICK):
            if not waiting:
                break
            self._sound_row(*waiting.pop(0))
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
        grid.addWidget(QLabel(source.name), row, 0)
        volume = QDoubleSpinBox()
        volume.setRange(-60.0, 12.0)
        volume.setSingleStep(0.5)
        volume.setDecimals(1)
        volume.setSuffix(" dB")
        volume.setValue(float(st.get("volume_db", 0.0) or 0.0))
        volume.setFixedWidth(108)
        grid.addWidget(volume, row, 1)
        mono = QCheckBox()
        mono.setChecked(bool(st.get("mono", False)))
        grid.addWidget(mono, row, 2, alignment=_CENTRE)
        balance = QSlider(Qt.Orientation.Horizontal)
        balance.setRange(-100, 100)
        balance.setValue(round(float(st.get("balance", 0.0) or 0.0) * 100))
        balance.setFixedWidth(90)
        balance.setToolTip("Left / right (double-click the Sound card's slider to center)")
        balance.valueChanged.connect(lambda v, b=balance: b.setToolTip(
            "Center" if not v else f"{abs(v)}% {'left' if v < 0 else 'right'}"))
        grid.addWidget(balance, row, 3)
        sync = QSpinBox()
        sync.setRange(0, 2000)
        sync.setSingleStep(10)
        sync.setSuffix(" ms")
        sync.setValue(int(st.get("delay_ms", 0) or 0))
        sync.setFixedWidth(100)
        sync.setToolTip("Hold this sound back to line it up with a camera that runs late")
        grid.addWidget(sync, row, 4)
        if half == 1:
            self.__dict__.setdefault("_first_halves", {})[source.id] = (volume, mono, balance, sync)
            return
        self._row_rest(grid, row, source, tracks, st, volume, mono, balance, sync)

    def _row_rest(self, grid, row, source, tracks, st, volume, mono, balance, sync) -> None:
        hear = QComboBox()
        current = st.get("monitor", False)
        for text, value in self.HEAR:
            hear.addItem(text, value)
        hear.setCurrentIndex(2 if current == "only" else 1 if current else 0)
        hear.setEnabled(source.kind != "audio_output")  # desktop sound already plays on your speakers
        hear.setFixedWidth(118)
        grid.addWidget(hear, row, 5)
        ticks = []
        have = source_tracks(source)  # once a row, not once a tick (on_track asks for all of them each time)
        names = self._track_names(tracks)
        for n in range(1, tracks + 1):
            tick = QCheckBox()
            tick.setChecked(AUDIO_TRACKS[n - 1] in have)
            tick.setToolTip(f"{source.name} on {names[n - 1]} (track {n})")
            grid.addWidget(tick, row, 5 + n, alignment=_CENTRE)
            ticks.append(tick)
        self.track_ticks[source.id] = ticks
        self.sound_rows[source.id] = {"volume": volume, "mono": mono, "balance": balance, "sync": sync,
                                      "hear": hear}
        ticks[0].toggled.connect(lambda _on, sid=source.id: self._row_ticks(sid))
        hear.currentIndexChanged.connect(lambda _i, sid=source.id: self._row_ticks(sid))
        only_me = current == "only"  # as _row_ticks would find it, without asking Qt back
        first = ticks[0].isChecked() if ticks else False
        for n, tick in enumerate(ticks, 1):
            if only_me or (n == 2 and not first):
                tick.setEnabled(False)

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
        only_me = self.sound_rows[source_id]["hear"].currentData() == "only"
        for n, tick in enumerate(ticks, 1):
            tick.setEnabled(not only_me and (n != 2 or ticks[0].isChecked()))

    # Saving ------------------------------------------------------------------------------

    def apply_to(self, collection: SceneCollection, recording: bool = False) -> None:
        self.finish()
        audio = collection.audio
        if audio.global_sources is None:  # pin the global devices before anything else moves
            audio.global_sources = collection.global_device_ids()
        audio.global_devices = self.global_on.isChecked()
        audio.track_names = [e.text().strip() or TRACK_NAMES[i] for i, e in enumerate(self.track_names)]
        if not recording:
            collection.recording.audio_tracks = [n for n, keep in enumerate(self.track_kept, 1) if keep.isChecked()]
            collection.recording.voice_track = 3 in collection.recording.audio_tracks
        for source in collection.sources:
            combo = self.device_combos.get(source.id)
            if combo is not None and combo.currentData() != source.settings.get("device", "default"):
                device = combo.currentData()
                source.settings = {**source.settings, "device": device}
                base = device_label(source.kind)
                source.name = base if device == "default" else f"{base} ({device})"
            row = self.sound_rows.get(source.id)
            if row is not None:
                wanted = {"volume_db": round(row["volume"].value(), 1), "mono": row["mono"].isChecked(),
                          "balance": row["balance"].value() / 100, "delay_ms": row["sync"].value(),
                          "monitor": row["hear"].currentData()}
                defaults = {"volume_db": 0.0, "mono": False, "balance": 0.0, "delay_ms": 0, "monitor": False}
                changed = {k: v for k, v in wanted.items() if source.settings.get(k, defaults[k]) != v}
                if changed:
                    source.settings = {**source.settings, **changed}
            ticks = self.track_ticks.get(source.id)
            if ticks is not None and not is_monitor_only(source):
                for n, tick in enumerate(ticks, 1):
                    put_on_track(source, n, tick.isChecked())
