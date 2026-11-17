"""Settings → Output: how streams and recordings are encoded, laid out like OBS's Output tab."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QHBoxLayout,
    QPushButton,
    QRadioButton,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.core.scene import EncoderSettings, SceneCollection

RATE_CONTROLS = {"cbr": "Constant Bitrate (CBR)", "vbr": "Variable Bitrate (VBR)", "cqp": "Constant Quality (CQP)"}
PRESETS = {
    "p1": "P1: Fastest (Lowest Quality)",
    "p2": "P2: Faster (Lower Quality)",
    "p3": "P3: Fast (Low Quality)",
    "p4": "P4: Medium (Medium Quality)",
    "p5": "P5: Slow (Good Quality)",
    "p6": "P6: Slower (Better Quality)",
    "p7": "P7: Slowest (Best Quality)",
}
TUNINGS = {"hq": "High Quality", "ll": "Low Latency", "ull": "Ultra Low Latency"}
MULTIPASS = {"disabled": "Single Pass", "qres": "Two Passes (Quarter Resolution)", "fullres": "Two Passes (Full Resolution)"}
PROFILES = {"high": "high", "main": "main", "baseline": "baseline"}
AUDIO_KBPS = [96, 128, 160, 192, 256, 320]


def _combo(choices: dict[str, str], value: str) -> QComboBox:
    combo = QComboBox()
    for key, label in choices.items():
        combo.addItem(label, key)
    combo.setCurrentIndex(max(0, combo.findData(value)))
    return combo


CLIP_LENGTHS = (15, 30, 45, 60)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.MUTED}")
    return label


X264_PRESETS = {
    "ultrafast": "ultrafast (lightest on the CPU)", "superfast": "superfast", "veryfast": "veryfast (recommended)",
    "faster": "faster", "fast": "fast", "medium": "medium", "slow": "slow (heaviest, best quality)",
}
X264_TUNES = {"": "None", "film": "Film", "animation": "Animation", "grain": "Grain",
              "stillimage": "Still image", "zerolatency": "Zero latency"}


class EncoderForm(QGroupBox):
    """The encoder settings card: every option shows, and only the rows the chosen encoder uses.

    values() returns a new EncoderSettings.
    """

    def __init__(self, title: str, settings: EncoderSettings, encoder_name: str = "", streaming: bool = False,
                 parent: QWidget | None = None):
        super().__init__(title, parent)
        from sprigcast.output.recorder import codec_label, encoder_choices, pick_codec

        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(10)
        self._form = form
        self.encoder = QComboBox()
        best = pick_codec()
        self.encoder.addItem(f"Best on this PC ({codec_label(best)})", "auto")
        for codec in encoder_choices(streaming):
            self.encoder.addItem(codec_label(codec), codec)
        if self.encoder.findData(settings.encoder) < 0:
            self.encoder.addItem(f"{codec_label(settings.encoder)} (not on this PC: uses the best one)", settings.encoder)
        self.encoder.setCurrentIndex(self.encoder.findData(settings.encoder))
        self._best = best
        form.addRow("Video encoder", self.encoder)
        self.rate_control = _combo(RATE_CONTROLS, settings.rate_control)
        form.addRow("Rate control", self.rate_control)
        self.cq_level = QSpinBox()
        self.cq_level.setRange(1, 51)
        self.cq_level.setValue(settings.cq_level)
        self.cq_level.setToolTip("Lower is better quality and bigger files. 18 to 23 suits most recordings.")
        form.addRow("Quality level", self.cq_level)
        self.keyint = QSpinBox()
        self.keyint.setRange(0, 10)
        self.keyint.setSuffix(" s")
        self.keyint.setSpecialValueText("Auto (2 s)")
        self.keyint.setValue(settings.keyint_sec)
        form.addRow("Keyframe interval", self.keyint)
        self.preset = _combo(PRESETS, settings.preset)
        form.addRow("Preset", self.preset)
        self.x264_preset = _combo(X264_PRESETS, settings.x264_preset)
        self.x264_preset.setToolTip("How hard the CPU works on each frame. Slower presets look better but "
                                    "take CPU time away from your game.")
        form.addRow("CPU usage preset", self.x264_preset)
        self.tuning = _combo(TUNINGS, settings.tuning)
        form.addRow("Tuning", self.tuning)
        self.x264_tune = _combo(X264_TUNES, settings.x264_tune)
        form.addRow("Tune", self.x264_tune)
        self.multipass = _combo(MULTIPASS, settings.multipass)
        form.addRow("Multipass mode", self.multipass)
        self.profile = _combo(PROFILES, settings.profile)
        form.addRow("Profile", self.profile)
        self.b_frames = QSpinBox()
        self.b_frames.setRange(0, 4)
        self.b_frames.setValue(settings.b_frames)
        self.b_frames.setToolTip("0 keeps SprigCast's lighter NVENC path, which uses less memory.")
        form.addRow("B-frames", self.b_frames)
        self.lookahead = QCheckBox("Look-ahead")
        self.lookahead.setToolTip("Lets the encoder look a few frames ahead to spend bits where motion starts. "
                                  "Uses more GPU time.")
        self.lookahead.setChecked(settings.lookahead)
        form.addRow("", self.lookahead)
        self.adaptive_quant = QCheckBox("Adaptive quantization")
        self.adaptive_quant.setToolTip("Spends more bits on flat areas (skies, walls) where blockiness shows most.")
        self.adaptive_quant.setChecked(settings.adaptive_quant)
        form.addRow("", self.adaptive_quant)
        self.audio = QComboBox()
        for kbps in sorted({*AUDIO_KBPS, settings.audio_kbps}):
            self.audio.addItem(f"{kbps} kbps", kbps)
        self.audio.setCurrentIndex(self.audio.findData(settings.audio_kbps))
        form.addRow("Audio bitrate", self.audio)
        form.setRowVisible(self.audio, False)  # set in Settings → Audio → Bitrate; kept so values() carries it
        self.note = _hint("")
        form.addRow("", self.note)
        self.rate_control.currentIndexChanged.connect(self._update_rows)
        self.encoder.currentIndexChanged.connect(self._update_rows)
        self._update_rows()

    def codec(self) -> str:
        chosen = self.encoder.currentData()
        return self._best if chosen == "auto" else chosen

    def _update_rows(self) -> None:
        """Show the rows the chosen encoder reads; the rest would do nothing."""
        from sprigcast.output.recorder import family

        fam = family(self.codec())
        gpu = fam != "x264"
        rows = {
            self.cq_level: self.rate_control.currentData() == "cqp",
            self.preset: gpu and fam != "mf",
            self.tuning: fam in ("nvenc", "amf", "qsv"),
            self.multipass: fam == "nvenc",
            self.profile: self.codec() in ("h264_nvenc", "h264_amf", "h264_qsv", "libx264"),
            self.b_frames: fam == "nvenc",
            self.lookahead: fam in ("nvenc", "qsv"),
            self.adaptive_quant: fam in ("nvenc", "amf", "x264"),
            self.x264_preset: not gpu,
            self.x264_tune: not gpu,
        }
        for widget, visible in rows.items():
            self._form.setRowVisible(widget, visible)
        notes = {
            "x264": "x264 encodes on the CPU. It looks great but competes with your game for CPU time.",
            "mf": "Windows Media Foundation has few options of its own. Use NVENC, AMF or Quick Sync if you have them.",
        }
        if self.codec().startswith(("hevc", "av1")):
            text = ("HEVC and AV1 make smaller files at the same quality. Most editors open them; some older "
                    "players and phones can't play AV1.")
        else:
            text = notes.get(fam, "")
        self.note.setText(text)
        self._form.setRowVisible(self.note, bool(text))

    def values(self) -> EncoderSettings:
        return EncoderSettings(
            encoder=self.encoder.currentData(),
            x264_preset=self.x264_preset.currentData(),
            x264_tune=self.x264_tune.currentData(),
            rate_control=self.rate_control.currentData(),
            cq_level=self.cq_level.value(),
            keyint_sec=self.keyint.value(),
            preset=self.preset.currentData(),
            tuning=self.tuning.currentData(),
            multipass=self.multipass.currentData(),
            profile=self.profile.currentData(),
            lookahead=self.lookahead.isChecked(),
            adaptive_quant=self.adaptive_quant.isChecked(),
            b_frames=self.b_frames.value(),
            audio_kbps=self.audio.currentData(),
        )


class SpaceBox(QGroupBox):
    """Space: how big the recordings folder is, how much the drive has left, and how many screenshots and
    clips to keep. The size is counted off the UI thread (a folder of long recordings can be big)."""

    def __init__(self, collection: SceneCollection, folder_edit=None, parent: QWidget | None = None):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QFormLayout

        super().__init__("Space", parent)
        self._folder_edit = folder_edit
        self._fallback = collection.recording.folder
        form = QFormLayout(self)
        form.setVerticalSpacing(12)
        self.usage = QLabel("Counting…")
        self.usage.setWordWrap(True)
        form.addRow("Recordings", self.usage)
        rec = collection.recording
        self.keep_screenshots = self._keep_box(rec.keep_screenshots, "screenshots")
        form.addRow("Screenshots", self.keep_screenshots)
        self.keep_clips = self._keep_box(rec.keep_clips, "clips")
        form.addRow("Clips", self.keep_clips)
        form.addRow(_hint("Screenshots and clips go in a folder for each day. With a limit, the oldest are "
                          "deleted as new ones arrive. Recordings are never deleted."))
        self._result: list = []
        self._poll = QTimer(self, interval=200)
        self._poll.timeout.connect(self._show)
        self.count()
        if folder_edit is not None:
            folder_edit.editingFinished.connect(self.count)

    @staticmethod
    def _keep_box(value: int, what: str) -> QSpinBox:
        box = QSpinBox()
        box.setRange(0, 10000)
        box.setSingleStep(10)
        box.setSpecialValueText("Keep all")
        box.setPrefix("Keep the last ")
        box.setSuffix(f" {what}")
        box.setValue(max(0, value))
        box.setFixedWidth(260)
        return box

    def folder(self):
        from pathlib import Path

        text = self._folder_edit.text().strip() if self._folder_edit is not None else self._fallback
        return Path(text) if text else None

    def count(self) -> None:
        from sprigcast.output import storage

        folder = self.folder()
        if folder is None:
            self.usage.setText("Pick a folder above.")
            return
        self.usage.setText("Counting…")
        result: list = []
        self._result = result

        def work():
            used = storage.folder_size(folder) if folder.is_dir() else 0
            result.append((used, storage.free_bytes(folder)))

        from sprigcast.core import workers

        workers.run(work, name="folder-size")
        self._poll.start()

    def _show(self) -> None:
        from sprigcast.output import storage

        if not self._result:
            return
        self._poll.stop()
        used, free = self._result[0]
        text = f"{storage.size_text(used)} in this folder"
        if free is not None:
            text += f"  ·  {storage.size_text(free)} free on the drive"
        self.usage.setText(text)

    def apply_to(self, collection: SceneCollection) -> None:
        collection.recording.keep_screenshots = self.keep_screenshots.value()
        collection.recording.keep_clips = self.keep_clips.value()


class OutputPage(QWidget):
    """Streaming and Recording sub-tabs. The Recording sub-tab holds the RecordingPage passed in."""

    def __init__(self, collection: SceneCollection, encoder_name: str, recording_page: QWidget,
                 streaming: bool = False, recording: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.banner = QLabel("Twitch Enhanced Broadcasting controls some of these settings")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(theme.NOTE_STYLE + " padding: 10px;")
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner.setToolTip("Twitch picks the resolutions, bitrates, keyframes and B-frames of the Twitch "
                               "output. The settings below still apply to your other streams.")
        self.banner.setVisible(enhanced_in_use(collection))
        root.addWidget(self.banner)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        stream_tab = QWidget()
        box = QVBoxLayout(stream_tab)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(14)
        if streaming:
            box.addWidget(_hint("You're live. End the stream to change these."))
        self.stream_form = EncoderForm("Encoder", collection.stream_encoder, encoder_name, streaming=True)
        self.stream_form.setEnabled(not streaming)
        box.addWidget(self.stream_form)
        box.addWidget(_hint("Bitrate and size are set for each place you stream to (Settings → Stream). "
                            "Most sites want CBR and 2 s keyframes."))
        box.addStretch(1)
        self.tabs.addTab(stream_tab, "Streaming")

        record_tab = QWidget()
        box = QVBoxLayout(record_tab)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(14)
        if recording:
            box.addWidget(_hint("Recording now. Stop the recording to change these."))
        recording_group = QGroupBox("Recording")
        inner = QVBoxLayout(recording_group)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addWidget(recording_page)
        inner.addWidget(_hint("Which sound tracks a recording keeps (everything, VOD-safe, voice...) is in Audio."))
        recording_group.setEnabled(not recording)
        box.addWidget(recording_group)
        from sprigcast.app.source_record_box import SourceRecordBox

        self.source_record = SourceRecordBox(collection)
        self.source_record.setEnabled(not recording)
        box.addWidget(self.source_record)
        self.record_form = EncoderForm("Encoder", collection.recording.encoder, encoder_name)
        self.record_form.setEnabled(not recording)
        box.addWidget(self.record_form)
        self.space = SpaceBox(collection, getattr(recording_page, "folder", None))
        box.addWidget(self.space)
        box.addStretch(1)
        self.tabs.addTab(record_tab, "Recording")

        clips_tab = QWidget()
        box = QVBoxLayout(clips_tab)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(14)
        box.addWidget(self._clips_group(collection))
        box.addWidget(_hint("Clips use the Recording encoder and bitrate, so a clip looks exactly like a recording."))
        box.addStretch(1)
        self.tabs.addTab(clips_tab, "Clips (replay buffer)")
        self.camera_tab = CameraTab(collection)
        self.tabs.addTab(self.camera_tab, "Virtual camera")
        self.streaming, self.recording = streaming, recording

    def show_tab(self, name: str) -> None:
        self.tabs.setCurrentIndex({"Recording": 1, "Clips": 2, "Replay": 2, "Camera": 3}.get(name, 0))

    def apply_to(self, collection: SceneCollection) -> None:
        if not self.streaming:
            collection.stream_encoder = self.stream_form.values()
        if not self.recording:
            collection.recording.encoder = self.record_form.values()
            self.source_record.apply_to(collection)
        collection.recording.replay_seconds = self.clip_length.checkedId()
        collection.recording.clip_shapes = self.clip_shapes.checkedButton().property("shape")
        collection.recording.clips_on_start = self.clips_on_start.isChecked()
        collection.recording.clips_idle_minutes = int(self.clips_idle.currentData())
        self.space.apply_to(collection)
        self.camera_tab.apply()

    def _clips_group(self, collection: SceneCollection) -> QGroupBox:
        """Clips: how long, which shapes, and whether they're ready from the start."""
        group = QGroupBox("Clips")
        form = QFormLayout(group)
        form.setVerticalSpacing(12)
        form.addRow(_hint("Clip saves the last few seconds as a video, so a great moment is never missed. "
                          "SprigCast keeps those seconds in memory (about 1 MB a second for each shape)."))
        lengths = QHBoxLayout()
        lengths.setSpacing(6)
        self.clip_length = QButtonGroup(group)
        current = min(CLIP_LENGTHS, key=lambda n: abs(n - collection.recording.replay_seconds))
        for seconds in CLIP_LENGTHS:
            pill = QPushButton(f"{seconds} s")
            pill.setCheckable(True)
            pill.setChecked(seconds == current)
            pill.setStyleSheet(theme.SMALL_PILL_STYLE)
            pill.setCursor(Qt.CursorShape.PointingHandCursor)
            self.clip_length.addButton(pill, seconds)
            lengths.addWidget(pill)
        lengths.addStretch(1)
        form.addRow("Length", lengths)
        shapes = QVBoxLayout()
        shapes.setSpacing(6)
        self.clip_shapes = QButtonGroup(group)
        for key, label, note in (
            ("both", "Both shapes", "two files from the same moment, one wide and one tall"),
            ("horizontal", "Horizontal", "for YouTube and highlights"),
            ("vertical", "Vertical", "for TikTok, Shorts and Reels"),
        ):
            choice = QRadioButton(f"{label}  ·  {note}")
            choice.setProperty("shape", key)
            choice.setChecked(collection.recording.clip_shapes == key)
            self.clip_shapes.addButton(choice)
            shapes.addWidget(choice)
        form.addRow("Shapes", shapes)
        self.clips_on_start = QCheckBox("Get clips ready when SprigCast opens")
        self.clips_on_start.setToolTip("Otherwise the first press of Clips on Live gets them ready")
        self.clips_on_start.setChecked(collection.recording.clips_on_start)
        form.addRow("", self.clips_on_start)
        self.clips_idle = QComboBox()
        for minutes, label in ((5, "After 5 minutes"), (15, "After 15 minutes"), (30, "After 30 minutes"), (0, "Never")):
            self.clips_idle.addItem(label, minutes)
        idle = collection.recording.clips_idle_minutes
        self.clips_idle.setCurrentIndex(max(0, self.clips_idle.findData(idle if idle in (0, 5, 15, 30) else 15)))
        self.clips_idle.setToolTip("While nothing is streamed or recorded and no clip is saved, clips pause to\n"
                                   "save memory and the graphics card. They come back when you go live or record.")
        form.addRow("Pause when unused", self.clips_idle)
        return group


def enhanced_in_use(collection: SceneCollection) -> bool:
    return any(o.type == "stream" and o.enabled and o.service == "twitch" and o.enhanced for o in collection.outputs)


CAMERA_ON = "camera/on"  # QSettings: show SprigCast as a camera while it runs
CAMERA_SHOWS = "camera/shows"  # "auto", "canvas:<id>", "bloom:<scene id>" or "source:<source id>"
CAMERA_SOUND = "camera/sound_to"  # a playback device that gets the stream mix while an app watches, or ""
VIDEO_KINDS_SKIP = ("audio_input", "audio_output", "app_audio", "scene")


def camera_choice() -> str:
    from PySide6.QtCore import QSettings

    return QSettings().value(CAMERA_SHOWS, "auto", type=str) or "auto"


def camera_sound() -> str:
    from PySide6.QtCore import QSettings

    return QSettings().value(CAMERA_SOUND, "", type=str) or ""


def camera_wanted() -> bool:
    from PySide6.QtCore import QSettings

    return QSettings().value(CAMERA_ON, False, type=bool)


class CameraTab(QWidget):
    """Virtual camera: set it up once (administrator prompt), then switch it on or off."""

    def __init__(self, collection: SceneCollection | None = None, parent: QWidget | None = None):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QPushButton

        from sprigcast.output import virtualcam

        super().__init__(parent)
        self.vc = virtualcam
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(14)
        group = QGroupBox("SprigCast Camera")
        inner = QVBoxLayout(group)
        inner.setSpacing(12)
        self.on = QCheckBox("Show SprigCast as a camera")
        self.on.setChecked(camera_wanted())
        inner.addWidget(self.on)
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        self.shows = QComboBox()
        self.shows.addItem("Matches the app (horizontal or vertical canvas)", "auto")
        for canvas in (collection.canvases if collection else []):
            self.shows.addItem(f"{canvas.id.capitalize()} canvas ({canvas.width}\u00d7{canvas.height})",
                               f"canvas:{canvas.id}")
        if collection is not None:
            for scene in collection.scenes:
                if scene.kind == "show" and not scene.shape_of:
                    self.shows.addItem(f"Bloom: {scene.name} ({scene.canvas})", f"bloom:{scene.id}")
            for source in collection.sources:
                if source.kind not in VIDEO_KINDS_SKIP:
                    self.shows.addItem(f"Source: {source.name}", f"source:{source.id}")
        current = self.shows.findData(camera_choice())
        self.shows.setCurrentIndex(max(0, current))
        form.addRow("Shows", self.shows)
        self.sound = QComboBox()
        self.sound.addItem("Not sent", "")
        from sprigcast.sources.audio import device_label, list_playback_devices

        chosen = camera_sound()
        names = list_playback_devices()
        if chosen and chosen not in names:
            names.append(chosen)  # not plugged in right now: still shown
        for name in names:
            self.sound.addItem(device_label(name), name)
        self.sound.setCurrentIndex(max(0, self.sound.findData(chosen)))
        form.addRow("Sound to", self.sound)
        inner.addLayout(form)
        inner.addWidget(_hint("TikTok LIVE Studio, Zoom, Discord, OBS and browsers list it as "
                              "\u201cSprigCast Camera\u201d. If the app wants another shape than what it shows, "
                              "you get black bars instead of a stretched picture."))
        inner.addWidget(_hint("For TikTok LIVE Studio, send the sound to a virtual cable (like CABLE Input) and "
                              "pick its other end (CABLE Output) as the mic there. It plays only while an app "
                              "has the camera open."))
        self.status = QLabel()
        self.status.setWordWrap(True)
        inner.addWidget(self.status)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.setup = QPushButton("Set up the camera")
        self.setup.setStyleSheet(theme.PILL_STYLE)
        self.setup.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setup.clicked.connect(self._set_up)
        row.addWidget(self.setup)
        self.remove = QPushButton("Remove")
        self.remove.setStyleSheet(theme.PILL_STYLE)
        self.remove.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove.clicked.connect(self._remove)
        row.addWidget(self.remove)
        self.privacy = QPushButton("Open settings")
        self.privacy.setToolTip("Windows Settings → Privacy & security → Camera")
        self.privacy.setStyleSheet(theme.PILL_STYLE)
        self.privacy.setCursor(Qt.CursorShape.PointingHandCursor)
        self.privacy.clicked.connect(lambda: self.vc.open_privacy_settings())
        row.addWidget(self.privacy)
        row.addStretch(1)
        inner.addLayout(row)
        inner.addWidget(_hint("Setting up copies the camera into Program Files and registers it with Windows. "
                              "Windows asks for permission once. It uses no memory or CPU until an app opens it."))
        box.addWidget(group)
        box.addStretch(1)
        self._timer = QTimer(self, interval=1500)  # after the admin prompt: notice when setup finished
        self._timer.timeout.connect(self._tick)
        self._ticks = 0
        self.refresh()

    def _tick(self) -> None:
        self._ticks += 1
        before = self.remove.isVisible()
        self.refresh()
        if self.remove.isVisible() != before or self._ticks > 40:  # finished, or the prompt was declined
            self._timer.stop()

    def refresh(self) -> None:
        ok, why = self.vc.supported()
        installed = self.vc.installed()
        if not ok:
            text = why
        elif not installed:
            text = "Not set up yet."
        elif self.vc.needs_update():
            text = "Set up, but this SprigCast has a newer camera. Press Update."
        else:
            text = "Set up. It turns on when you press Apply." if self.on.isChecked() else "Set up."
        blocked = self.vc.privacy_block() if ok else ""
        if blocked:
            text = f"{blocked} Windows won't add SprigCast Camera until you turn it on."
        elif installed and self.vc.state.privacy_failed:
            text = "Windows blocked the camera. Its camera settings may say why."
        self.status.setText(text)
        self.privacy.setVisible(bool(blocked) or self.vc.state.privacy_failed)
        self.status.setStyleSheet(f"color: {theme.MUTED if installed or not ok else theme.TEXT}")
        self.setup.setVisible(ok and (not installed or self.vc.needs_update()))
        self.setup.setText("Update the camera" if installed else "Set up the camera")
        self.remove.setVisible(ok and installed)
        self.on.setEnabled(ok)

    def _set_up(self) -> None:
        if self.vc.set_up():
            self.status.setText("Waiting for Windows… say Yes on its prompt.")
            self._ticks = 0
            self._timer.start()

    def _remove(self) -> None:
        self.on.setChecked(False)
        if self.vc.remove():
            self._ticks = 0
            self._timer.start()

    def apply(self) -> None:
        from PySide6.QtCore import QSettings

        QSettings().setValue(CAMERA_ON, self.on.isChecked())
        QSettings().setValue(CAMERA_SHOWS, self.shows.currentData())
        QSettings().setValue(CAMERA_SOUND, self.sound.currentData() or "")
