"""Settings → Audio: global devices, sound tracks 1-5 and which sounds go on each.

- Global devices: the Desktop audio and Mic every setup starts with. Switched off, they are not opened
  at all and leave the Sound list, so only the mics and desktop sounds you add yourself play.
- Tracks: a recording can keep up to five sound tracks. 1 is everything, 2 is VOD-safe (everything
  but the sounds you keep out of it, music by default), 3 is your voice, 4 and 5 are yours to fill.
- Which sounds go where: a tick grid, one row per sound, one column per track.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.audio.mixer import is_monitor_only, source_tracks
from sprigcast.core.scene import AUDIO_DEVICE_KINDS, AUDIO_TRACKS, TRACK_NAMES, SceneCollection, SourceConfig

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


def device_label(kind: str) -> str:
    return "Desktop audio" if kind == "audio_output" else "Mic"


class AudioPage(QWidget):
    def __init__(self, collection: SceneCollection, audio_devices: dict[str, list[str]], recording: bool = False):
        super().__init__()
        self.collection = collection
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        self.device_combos: dict[str, QComboBox] = {}
        layout.addWidget(self._devices(audio_devices))
        layout.addWidget(self._tracks(recording))
        layout.addWidget(self._grid())
        layout.addStretch(1)

    # Devices -----------------------------------------------------------------------------

    def _combo(self, source: SourceConfig, names: list[str]) -> QComboBox:
        combo = QComboBox()
        combo.addItem("Default (follows Windows)", "default")
        for name in names:
            combo.addItem(name, name)
        current = str(source.settings.get("device", "default"))
        if combo.findData(current) < 0:
            combo.addItem(f"{current} (not connected)", current)
        combo.setCurrentIndex(combo.findData(current))
        self.device_combos[source.id] = combo
        return combo

    def _devices(self, audio_devices: dict[str, list[str]]) -> QWidget:
        group = QGroupBox("Devices")
        box = QVBoxLayout(group)
        box.setContentsMargins(18, 30, 18, 18)
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
        group = QGroupBox("Sound tracks")
        box = QVBoxLayout(group)
        box.setContentsMargins(18, 30, 18, 18)
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
            grid.addWidget(name, n, 1)
            keep = QCheckBox()
            keep.setChecked(n in kept or n == 1)
            keep.setEnabled(n != 1 and not recording)  # track 1 is always there: it's what players play
            keep.setToolTip(TRACK_TIPS[n - 1] if n != 1 else "Always kept: players play the first track")
            grid.addWidget(keep, n, 2, alignment=Qt.AlignmentFlag.AlignCenter)
            self.track_names.append(name)
            self.track_kept.append(keep)
        grid.setColumnStretch(1, 1)
        box.addLayout(grid)
        box.addWidget(_hint("Track 2 leaves out music, so you can upload it without copyright strikes. "
                            "Pick which sounds go on each track below."
                            + (" Stop the recording to change which tracks it keeps." if recording else "")))
        return group

    # Which sounds go where ---------------------------------------------------------------

    def _grid(self) -> QWidget:
        group = QGroupBox("Which sounds go where")
        box = QVBoxLayout(group)
        box.setContentsMargins(18, 30, 18, 18)
        box.setSpacing(12)
        sounds = [s for s in self.collection.sources if has_sound(s) and not self.collection.device_off(s)]
        self.track_ticks: dict[str, list[QCheckBox]] = {}
        if not sounds:
            box.addWidget(_hint("No sounds yet."))
            return group
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        for n in range(1, len(AUDIO_TRACKS) + 1):
            head = QLabel(str(n))
            head.setAlignment(Qt.AlignmentFlag.AlignCenter)
            head.setToolTip(self.collection.audio.track_name(n))
            head.setStyleSheet(f"color: {theme.MUTED}")
            grid.addWidget(head, 0, n)
        for row, source in enumerate(sounds, 1):
            name = QLabel(source.name)
            grid.addWidget(name, row, 0)
            ticks = []
            only_me = is_monitor_only(source)
            for n in range(1, len(AUDIO_TRACKS) + 1):
                tick = QCheckBox()
                tick.setChecked(on_track(source, n))
                tick.setEnabled(not only_me and (n != 2 or on_track(source, 1)))
                tick.setToolTip("Only on your speakers (Sound → Who hears it)" if only_me
                                else f"{source.name} on track {n}")
                grid.addWidget(tick, row, n, alignment=Qt.AlignmentFlag.AlignCenter)
                ticks.append(tick)
            ticks[0].toggled.connect(lambda on, t=ticks[1]: t.setEnabled(on))  # VOD-safe is a part of track 1
            self.track_ticks[source.id] = ticks
        grid.setColumnStretch(0, 1)
        box.addLayout(grid)
        return group

    # Saving ------------------------------------------------------------------------------

    def apply_to(self, collection: SceneCollection, recording: bool = False) -> None:
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
            ticks = self.track_ticks.get(source.id)
            if ticks is not None and not is_monitor_only(source):
                for n, tick in enumerate(ticks, 1):
                    put_on_track(source, n, tick.isChecked())
