"""Pick a camera, its resolution and frame rate, and whether to mirror it."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QWidget

from sprigcast.sources.camera import MODES, CameraDevice


class CameraDialog(QDialog):
    def __init__(self, cameras: list[CameraDevice], current: dict[str, Any] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Camera")
        self.cameras = cameras
        current = current or {}
        form = QFormLayout(self)

        self.device = QComboBox()
        for cam in cameras:
            self.device.addItem(cam.name)
        index = next((i for i, c in enumerate(cameras) if c.path and c.path == current.get("path")), None)
        if index is None:
            index = next((i for i, c in enumerate(cameras) if c.name == current.get("name")), 0)
        self.device.setCurrentIndex(index)
        form.addRow("Device", self.device)

        self.mode = QComboBox()
        for w, h, fps in MODES:
            self.mode.addItem(f"{w}x{h} at {fps} fps", (w, h, fps))
        wanted = (current.get("width", 1280), current.get("height", 720), current.get("fps", 30))
        self.mode.setCurrentIndex(next((i for i, m in enumerate(MODES) if m == wanted), MODES.index((1280, 720, 30))))
        form.addRow("Mode", self.mode)

        self.mirror = QCheckBox("Mirror horizontally")
        self.mirror.setChecked(bool(current.get("mirror", False)))
        form.addRow("", self.mirror)

        hint = QLabel("If the camera can't do the chosen mode, it picks the closest one it supports.")
        hint.setWordWrap(True)
        form.addRow(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def device_choice(self) -> CameraDevice:
        return self.cameras[self.device.currentIndex()]

    def settings(self) -> dict[str, Any]:
        cam = self.device_choice()
        w, h, fps = self.mode.currentData()
        return {"name": cam.name, "path": cam.path, "width": w, "height": h, "fps": fps, "mirror": self.mirror.isChecked()}
