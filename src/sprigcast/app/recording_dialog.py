"""Recording settings: folder, quality and file type."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from sprigcast.core.scene import RecordingSettings

CONTAINER_LABELS = {
    "mkv": "MKV (recommended: still plays if SprigCast or Windows crashes)",
    "mp4": "MP4 (plays everywhere, but the file is lost if recording is cut off)",
}


class RecordingDialog(QDialog):
    def __init__(self, settings: RecordingSettings, default_folder: str, encoder: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Recording settings")
        self.setMinimumWidth(560)
        form = QFormLayout(self)

        self.folder = QLineEdit(settings.folder or default_folder)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        form.addRow("Folder", row)

        self.bitrate = QSpinBox()
        self.bitrate.setRange(1000, 100000)
        self.bitrate.setSingleStep(1000)
        self.bitrate.setSuffix(" kbps")
        self.bitrate.setValue(settings.bitrate_kbps)
        form.addRow("Video bitrate", self.bitrate)
        hint = QLabel("12000 is good for 1080p60. Use 20000 or more for fast games.")
        hint.setStyleSheet("color: gray")
        form.addRow("", hint)

        self.container = QComboBox()
        for key, label in CONTAINER_LABELS.items():
            self.container.addItem(label, key)
        self.container.setCurrentIndex(list(CONTAINER_LABELS).index(settings.container))
        form.addRow("File type", self.container)

        form.addRow("Encoder", QLabel(encoder))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Recording folder", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def apply_to(self, settings: RecordingSettings, default_folder: str) -> None:
        folder = self.folder.text().strip()
        settings.folder = "" if folder == default_folder else folder
        settings.bitrate_kbps = self.bitrate.value()
        settings.container = self.container.currentData()
