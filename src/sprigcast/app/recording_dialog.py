"""Recording settings page: folder, quality, file type and which canvases to record."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from sprigcast.core.scene import Canvas, RecordingSettings

CONTAINER_LABELS = {
    "mkv": "MKV (recommended: still plays if SprigCast or Windows crashes)",
    "mp4": "MP4 (plays everywhere, but the file is lost if recording is cut off)",
}


class RecordingPage(QWidget):
    def __init__(
        self,
        settings: RecordingSettings,
        default_folder: str,
        encoder: str,
        canvases: list[Canvas] = (),
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)

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

        self.canvas_boxes: dict[str, QCheckBox] = {}
        for i, canvas in enumerate(canvases):
            box = QCheckBox(f"{canvas.id.capitalize()} ({canvas.width}×{canvas.height})")
            box.setChecked(canvas.id in settings.canvases)
            self.canvas_boxes[canvas.id] = box
            form.addRow("Record" if i == 0 else "", box)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Recording folder", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def apply_to(self, settings: RecordingSettings, default_folder: str) -> None:
        folder = self.folder.text().strip()
        settings.folder = "" if folder == default_folder else folder
        settings.bitrate_kbps = self.bitrate.value()
        settings.container = self.container.currentData()
        if self.canvas_boxes:
            settings.canvases = [cid for cid, box in self.canvas_boxes.items() if box.isChecked()]
