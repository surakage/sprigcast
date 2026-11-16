"""Recording settings page: folder, quality, file type and which canvases to record."""

from __future__ import annotations

from PySide6.QtCore import QSettings
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

from sprigcast.app import theme
from sprigcast.core.scene import Canvas, RecordingSettings

SIZE_LABELS = {
    None: "Canvas size (best quality)",
    1080: "1080p at most",
    720: "720p (lighter: for slower PCs)",
}
CONTAINER_LABELS = {
    "mkv": "MKV (recommended: still plays if SprigCast or Windows crashes)",
    "mp4": "MP4 (plays everywhere, but the file is lost if recording is cut off)",
}
# Repot: copy a finished MKV into an MP4 (remux: same video and sound, a new file type, no quality change).
REPOT = "advanced/remux_mp4"  # keys kept from when this lived in Advanced, so the choice carries over
REPOT_DELETE = "advanced/remux_delete_mkv"


def repot_choice() -> tuple[bool, bool]:
    """(repot each MKV recording as an MP4 when it stops, delete the MKV afterwards)."""
    q = QSettings()
    return q.value(REPOT, False, type=bool), q.value(REPOT_DELETE, False, type=bool)


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
        hint.setStyleSheet(f"color: {theme.MUTED}")
        form.addRow("", hint)

        self.container = QComboBox()
        for key, label in CONTAINER_LABELS.items():
            self.container.addItem(label, key)
        self.container.setCurrentIndex(list(CONTAINER_LABELS).index(settings.container))
        form.addRow("File type", self.container)
        repot, delete = repot_choice()
        self.repot = QCheckBox("Repot as MP4 when it stops")
        self.repot.setToolTip("Copies the MKV into an MP4 in seconds: same video and sound, no quality change")
        self.repot.setChecked(repot)
        form.addRow("", self.repot)
        self.repot_delete = QCheckBox("Then delete the MKV")
        self.repot_delete.setChecked(delete)
        form.addRow("", self.repot_delete)

        def follow(*_):
            mkv = self.container.currentData() == "mkv"
            self.repot.setEnabled(mkv)
            self.repot_delete.setEnabled(mkv and self.repot.isChecked())

        self.container.currentIndexChanged.connect(follow)
        self.repot.toggled.connect(follow)
        follow()

        self.size = QComboBox()
        for short, label in SIZE_LABELS.items():
            self.size.addItem(label, short)
        if self.size.findData(settings.short_side) < 0:
            self.size.addItem(f"{settings.short_side}p", settings.short_side)
        self.size.setCurrentIndex(self.size.findData(settings.short_side))
        form.addRow("Size", self.size)


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
        q = QSettings()
        q.setValue(REPOT, self.repot.isChecked())
        q.setValue(REPOT_DELETE, self.repot_delete.isChecked())
        settings.short_side = self.size.currentData()
        if self.canvas_boxes:
            settings.canvases = [cid for cid, box in self.canvas_boxes.items() if box.isChecked()]
