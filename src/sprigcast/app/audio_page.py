"""Settings → Audio, in tabs: Devices, Tracks, Sounds (which go on each track) and Bitrate.

- Global devices: the Desktop audio and Mic every setup starts with. Switched off, they are not opened
  at all and leave the Sound list, so only the mics and desktop sounds you add yourself play.
- Tracks: a recording can keep up to five sound tracks. 1 is everything, 2 is VOD-safe (everything
  but the sounds you keep out of it, music by default), 3 is your voice, 4 and 5 are yours to fill.
- Sounds: one row per sound, like OBS's Advanced Audio Properties: volume, mono, balance, sync
  offset, who hears it and its tracks. The Sound card's ⚙ opens it.
- Bitrate: how good the sound is on the stream and in recordings (clips and source record too).
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
    QLabel,
    QLineEdit,
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
    TABS = ("Devices", "Tracks", "Sounds", "Bitrate")

    def __init__(self, collection: SceneCollection, audio_devices: dict[str, list[str]], recording: bool = False,
                 streaming: bool = False):
        super().__init__()
        self.collection = collection
        self.streaming, self.recording = streaming, recording
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.device_combos: dict[str, QComboBox] = {}
        self.tabs = QTabWidget()
        for name, card in zip(self.TABS, (self._devices(audio_devices), self._tracks(recording), self._grid(),
                                          self._bitrate())):
            tab = QWidget()
            box = QVBoxLayout(tab)
            box.setContentsMargins(0, 4, 0, 0)
            box.addWidget(card)
            box.addStretch(1)
            self.tabs.addTab(tab, name)
        layout.addWidget(self.tabs)

    def show_tab(self, name: str) -> None:
        if name in self.TABS:
            self.tabs.setCurrentIndex(self.TABS.index(name))

    # Bitrate -----------------------------------------------------------------------------

    def _kbps(self, value: int) -> QComboBox:
        combo = QComboBox()
        for kbps in sorted({*AUDIO_KBPS, value}):
            combo.addItem(f"{kbps} kbps", kbps)
        combo.setCurrentIndex(combo.findData(value))
        return combo

    def _bitrate(self) -> QWidget:
        group = QGroupBox("Bitrate")
        box = QVBoxLayout(group)
        box.setContentsMargins(0, 0, 0, 0)  # the card's own padding (settings_window style) is enough
        box.setSpacing(12)
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        c = self.collection
        self.stream_kbps = self._kbps(c.stream_encoder.audio_kbps)
        self.stream_kbps.setEnabled(not self.streaming)
        self.stream_kbps.setFixedWidth(180)
        form.addRow("Stream", self.stream_kbps)
        self.recording_kbps = self._kbps(c.recording.encoder.audio_kbps)
        self.recording_kbps.setEnabled(not self.recording)
        self.recording_kbps.setFixedWidth(180)
        form.addRow("Recordings and clips", self.recording_kbps)
        box.addLayout(form)
        notes = ["160 kbps is plenty for a stream. Recordings can go higher: every track uses this bitrate."]
        if self.streaming:
            notes.append("End the stream to change the stream's.")
        if self.recording:
            notes.append("Stop the recording to change the recording's.")
        box.addWidget(_hint(" ".join(notes)))
        return group

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
        self.wavelink = None
        if any(is_mixer_app_device(n) for n in audio_devices.get("audio_output", [])):
            from sprigcast.audio.wavelink import enabled as wavelink_enabled

            self.wavelink = QCheckBox("Keep Wave Link in step")
            self.wavelink.setToolTip("Mute or change the volume of a Wave Link channel here or in Wave Link,\n"
                                     "and the other follows (Wave Link's Stream Mix).")
            self.wavelink.setChecked(wavelink_enabled())
            box.addWidget(self.wavelink)
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
        heads = ["Sound", "Volume", "Mono", "Balance", "Sync offset", "Who hears it",
                 *(str(n) for n in range(1, tracks + 1))]
        for col, text in enumerate(heads):
            head = QLabel(text)
            head.setStyleSheet(f"color: {theme.MUTED}")
            if col >= 6:
                head.setAlignment(Qt.AlignmentFlag.AlignCenter)
                head.setToolTip(f"Track {text}: {self.collection.audio.track_name(int(text))}")
            grid.addWidget(head, 0, col)
        for row, source in enumerate(sounds, 1):
            st = source.settings
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
            grid.addWidget(mono, row, 2, alignment=Qt.AlignmentFlag.AlignCenter)
            balance = QSlider(Qt.Orientation.Horizontal)
            balance.setRange(-100, 100)
            balance.setValue(round(float(st.get("balance", 0.0) or 0.0) * 100))
            balance.setFixedWidth(90)
            balance.setToolTip("Left / right (double-click the Sound card's slider to centre)")
            balance.valueChanged.connect(lambda v, b=balance: b.setToolTip(
                "Centre" if not v else f"{abs(v)}% {'left' if v < 0 else 'right'}"))
            grid.addWidget(balance, row, 3)
            sync = QSpinBox()
            sync.setRange(0, 2000)
            sync.setSingleStep(10)
            sync.setSuffix(" ms")
            sync.setValue(int(st.get("delay_ms", 0) or 0))
            sync.setFixedWidth(100)
            sync.setToolTip("Hold this sound back to line it up with a camera that runs late")
            grid.addWidget(sync, row, 4)
            hear = QComboBox()
            current = st.get("monitor", False)
            for text, value in self.HEAR:
                hear.addItem(text, value)
            hear.setCurrentIndex(2 if current == "only" else 1 if current else 0)
            hear.setEnabled(source.kind != "audio_output")  # desktop sound already plays on your speakers
            hear.setFixedWidth(118)
            grid.addWidget(hear, row, 5)
            ticks = []
            for n in range(1, tracks + 1):
                tick = QCheckBox()
                tick.setChecked(on_track(source, n))
                tick.setToolTip(f"{source.name} on {self.collection.audio.track_name(n)} (track {n})")
                grid.addWidget(tick, row, 5 + n, alignment=Qt.AlignmentFlag.AlignCenter)
                ticks.append(tick)
            self.track_ticks[source.id] = ticks
            self.sound_rows[source.id] = {"volume": volume, "mono": mono, "balance": balance, "sync": sync,
                                          "hear": hear}
            ticks[0].toggled.connect(lambda _on, sid=source.id: self._row_ticks(sid))
            hear.currentIndexChanged.connect(lambda _i, sid=source.id: self._row_ticks(sid))
            self._row_ticks(source.id)
        grid.setColumnStretch(0, 1)
        box.addLayout(grid)
        legend = " · ".join(f"{n} {self.collection.audio.track_name(n)}" for n in range(1, tracks + 1))
        box.addWidget(_hint(f"Tracks: {legend}"))
        return group

    def _row_ticks(self, source_id: str) -> None:
        """Just me: on no track. VOD-safe is a part of track 1, so it needs track 1."""
        ticks = self.track_ticks[source_id]
        only_me = self.sound_rows[source_id]["hear"].currentData() == "only"
        for n, tick in enumerate(ticks, 1):
            tick.setEnabled(not only_me and (n != 2 or ticks[0].isChecked()))

    # Saving ------------------------------------------------------------------------------

    def apply_to(self, collection: SceneCollection, recording: bool = False) -> None:
        audio = collection.audio
        if audio.global_sources is None:  # pin the global devices before anything else moves
            audio.global_sources = collection.global_device_ids()
        audio.global_devices = self.global_on.isChecked()
        if self.wavelink is not None:
            from PySide6.QtCore import QSettings

            from sprigcast.audio.wavelink import SETTING

            QSettings().setValue(SETTING, self.wavelink.isChecked())
        audio.track_names = [e.text().strip() or TRACK_NAMES[i] for i, e in enumerate(self.track_names)]
        if not self.streaming:
            collection.stream_encoder = collection.stream_encoder.model_copy(
                update={"audio_kbps": self.stream_kbps.currentData()})
        if not recording:
            collection.recording.encoder = collection.recording.encoder.model_copy(
                update={"audio_kbps": self.recording_kbps.currentData()})
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
