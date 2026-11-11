"""The Settings window: one place for every option, in tabs down the left side like OBS.

Tabs: General, Stream, Recording, Audio, Video, Hotkeys. OK and Apply write the changes into the
scene collection and QSettings; the main window then refreshes itself (see MainWindow._after_settings).
"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app.hotkeys_dialog import HotkeysPage
from sprigcast.app.recording_dialog import RecordingPage
from sprigcast.app.stream_dialog import StreamPage
from sprigcast.app.theme import THEMES, chosen_theme
from sprigcast.core.scene import SceneCollection

TABS = ["General", "Stream", "Recording", "Audio", "Video", "Hotkeys"]
FPS_CHOICES = [24, 25, 30, 48, 50, 60]
AUDIO_KINDS = ("audio_input", "audio_output")

# QSettings keys
CONFIRM_STREAM = "general/confirm_end_stream"
CONFIRM_RECORDING = "general/confirm_stop_recording"
PENDING_VIDEO = "video/pending"  # canvas changes saved here, applied at the next start


def confirm_enabled(key: str) -> bool:
    return QSettings().value(key, True, type=bool)


def pending_video() -> dict[str, dict[str, Any]]:
    raw = QSettings().value(PENDING_VIDEO, "")
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def apply_pending_video(collection: SceneCollection) -> bool:
    """At startup, move canvas changes from Settings → Video into the collection. True if any applied."""
    pending = pending_video()
    if not pending:
        return False
    for canvas in collection.canvases:
        change = pending.get(canvas.id)
        if change:
            canvas.width = int(change.get("width", canvas.width))
            canvas.height = int(change.get("height", canvas.height))
            canvas.fps = int(change.get("fps", canvas.fps))
            canvas.enabled = bool(change.get("enabled", canvas.enabled))
    QSettings().remove(PENDING_VIDEO)
    return True


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: gray")
    return label


def _page(*widgets: QWidget, stretch: bool = True) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        layout.addWidget(widget)
    if stretch:
        layout.addStretch(1)
    return page


class SettingsWindow(QDialog):
    applied = Signal()

    def __init__(
        self,
        collection: SceneCollection,
        *,
        default_recordings_folder: str,
        encoder: str,
        audio_devices: dict[str, list[str]],
        plugin_report=None,
        open_plugins_folder=None,
        streaming: bool = False,
        recording: bool = False,
        tab: str = "General",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(900, 600)
        self.collection = collection
        self.default_recordings_folder = default_recordings_folder
        self.streaming, self.recording = streaming, recording

        self.tabs = QListWidget()
        self.tabs.setObjectName("settingsTabs")
        self.tabs.setFixedWidth(160)
        self.tabs.setStyleSheet("#settingsTabs::item { padding: 9px 12px; }")
        font = self.tabs.font()
        font.setPointSizeF(font.pointSizeF() * 1.15)
        self.tabs.setFont(font)
        self.stack = QStackedWidget()
        for name, page in (
            ("General", self._general_page(plugin_report, open_plugins_folder)),
            ("Stream", self._stream_page()),
            ("Recording", self._recording_page(encoder)),
            ("Audio", self._audio_page(audio_devices)),
            ("Video", self._video_page()),
            ("Hotkeys", self._hotkeys_page()),
        ):
            QListWidgetItem(name, self.tabs)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(page)
            self.stack.addWidget(scroll)
        self.tabs.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.tabs.setCurrentRow(TABS.index(tab) if tab in TABS else 0)

        body = QHBoxLayout()
        body.addWidget(self.tabs)
        body.addWidget(self.stack, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        root = QVBoxLayout(self)
        root.addLayout(body, 1)
        root.addWidget(buttons)

    def current_tab(self) -> str:
        return TABS[self.tabs.currentRow()]

    # Pages -------------------------------------------------------------------------

    def _general_page(self, plugin_report, open_plugins_folder) -> QWidget:
        look = QGroupBox("Appearance")
        form = QFormLayout(look)
        self.theme = QComboBox()
        for key, label in THEMES.items():
            self.theme.addItem(label, key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(chosen_theme())))
        form.addRow("Theme", self.theme)

        safety = QGroupBox("Confirmations")
        box = QVBoxLayout(safety)
        self.confirm_stream = QCheckBox("Ask before ending a stream")
        self.confirm_stream.setChecked(confirm_enabled(CONFIRM_STREAM))
        box.addWidget(self.confirm_stream)
        self.confirm_recording = QCheckBox("Ask before stopping a recording")
        self.confirm_recording.setChecked(confirm_enabled(CONFIRM_RECORDING))
        box.addWidget(self.confirm_recording)

        plugins = QGroupBox("Plugins")
        box = QVBoxLayout(plugins)
        lines = []
        if plugin_report is not None:
            lines += [f"✓ {name}" for name in plugin_report.loaded]
            lines += [f"✗ {name}: {error}" for name, error in plugin_report.failed.items()]
        status = QLabel("\n".join(lines) or "No plugins loaded.")
        status.setWordWrap(True)
        status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(status)
        box.addWidget(_hint("Drop a .py file into the plugins folder and restart SprigCast to load it."))
        if open_plugins_folder is not None:
            open_button = QPushButton("Open plugins folder")
            open_button.clicked.connect(open_plugins_folder)
            row = QHBoxLayout()
            row.addWidget(open_button)
            row.addStretch(1)
            box.addLayout(row)
        return _page(look, safety, plugins)

    def _stream_page(self) -> QWidget:
        self.stream_page = StreamPage(self.collection)
        widgets: list[QWidget] = [self.stream_page]
        if self.streaming:
            self.stream_page.setEnabled(False)
            widgets.insert(0, _hint("You're live. End the stream to change these."))
        return _page(*widgets, stretch=False)

    def _recording_page(self, encoder: str) -> QWidget:
        self.recording_page = RecordingPage(
            self.collection.recording, self.default_recordings_folder, encoder, self.collection.canvases
        )
        widgets: list[QWidget] = [self.recording_page]
        if self.recording:
            self.recording_page.setEnabled(False)
            widgets.insert(0, _hint("Recording now. Stop the recording to change these."))
        return _page(*widgets)

    def _audio_page(self, audio_devices: dict[str, list[str]]) -> QWidget:
        group = QGroupBox("Devices")
        form = QFormLayout(group)
        self.audio_combos: dict[str, QComboBox] = {}
        sources = [s for s in self.collection.sources if s.kind in AUDIO_KINDS]
        for source in sources:
            combo = QComboBox()
            combo.addItem("Default (follows Windows)", "default")
            for name in audio_devices.get(source.kind, []):
                combo.addItem(name, name)
            current = str(source.settings.get("device", "default"))
            if combo.findData(current) < 0:
                combo.addItem(f"{current} (not connected)", current)
            combo.setCurrentIndex(combo.findData(current))
            self.audio_combos[source.id] = combo
            kind = "Desktop audio" if source.kind == "audio_output" else "Microphone"
            form.addRow(f"{source.name}\n({kind})" if source.name != kind else source.name, combo)
        if not sources:
            form.addRow(_hint("No audio sources yet. Add them with + in the Audio Mixer dock."))
        hint = _hint(
            "\"Default\" follows the device Windows uses, so switching headsets in Windows just works. "
            "Volume and mute are in the Audio Mixer dock."
        )
        return _page(group, hint)

    def _video_page(self) -> QWidget:
        pending = pending_video()
        group = QGroupBox("Canvases")
        grid = QFormLayout(group)
        self.video_fields: dict[str, tuple[QCheckBox, QSpinBox, QSpinBox, QComboBox]] = {}
        for canvas in self.collection.canvases:
            values = {"width": canvas.width, "height": canvas.height, "fps": canvas.fps, "enabled": canvas.enabled}
            values.update(pending.get(canvas.id, {}))
            enabled = QCheckBox("On")
            enabled.setChecked(bool(values["enabled"]))
            width, height = QSpinBox(), QSpinBox()
            for box, value in ((width, values["width"]), (height, values["height"])):
                box.setRange(16, 7680)
                box.setSingleStep(2)
                box.setValue(int(value))
            fps = QComboBox()
            for choice in FPS_CHOICES:
                fps.addItem(f"{choice} fps", choice)
            if fps.findData(int(values["fps"])) < 0:
                fps.addItem(f"{values['fps']} fps", int(values["fps"]))
            fps.setCurrentIndex(fps.findData(int(values["fps"])))
            row = QHBoxLayout()
            row.addWidget(enabled)
            row.addWidget(width)
            row.addWidget(QLabel("×"))
            row.addWidget(height)
            row.addWidget(fps)
            row.addStretch(1)
            grid.addRow(canvas.id.capitalize(), row)
            self.video_fields[canvas.id] = (enabled, width, height, fps)
            if self.streaming or self.recording:
                for widget in (enabled, width, height, fps):
                    widget.setEnabled(False)
        note = "Canvas size and frame rate change the next time SprigCast starts."
        if pending:
            note += " Changes are waiting for a restart."
        if self.streaming or self.recording:
            note = "Stop streaming and recording to change the canvases. " + note
        return _page(group, _hint(note))

    def _hotkeys_page(self) -> QWidget:
        self.hotkeys_page = HotkeysPage(self.collection)
        return _page(self.hotkeys_page, stretch=False)

    # Saving ------------------------------------------------------------------------

    def _ok(self) -> None:
        if self.apply():
            self.accept()

    def apply(self) -> bool:
        """Write every tab back. Returns False (and shows why) if something can't be saved."""
        problem = self.hotkeys_page.problem()
        if problem:
            self.tabs.setCurrentRow(TABS.index("Hotkeys"))
            QMessageBox.warning(self, "Hotkeys", problem)
            return False
        if not self.streaming and not self.stream_page.apply_to(self.collection):
            self.tabs.setCurrentRow(TABS.index("Stream"))
            return False
        if not self.recording:
            self.recording_page.apply_to(self.collection.recording, self.default_recordings_folder)
        self.collection.hotkeys = self.hotkeys_page.result_hotkeys()
        for source in self.collection.sources:
            combo = self.audio_combos.get(source.id)
            if combo is not None and combo.currentData() != source.settings.get("device", "default"):
                device = combo.currentData()
                source.settings = {**source.settings, "device": device}
                base = "Desktop audio" if source.kind == "audio_output" else "Mic"
                source.name = base if device == "default" else f"{base} ({device})"

        settings = QSettings()
        settings.setValue("general/theme", self.theme.currentData())
        settings.setValue(CONFIRM_STREAM, self.confirm_stream.isChecked())
        settings.setValue(CONFIRM_RECORDING, self.confirm_recording.isChecked())
        if not (self.streaming or self.recording):
            changes = {}
            for canvas in self.collection.canvases:
                enabled, width, height, fps = self.video_fields[canvas.id]
                wanted = {
                    "width": width.value(),
                    "height": height.value(),
                    "fps": fps.currentData(),
                    "enabled": enabled.isChecked(),
                }
                current = {"width": canvas.width, "height": canvas.height, "fps": canvas.fps, "enabled": canvas.enabled}
                if wanted != current:
                    changes[canvas.id] = wanted
            if changes:
                settings.setValue(PENDING_VIDEO, json.dumps(changes))
            else:
                settings.remove(PENDING_VIDEO)
        self.applied.emit()
        return True
