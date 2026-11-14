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


class EncoderForm(QGroupBox):
    """The encoder settings group. values() returns a new EncoderSettings."""

    def __init__(self, title: str, settings: EncoderSettings, encoder_name: str, parent: QWidget | None = None):
        super().__init__(title, parent)
        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("Video encoder", QLabel(encoder_name))
        self.rate_control = _combo(RATE_CONTROLS, settings.rate_control)
        form.addRow("Rate control", self.rate_control)
        self.cq_level = QSpinBox()
        self.cq_level.setRange(1, 51)
        self.cq_level.setValue(settings.cq_level)
        self.cq_label = QLabel("CQ level")
        form.addRow(self.cq_label, self.cq_level)
        self.keyint = QSpinBox()
        self.keyint.setRange(0, 10)
        self.keyint.setSuffix(" s")
        self.keyint.setSpecialValueText("Auto (2 s)")
        self.keyint.setValue(settings.keyint_sec)
        form.addRow("Keyframe interval", self.keyint)
        self.preset = _combo(PRESETS, settings.preset)
        form.addRow("Preset", self.preset)
        self.tuning = _combo(TUNINGS, settings.tuning)
        form.addRow("Tuning", self.tuning)
        self.multipass = _combo(MULTIPASS, settings.multipass)
        form.addRow("Multipass mode", self.multipass)
        self.profile = _combo(PROFILES, settings.profile)
        form.addRow("Profile", self.profile)
        self.lookahead = QCheckBox("Look-ahead")
        self.lookahead.setToolTip("Lets the encoder look a few frames ahead to spend bits where motion starts. "
                                  "Uses more GPU time.")
        self.lookahead.setChecked(settings.lookahead)
        form.addRow("", self.lookahead)
        self.adaptive_quant = QCheckBox("Adaptive quantization")
        self.adaptive_quant.setToolTip("Spends more bits on flat areas (skies, walls) where blockiness shows most.")
        self.adaptive_quant.setChecked(settings.adaptive_quant)
        form.addRow("", self.adaptive_quant)
        self.b_frames = QSpinBox()
        self.b_frames.setRange(0, 4)
        self.b_frames.setValue(settings.b_frames)
        form.addRow("B-frames", self.b_frames)
        self.audio = QComboBox()
        for kbps in sorted({*AUDIO_KBPS, settings.audio_kbps}):
            self.audio.addItem(f"{kbps} kbps", kbps)
        self.audio.setCurrentIndex(self.audio.findData(settings.audio_kbps))
        form.addRow("Audio bitrate", self.audio)
        if "x264" in encoder_name.lower():
            form.addRow("", _hint("No graphics-card encoder found: using x264 (CPU). "
                                  "Multipass and look-ahead are ignored."))
        elif "nvenc" not in encoder_name.lower():
            form.addRow("", _hint("Multipass is NVIDIA-only and is ignored with this encoder."))
        self.rate_control.currentIndexChanged.connect(self._rate_changed)
        self._rate_changed()

    def _rate_changed(self) -> None:
        cqp = self.rate_control.currentData() == "cqp"
        self.cq_level.setVisible(cqp)
        self.cq_label.setVisible(cqp)

    def values(self) -> EncoderSettings:
        return EncoderSettings(
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
        if streaming:
            box.addWidget(_hint("You're live. End the stream to change these."))
        self.stream_form = EncoderForm("Encoder settings", collection.stream_encoder, encoder_name)
        self.stream_form.setEnabled(not streaming)
        box.addWidget(self.stream_form)
        box.addWidget(_hint("Bitrate and size are set per destination (Settings → Stream). Most sites want CBR and 2 s keyframes."))
        box.addStretch(1)
        self.tabs.addTab(stream_tab, "Streaming")

        record_tab = QWidget()
        box = QVBoxLayout(record_tab)
        if recording:
            box.addWidget(_hint("Recording now. Stop the recording to change these."))
        recording_group = QGroupBox("Recording settings")
        inner = QVBoxLayout(recording_group)
        inner.addWidget(recording_page)
        recording_group.setEnabled(not recording)
        box.addWidget(recording_group)
        self.record_form = EncoderForm("Encoder settings", collection.recording.encoder, encoder_name)
        self.record_form.setEnabled(not recording)
        box.addWidget(self.record_form)
        box.addWidget(self._clips_group(collection))
        box.addStretch(1)
        self.tabs.addTab(record_tab, "Recording")
        self.streaming, self.recording = streaming, recording

    def show_tab(self, name: str) -> None:
        self.tabs.setCurrentIndex(1 if name == "Recording" else 0)

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
