"""The Ducking window: when you talk, the other sounds glide down so your voice stays clear."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from sprigcast.core.scene import AudioSettings, SceneCollection

AUDIO_KINDS = ("audio_input", "audio_output")


def is_voice(settings: dict, kind: str) -> bool:
    return bool(settings.get("duck_voice", kind == "audio_input"))


def _slider(low: int, high: int, value: float, fmt: str) -> tuple[QWidget, QSlider]:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(low, high)
    slider.setValue(int(round(value)))
    label = QLabel(fmt.format(slider.value()))
    label.setMinimumWidth(64)
    slider.valueChanged.connect(lambda v: label.setText(fmt.format(v)))
    layout.addWidget(slider, 1)
    layout.addWidget(label)
    return row, slider


class DuckingDialog(QDialog):
    def __init__(self, collection: SceneCollection, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Ducking")
        audio = collection.audio
        self.enabled = QCheckBox("Lower other sounds while I talk")
        self.enabled.setChecked(audio.ducking)
        intro = QLabel("Your voice stays clear; game and music dip, then come back when you stop.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #8d8a83")

        voices = QGroupBox("Voices (these do the lowering)")
        voice_box = QVBoxLayout(voices)
        self.voice_boxes: dict[str, QCheckBox] = {}
        sources = [s for s in collection.sources if s.kind in AUDIO_KINDS]
        for source in sources:
            box = QCheckBox(source.name)
            box.setChecked(is_voice(source.settings, source.kind))
            self.voice_boxes[source.id] = box
            voice_box.addWidget(box)
        if not sources:
            voice_box.addWidget(QLabel("No audio sources yet."))

        sound = QGroupBox("How it sounds")
        form = QFormLayout(sound)
        row, self.amount = _slider(3, 30, -audio.duck_amount_db, "−{} dB")
        form.addRow("Lower others by", row)
        row, self.threshold = _slider(-60, -10, audio.duck_threshold_db, "{} dB")
        self.threshold.setToolTip("Lower = reacts to quieter speech. Raise it if background noise sets it off.")
        form.addRow("Talk sensitivity", row)
        row, self.attack = _slider(50, 1000, audio.duck_attack_ms, "{} ms")
        form.addRow("Fade down", row)
        row, self.release = _slider(200, 3000, audio.duck_release_ms, "{} ms")
        form.addRow("Come back", row)
        row, self.hold = _slider(0, 2000, audio.duck_hold_ms, "{} ms")
        self.hold.setToolTip("How long to wait after you stop talking, so it doesn't bounce between words")
        form.addRow("Wait before", row)

        def toggle(on: bool) -> None:
            voices.setEnabled(on)
            sound.setEnabled(on)

        self.enabled.toggled.connect(toggle)
        toggle(audio.ducking)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.addWidget(self.enabled)
        root.addWidget(intro)
        root.addWidget(voices)
        root.addWidget(sound)
        root.addWidget(buttons)
        self.resize(440, self.sizeHint().height())

    def settings(self) -> AudioSettings:
        return AudioSettings(
            ducking=self.enabled.isChecked(),
            duck_amount_db=-float(self.amount.value()),
            duck_threshold_db=float(self.threshold.value()),
            duck_attack_ms=float(self.attack.value()),
            duck_release_ms=float(self.release.value()),
            duck_hold_ms=float(self.hold.value()),
        )

    def apply_to(self, collection: SceneCollection) -> None:
        collection.audio = self.settings()
        for source in collection.sources:
            box = self.voice_boxes.get(source.id)
            if box is not None:
                source.settings = {**source.settings, "duck_voice": box.isChecked()}
