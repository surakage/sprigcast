"""Webcam and capture-card source through OpenCV (DirectShow by default).

Devices are remembered by their system path and name rather than by index, because Windows
renumbers cameras when one is plugged in or out. A capture thread reads frames as they arrive;
if the camera is unplugged it keeps the last picture and tries to reopen it every two seconds.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

from sprigcast.core.mailbox import Mailbox
from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

RETRY_SECONDS = 2.0

# Offered in the picker. Most webcams only reach 1080p30 or 720p60 in MJPG, which we request.
MODES = [
    (1920, 1080, 30),
    (1920, 1080, 60),
    (1280, 720, 60),
    (1280, 720, 30),
    (640, 480, 30),
]


@dataclass(frozen=True)
class CameraDevice:
    index: int  # position in the backend's list right now; may change after replugging
    name: str
    path: str


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("unavailable: opencv-python is not installed") from exc
    return cv2


def list_cameras() -> list[CameraDevice]:
    """Cameras visible to DirectShow, in DirectShow's order. Empty when the lookup is unavailable."""
    try:
        cv2 = _cv2()
        from cv2_enumerate_cameras import enumerate_cameras
    except (ImportError, RuntimeError):
        return []
    try:
        return [CameraDevice(c.index, c.name, c.path or "") for c in enumerate_cameras(cv2.CAP_DSHOW)]
    except Exception as exc:  # driver hiccup while listing
        log.warning("could not list cameras: %s", exc)
        return []


def find_camera(name: str, path: str) -> CameraDevice | None:
    cameras = list_cameras()
    for match in (lambda c: path and c.path == path, lambda c: c.name == name):
        found = next((c for c in cameras if match(c)), None)
        if found is not None:
            return found
    return None


@register("camera")
class CameraSource(Source):
    """Settings: name, path, width, height, fps, mirror (bool)."""

    channel_order = "BGR"  # OpenCV delivers 3-channel BGR; the compositor swizzles on the GPU

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frames: Mailbox[np.ndarray] = Mailbox()
        self._size = (int(settings.get("width", 1280)), int(settings.get("height", 720)))
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = "starting"

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._restart.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def update(self, settings: dict[str, Any]) -> None:
        reopen = {k: v for k, v in settings.items() if k != "mirror"} != {
            k: v for k, v in self.settings.items() if k != "mirror"
        }
        super().update(settings)
        if reopen:
            self._restart.set()

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    def video_frame(self) -> np.ndarray | None:
        return self._frames.take()

    # Capture thread ------------------------------------------------------------------

    def _open(self, cv2):
        device = find_camera(str(self.settings.get("name", "")), str(self.settings.get("path", "")))
        if device is None:
            return None
        cap = cv2.VideoCapture(device.index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            return None
        # FOURCC must be set before the size for DirectShow to pick the MJPG mode.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(self.settings.get("width", 1280)))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self.settings.get("height", 720)))
        cap.set(cv2.CAP_PROP_FPS, int(self.settings.get("fps", 30)))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always the newest frame, no backlog
        return cap

    def _run(self) -> None:
        try:
            cv2 = _cv2()
        except RuntimeError as exc:
            self.status = str(exc)
            return
        while not self._stop.is_set():
            self._restart.clear()
            cap = self._open(cv2)
            if cap is None:
                self.status = "camera not found, retrying"
                self._restart.wait(RETRY_SECONDS)
                continue
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            self.status = f"capturing {w}x{h} @ {fps:.0f}" if fps else "capturing"
            try:
                while not (self._stop.is_set() or self._restart.is_set()):
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        self.status = "camera disconnected, retrying"
                        break
                    if self.settings.get("mirror"):
                        frame = cv2.flip(frame, 1)
                    self._size = (frame.shape[1], frame.shape[0])
                    self._frames.put(np.ascontiguousarray(frame))
            finally:
                cap.release()
            if not (self._stop.is_set() or self._restart.is_set()):
                self._restart.wait(RETRY_SECONDS)
