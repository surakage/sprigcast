"""Pick a camera, the size and frame rate SprigCast takes from it, and how light it should run."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import theme
from sprigcast.app.card import CardDialog, combo, field_label

from sprigcast.core.device_makers import label as camera_label
from sprigcast.sources.camera import (
    SIZES,
    CameraDevice,
    choose_modes,
    dshow_modes,
    is_capture_card,
    mode_cost,
    plain_mode,
)

RATES = (60, 30, 15)
FORMAT_NAMES = {"nv12": "NV12", "yuyv422": "YUY2", "mjpeg": "MJPEG", "uyvy422": "UYVY", "bgr24": "RGB24"}


def _field(label: str, box: QWidget) -> QVBoxLayout:
    """A label over its drop-down."""
    col = QVBoxLayout()
    col.setSpacing(6)
    col.addWidget(field_label(label))
    col.addWidget(box)
    return col


class CameraDialog(CardDialog):
    def __init__(self, cameras: list[CameraDevice], current: dict[str, Any] | None = None,
                 parent: QWidget | None = None, *, max_fps: int = 60, modes: list | None = None):
        """max_fps: the canvas frame rate (a camera faster than that only costs more); modes: what the
        chosen camera said it offers, when it's already open (see CameraSource.modes_seen)."""
        super().__init__("Camera", parent, width=480)
        from sprigcast.app.game_dialog import _row
        from sprigcast.app.live_stage import Switch

        self.cameras = cameras
        self._modes = {}  # camera path or name -> its listed modes, once known
        current = current or {}

        self.device = combo()
        for cam in cameras:
            self.device.addItem(camera_label(cam.name))
        index = next((i for i, c in enumerate(cameras) if c.path and c.path == current.get("path")), None)
        if index is None:
            index = next((i for i, c in enumerate(cameras) if c.name == current.get("name")), 0)
        self.device.setCurrentIndex(index)
        if modes and cameras:
            self._modes[self._key(cameras[index])] = list(modes)
        self.body.addLayout(_field("Device", self.device))

        self.size = combo(8)
        for w, h in SIZES:
            self.size.addItem(f"{w} x {h}", (w, h))
        wanted = (current.get("width", 1280), current.get("height", 720))
        self.size.setCurrentIndex(next((i for i, s in enumerate(SIZES) if s == wanted), SIZES.index((1280, 720))))

        self.rate = combo(6)
        rates = [r for r in RATES if r <= max_fps] or [min(RATES)]
        for r in rates:  # faster than the canvas only costs more: those frames are never shown
            self.rate.addItem(f"{r} fps", r)
        card = "width" not in current and is_capture_card(cameras[index].name) if cameras else False
        if card and (1920, 1080) in SIZES:  # a console or second PC: full HD, filling the canvas
            self.size.setCurrentIndex(SIZES.index((1920, 1080)))
        fps = min(int(current.get("fps", 60 if card else 30)), max(rates))
        self.rate.setCurrentIndex(next((i for i, r in enumerate(rates) if r == fps), rates.index(min(30, max(rates)))
                                       if min(30, max(rates)) in rates else 0))
        pair = QHBoxLayout()
        pair.setSpacing(12)
        pair.addLayout(_field("Size", self.size), 1)
        pair.addLayout(_field("Frame rate", self.rate), 1)
        self.body.addLayout(pair)

        # What the device itself is asked for, as it lists it (a capture card fed 1440p, a camera's MJPEG): off,
        # SprigCast picks from the device's list for the size and rate above.
        self.custom = Switch()
        self.custom.setChecked(current.get("capture_mode") == "custom")
        self.body.addLayout(_row("Custom device mode", self.custom))
        self.device_size = combo(4)
        self.device_rate = combo(4)
        self.device_format = combo(4)
        self.custom_box = QWidget()
        trio = QHBoxLayout(self.custom_box)
        trio.setContentsMargins(0, 0, 0, 0)
        trio.setSpacing(12)
        trio.addLayout(_field("Device size", self.device_size), 5)
        trio.addLayout(_field("Device rate", self.device_rate), 4)
        trio.addLayout(_field("Format", self.device_format), 4)
        self.body.addWidget(self.custom_box)
        self._custom_wanted = (tuple(current.get("capture_size") or ()), current.get("capture_fps"),
                               current.get("capture_format"))
        self.custom.toggled.connect(self._show_custom)
        for box in (self.device_size, self.device_rate, self.device_format):
            box.currentIndexChanged.connect(lambda *_: self._explain())
        self.device.currentIndexChanged.connect(self._fill_custom)
        self.device_size.currentIndexChanged.connect(self._fill_rates)

        self.pause = Switch()
        self.pause.setToolTip("Frees the camera and its memory while no bloom on screen shows it.\n"
                              "It takes a second to come back.")
        self.pause.setChecked(bool(current.get("pause_off_screen", True)))
        self.body.addLayout(_row("Pause when not on screen", self.pause))
        self.mirror = Switch()
        self.mirror.setChecked(bool(current.get("mirror", False)))
        self.body.addLayout(_row("Mirror", self.mirror))

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"{theme.NOTE_STYLE} padding: 10px 14px;")
        self.body.addWidget(self.note)
        self.lighter = QPushButton("Find a lighter camera")
        self.lighter.setStyleSheet(theme.button_style(17, "8px 16px"))
        self.lighter.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lighter.setAutoDefault(False)
        self.lighter.setToolTip("Checks what your other cameras offer at this size and frame rate")
        self.lighter.clicked.connect(self._find_lighter)
        self.body.addWidget(self.lighter, 0, Qt.AlignmentFlag.AlignLeft)
        self.lighter.setVisible(len(cameras) > 1)
        self.ok = self.add_buttons("Save")

        for box in (self.device, self.size, self.rate):
            box.currentIndexChanged.connect(self._explain)
        self.device.currentIndexChanged.connect(self._retitle)
        self._retitle()
        self._fill_custom()
        self._show_custom()
        self._explain()

    # The device's own mode (Custom device mode) -------------------------------------------------------------
    def _listed(self) -> list:
        return [m for m in (self._modes.get(self._key(self.device_choice())) or [])
                if m[0] == "pixel_format" or m[1] == "mjpeg"] if self.cameras else []

    def _fill_custom(self) -> None:
        """The device's sizes as it lists them, biggest first; read once the switch is on (a list takes a moment)."""
        if self.cameras and self.custom.isChecked():
            self._modes_of(self.device_choice())
        sizes = sorted({(m[2], m[3]) for m in self._listed()}, key=lambda s: -s[0] * s[1])
        want = self._custom_wanted[0]
        self.device_size.blockSignals(True)
        self.device_size.clear()
        for w, h in sizes:
            self.device_size.addItem(f"{w} x {h}", (w, h))
        if not sizes and len(want) == 2:
            self.device_size.addItem(f"{want[0]} x {want[1]}", tuple(want))
        at = next((i for i in range(self.device_size.count()) if self.device_size.itemData(i) == want), 0)
        self.device_size.setCurrentIndex(at)
        self.device_size.blockSignals(False)
        self._fill_rates()

    def _fill_rates(self) -> None:
        size = self.device_size.currentData()
        here = [m for m in self._listed() if (m[2], m[3]) == size]
        rates = sorted({round(m[5]) for m in here} | {r for r in RATES for m in here if m[4] - 0.01 <= r <= m[5] + 0.01},
                       reverse=True) or [int(self._custom_wanted[1] or 60)]
        formats = sorted({m[1] for m in here}, key=lambda f: {"nv12": 0, "yuyv422": 1, "mjpeg": 2}.get(f, 9))
        _, want_fps, want_fmt = self._custom_wanted
        self.device_rate.clear()
        for r in rates:
            self.device_rate.addItem(f"{r} fps", r)
        self.device_rate.setCurrentIndex(next((i for i, r in enumerate(rates) if r == want_fps), 0))
        self.device_format.clear()
        self.device_format.addItem("Automatic", "")
        for f in formats:
            self.device_format.addItem(FORMAT_NAMES.get(f, f.upper()), f)
        self.device_format.setCurrentIndex(next((i for i in range(self.device_format.count())
                                                 if self.device_format.itemData(i) == want_fmt and want_fmt), 0))

    def _show_custom(self, *_args) -> None:
        on = self.custom.isChecked()
        if on and not self._listed():
            self._fill_custom()
        self.custom_box.setVisible(on)
        self._explain()
        self.adjustSize()

    def _is_card(self) -> bool:
        return bool(self.cameras) and is_capture_card(self.device_choice().name)

    def _retitle(self) -> None:
        """A capture card (Elgato HD60 X...) is called what it is, not a camera."""
        title = "Video capture" if self._is_card() else "Camera"
        self.heading.setText(title)
        self.setWindowTitle(title)
        self.lighter.setVisible(len(self.cameras) > 1 and not self._is_card())

    @staticmethod
    def _key(cam: CameraDevice) -> str:
        return cam.path or cam.name

    def _ask(self) -> tuple[int, int, int]:
        w, h = self.size.currentData()
        return w, h, int(self.rate.currentData())

    def _say(self, text: str) -> None:
        self.note.setText(text)
        self.note.setVisible(bool(text))

    def _explain(self) -> None:
        """Say what the camera really sends for what's asked, when we know (it's open, or was checked)."""
        if not self.cameras:
            self._say("")
            return
        noun = "capture card" if self._is_card() else "camera"
        size = self.device_size.currentData()
        if self.custom.isChecked() and size:
            w, h, fps = self._ask()
            fmt = self.device_format.currentData() or ""
            mode = {"video_size": f"{size[0]}x{size[1]}", "framerate": str(self.device_rate.currentData() or fps)}
            if fmt:
                mode["vcodec" if fmt == "mjpeg" else "pixel_format"] = fmt
            shrink = f", and SprigCast shrinks it to {w} x {h}" if tuple(size) != (w, h) else ""
            self._say(f"The {noun} is asked for {plain_mode(mode)}{shrink}.")
            return
        modes = self._modes.get(self._key(self.device_choice()))
        self._say(describe_mode(modes, *self._ask(), noun=noun) if modes else "")

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
            self._say((self.note.text() + "\n" if self.note.text() else "")
                      + "None of your other cameras is lighter for this.")
            return
        cam, mode = best
        self._say((self.note.text() + "\n" if self.note.text() else "")
                  + f"Lighter: {cam.name} sends {plain_mode(mode)}.")
        self.device.setCurrentIndex(self.cameras.index(cam))

    def device_choice(self) -> CameraDevice:
        return self.cameras[self.device.currentIndex()]

    def settings(self) -> dict[str, Any]:
        cam = self.device_choice()
        w, h, fps = self._ask()
        out = {"name": cam.name, "path": cam.path, "width": w, "height": h, "fps": fps,
               "mirror": self.mirror.isChecked(), "pause_off_screen": self.pause.isChecked()}
        size = self.device_size.currentData()
        if self.custom.isChecked() and size:
            out.update(capture_mode="custom", capture_size=list(size), capture_fps=int(self.device_rate.currentData()
                                                                                       or fps),
                       capture_format=self.device_format.currentData() or "")
        else:
            out["capture_mode"] = "auto"
        return out


def describe_mode(modes: list, w: int, h: int, fps: int, noun: str = "camera") -> str:
    """One plain line on what the camera sends for this ask and what SprigCast does with it."""
    options = choose_modes(modes, w, h, fps)
    if not options:
        return ""
    first = options[0]
    mw, mh = (int(v) for v in first["video_size"].split("x"))
    parts = []
    if (mw, mh) != (w, h):
        parts.append(f"shrinks it to {w} x {h}")
    rate = _rate_value(first.get("framerate", str(fps)))
    if rate > fps * 1.1:
        parts.append(f"skips frames down to {fps} fps")
    sends = plain_mode(first)
    if not parts:
        return f"The {noun} sends {sends}, exactly what's needed."
    return f"The {noun} only sends {sends}, so SprigCast " + " and ".join(parts) + "."


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
