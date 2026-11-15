"""Pick a camera, the size and frame rate SprigCast takes from it, and how light it should run."""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from sprigcast.sources.camera import (
    SIZES,
    CameraDevice,
    choose_modes,
    dshow_modes,
    mode_cost,
    plain_mode,
)

RATES = (60, 30, 15)


class CameraDialog(QDialog):
    def __init__(self, cameras: list[CameraDevice], current: dict[str, Any] | None = None,
                 parent: QWidget | None = None, *, max_fps: int = 60, modes: list | None = None):
        """max_fps: the canvas frame rate (a camera faster than that only costs more); modes: what the
        chosen camera said it offers, when it's already open (see CameraSource.modes_seen)."""
        super().__init__(parent)
        self.setWindowTitle("Camera")
        self.cameras = cameras
        self._modes = {}  # camera path or name -> its listed modes, once known
        current = current or {}
        form = QFormLayout(self)

        self.device = QComboBox()
        for cam in cameras:
            self.device.addItem(cam.name)
        index = next((i for i, c in enumerate(cameras) if c.path and c.path == current.get("path")), None)
        if index is None:
            index = next((i for i, c in enumerate(cameras) if c.name == current.get("name")), 0)
        self.device.setCurrentIndex(index)
        if modes and cameras:
            self._modes[self._key(cameras[index])] = list(modes)
        form.addRow("Camera", self.device)

        self.size = QComboBox()
        for w, h in SIZES:
            self.size.addItem(f"{w} x {h}", (w, h))
        wanted = (current.get("width", 1280), current.get("height", 720))
        self.size.setCurrentIndex(next((i for i, s in enumerate(SIZES) if s == wanted), SIZES.index((1280, 720))))
        form.addRow("Size", self.size)

        self.rate = QComboBox()
        rates = [r for r in RATES if r <= max_fps] or [min(RATES)]
        for r in rates:  # faster than the canvas only costs more: those frames are never shown
            self.rate.addItem(f"{r} fps", r)
        fps = min(int(current.get("fps", 30)), max(rates))
        self.rate.setCurrentIndex(next((i for i, r in enumerate(rates) if r == fps), rates.index(min(30, max(rates)))
                                       if min(30, max(rates)) in rates else 0))
        form.addRow("Frame rate", self.rate)

        self.pause = QCheckBox("Pause when not on screen")
        self.pause.setToolTip("Frees the camera and its memory while no bloom on screen shows it.\n"
                              "It takes a second to come back.")
        self.pause.setChecked(bool(current.get("pause_off_screen", True)))
        form.addRow("", self.pause)

        self.mirror = QCheckBox("Mirror")
        self.mirror.setChecked(bool(current.get("mirror", False)))
        form.addRow("", self.mirror)

        self.note = QLabel()
        self.note.setWordWrap(True)
        form.addRow(self.note)
        self.lighter = QPushButton("Find a lighter camera")
        self.lighter.setToolTip("Checks what your other cameras offer at this size and frame rate")
        self.lighter.clicked.connect(self._find_lighter)
        form.addRow("", self.lighter)
        self.lighter.setVisible(len(cameras) > 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        for box in (self.device, self.size, self.rate):
            box.currentIndexChanged.connect(self._explain)
        self._explain()

    @staticmethod
    def _key(cam: CameraDevice) -> str:
        return cam.path or cam.name

    def _ask(self) -> tuple[int, int, int]:
        w, h = self.size.currentData()
        return w, h, int(self.rate.currentData())

    def _explain(self) -> None:
        """Say what the camera really sends for what's asked, when we know (it's open, or was checked)."""
        if not self.cameras:
            self.note.setText("")
            return
        modes = self._modes.get(self._key(self.device_choice()))
        self.note.setText(describe_mode(modes, *self._ask()) if modes else "")

    def _modes_of(self, cam: CameraDevice) -> list:
        key = self._key(cam)
        if key not in self._modes:
            name = f"video=@device_pnp_{cam.path}" if cam.path.startswith("\\\\?\\") else f"video={cam.name}"
            self._modes[key] = dshow_modes(name) or (dshow_modes(f"video={cam.name}") if "@device_pnp" in name else [])
        return self._modes[key]

    def _find_lighter(self) -> None:
        w, h, fps = self._ask()
        here = self.device_choice()
        best = lighter_camera(here, [c for c in self.cameras if c != here], self._modes_of, w, h, fps)
        self._explain()
        if best is None:
            self.note.setText((self.note.text() + "\n" if self.note.text() else "")
                              + "None of your other cameras is lighter for this.")
            return
        cam, mode = best
        self.note.setText((self.note.text() + "\n" if self.note.text() else "")
                          + f"Lighter: {cam.name} sends {plain_mode(mode)}.")
        self.device.setCurrentIndex(self.cameras.index(cam))

    def device_choice(self) -> CameraDevice:
        return self.cameras[self.device.currentIndex()]

    def settings(self) -> dict[str, Any]:
        cam = self.device_choice()
        w, h, fps = self._ask()
        return {"name": cam.name, "path": cam.path, "width": w, "height": h, "fps": fps,
                "mirror": self.mirror.isChecked(), "pause_off_screen": self.pause.isChecked()}


def describe_mode(modes: list, w: int, h: int, fps: int) -> str:
    """One plain line on what the camera sends for this ask and what SprigCast does with it."""
    options = choose_modes(modes, w, h, fps)
    if not options:
        return ""
    first = options[0]
    mw, mh = (int(v) for v in first["video_size"].split("x"))
    parts = []
    if (mw, mh) != (w, h):
        parts.append(f"shrinks it to {w} x {h}")
    rate = _rate_value(first["framerate"])
    if rate > fps * 1.1:
        parts.append(f"skips frames down to {fps} fps")
    sends = plain_mode(first)
    if not parts:
        return f"The camera sends {sends}, exactly what's needed."
    return f"The camera only sends {sends}, so SprigCast " + " and ".join(parts) + "."


def lighter_camera(here: CameraDevice, others: list[CameraDevice], modes_of, w: int, h: int, fps: int):
    """(camera, its mode) among `others` that gives this size and rate for clearly less data, or None."""
    mine = choose_modes(modes_of(here), w, h, fps)
    cost = mode_cost(mine[0], fps) if mine else float("inf")
    best = None
    for cam in others:
        options = choose_modes(modes_of(cam), w, h, fps)
        if not options:
            continue
        c = mode_cost(options[0], fps)
        if c < cost * 0.75 and (best is None or c < best[0]):
            best = (c, cam, options[0])
    return None if best is None else (best[1], best[2])


def _rate_value(text: str) -> float:
    num, _, den = text.partition("/")
    return float(num) / float(den) if den else float(num)
