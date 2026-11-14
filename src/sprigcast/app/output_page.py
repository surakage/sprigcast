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
        self.record_form = EncoderForm("Encoder", collection.recording.encoder, encoder_name)
        self.record_form.setEnabled(not recording)
        box.addWidget(self.record_form)
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
        self.streaming, self.recording = streaming, recording

    def show_tab(self, name: str) -> None:
        self.tabs.setCurrentIndex({"Recording": 1, "Clips": 2, "Replay": 2}.get(name, 0))

    def apply_to(self, collection: SceneCollection) -> None:
        if not self.streaming:
            collection.stream_encoder = self.stream_form.values()
        if not self.recording:
            collection.recording.encoder = self.record_form.values()
        collection.recording.replay_seconds = self.clip_length.checkedId()
        collection.recording.clip_shapes = self.clip_shapes.checkedButton().property("shape")
        collection.recording.clips_on_start = self.clips_on_start.isChecked()

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
            pill.setStyleSheet(theme.SMALL_PILL_STYLE + f"QPushButton:checked {{ background: {theme.SELECT_BG};"
                               f" color: {theme.SELECT_TEXT}; }}")
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
        return group


def enhanced_in_use(collection: SceneCollection) -> bool:
    return any(o.type == "stream" and o.enabled and o.service == "twitch" and o.enhanced for o in collection.outputs)
