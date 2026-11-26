"""The Ducking window: when you talk, the other sounds glide down so your voice stays clear."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QSlider,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.card import CardDialog
from sprigcast.app.card import section as card_section
from sprigcast.app.labels import literal
from sprigcast.core.scene import AudioSettings, SceneCollection

AUDIO_KINDS = ("audio_input", "audio_output", "app_audio", "media")


def is_voice(settings: dict, kind: str) -> bool:
    return bool(settings.get("duck_voice", kind == "audio_input" or (kind == "browser" and bool(settings.get("phone")))))


def _slider(low: int, high: int, value: float, fmt: str, name: str = "") -> tuple[QWidget, QSlider]:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(low, high)
    slider.setValue(int(round(value)))
    slider.setAccessibleName(name)
    label = QLabel(fmt.format(slider.value()))
    label.setMinimumWidth(64)
    slider.valueChanged.connect(lambda v: label.setText(fmt.format(v)))
    layout.addWidget(slider, 1)
    layout.addWidget(label)
    return row, slider


class DuckingDialog(CardDialog):
    def __init__(self, collection: SceneCollection, parent: QWidget | None = None):
        super().__init__("Ducking", parent, width=500)
        from sprigcast.app.game_dialog import _row
        from sprigcast.app.live_stage import Switch

        audio = collection.audio
        self.enabled = Switch()
        self.enabled.setChecked(audio.ducking)
        self.enabled.setAccessibleName("Lower other sounds while I talk")

        voices, voice_box = card_section("Voices (these do the lowering)", 8)
        self.voice_boxes: dict[str, QCheckBox] = {}
        sources = [s for s in collection.sources if s.kind in AUDIO_KINDS]
        for source in sources:
            box = QCheckBox(literal(source.name))
            box.setChecked(is_voice(source.settings, source.kind))
            self.voice_boxes[source.id] = box
            voice_box.addWidget(box)
        if not sources:
            voice_box.addWidget(QLabel("No audio sources yet."))

        sound, sound_box = card_section("How it sounds")
        form = QFormLayout()
        sound_box.addLayout(form)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        row, self.amount = _slider(3, 30, -audio.duck_amount_db, "−{} dB", "Lower others by")
        form.addRow("Lower others by", row)
        row, self.threshold = _slider(-60, -10, audio.duck_threshold_db, "{} dB", "Talk sensitivity")
        self.threshold.setToolTip("Lower = reacts to quieter speech. Raise it if background noise sets it off.")
        form.addRow("Talk sensitivity", row)
        row, self.attack = _slider(50, 1000, audio.duck_attack_ms, "{} ms", "Fade down")
        form.addRow("Fade down", row)
        row, self.release = _slider(200, 3000, audio.duck_release_ms, "{} ms", "Come back")
        form.addRow("Come back", row)
        row, self.hold = _slider(0, 2000, audio.duck_hold_ms, "{} ms", "Wait before")
        self.hold.setToolTip("How long to wait after you stop talking, so it doesn't bounce between words")
        form.addRow("Wait before", row)

        # Off: the voices and how it sounds can't be changed, and look it (PC 2026-11-20: the ticks looked
        # live but didn't respond).
        for part in (voices, sound):
            part.setProperty("duckPart", True)  # its name stays the section card's (its look)
        # (The tick boxes' own leaf pictures already have a quiet off version: only the words and bars change.)
        self.setStyleSheet(
            f"QFrame[duckPart='true'] QCheckBox:disabled, QFrame[duckPart='true'] QLabel:disabled {{ color: {theme.BUTTON_EDGE}; }}"
            f"QFrame[duckPart='true'] QSlider::sub-page:horizontal:disabled {{ background: {theme.EDGE_STRONG}; }}")

        def toggle(on: bool) -> None:
            voices.setEnabled(on)
            sound.setEnabled(on)

        self.enabled.toggled.connect(toggle)
        toggle(audio.ducking)
        self.section().addLayout(_row("Lower other sounds while I talk", self.enabled))
        self.body.addWidget(voices)
        self.body.addWidget(sound)
        self.ok = self.add_buttons("Save")

    def settings(self, keep: AudioSettings | None = None) -> AudioSettings:
        """The ducking options, on top of the rest of the audio settings (track names, sound types...)."""
        return (keep or AudioSettings()).model_copy(update={
            "ducking": self.enabled.isChecked(),
            "duck_amount_db": -float(self.amount.value()),
            "duck_threshold_db": float(self.threshold.value()),
            "duck_attack_ms": float(self.attack.value()),
            "duck_release_ms": float(self.release.value()),
            "duck_hold_ms": float(self.hold.value()),
        })

    def apply_to(self, collection: SceneCollection) -> None:
        collection.audio = self.settings(collection.audio)
        for source in collection.sources:
            box = self.voice_boxes.get(source.id)
            if box is not None:
                source.settings = {**source.settings, "duck_voice": box.isChecked()}
