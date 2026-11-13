"""Webcam and capture-card source through DirectShow.

Frames are read with FFmpeg's DirectShow input (PyAV, already loaded for encoding). OpenCV is only
a fallback, imported if FFmpeg can't open the camera: on its own it adds about 90 MB of memory.

Devices are remembered by their system path and name rather than by index, because Windows
renumbers cameras when one is plugged in or out. A capture thread reads frames as they arrive;
if the camera is unplugged it keeps the last picture and tries to reopen it every two seconds.
"""

from __future__ import annotations

import logging
import sys
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
    log.info("loading OpenCV (camera fallback)")
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("unavailable: opencv-python is not installed") from exc
    return cv2


def _dshow_list():
    """DirectShow's camera list as (name, device path) pairs, without importing OpenCV, or None.

    cv2_enumerate_cameras' package imports OpenCV just for a few constants, so its native DirectShow
    lister is loaded on its own, straight from its file.
    """
    if sys.platform != "win32":
        return None
    name = "cv2_enumerate_cameras._windows_backend"
    module = sys.modules.get(name)
    if module is not None:
        return module.DSHOW_enumerate_cameras() or []
    import glob
    import importlib.machinery
    import importlib.util
    import os

    try:
        spec = importlib.util.find_spec("cv2_enumerate_cameras")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    folder = list(spec.submodule_search_locations)[0]
    files = glob.glob(os.path.join(folder, "_windows_backend*.pyd"))
    if not files:
        return None
    libs = os.path.join(os.path.dirname(folder), "cv2_enumerate_cameras.libs")  # its bundled C++ runtime
    if os.path.isdir(libs):
        os.add_dll_directory(libs)
    loader = importlib.machinery.ExtensionFileLoader(name, files[0])
    module = importlib.util.module_from_spec(importlib.util.spec_from_file_location(name, files[0], loader=loader))
    loader.exec_module(module)
    sys.modules[name] = module
    return module.DSHOW_enumerate_cameras() or []


def list_cameras() -> list[CameraDevice]:
    """Cameras visible to DirectShow, in DirectShow's order. Empty when the lookup is unavailable."""
    try:
        found = _dshow_list()
    except Exception as exc:  # driver hiccup while listing
        log.warning("could not list cameras: %s", exc)
        return []
    if found is not None:
        return [CameraDevice(i, name, path or "") for i, (name, path) in enumerate(found)]
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
        self.backend = ""  # "FFmpeg" or "OpenCV" once a camera opened: memcheck reports it
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

    def _mode(self) -> tuple[int, int, int]:
        return int(self.settings.get("width", 1280)), int(self.settings.get("height", 720)), int(self.settings.get("fps", 30))

    def _run(self) -> None:
        while not self._stop.is_set():
            self._restart.clear()
            device = find_camera(str(self.settings.get("name", "")), str(self.settings.get("path", "")))
            opened = device is not None and (self._run_ffmpeg(device) or self._run_opencv(device))
            if self._stop.is_set() or self._restart.is_set():
                continue
            if not opened:
                self.status = "camera not found, retrying" if device is None else self.status
            self._restart.wait(RETRY_SECONDS)

    def _deliver(self, frame: np.ndarray) -> None:
        if self.settings.get("mirror"):
            frame = frame[:, ::-1]
        self._size = (frame.shape[1], frame.shape[0])
        self._frames.put(np.ascontiguousarray(frame))

    # FFmpeg (PyAV) DirectShow: the default ---------------------------------------------

    def _open_ffmpeg(self, device: CameraDevice):
        """Open the camera through FFmpeg's DirectShow input, or None if that isn't possible."""
        try:
            import av
        except ImportError:
            return None
        if "dshow" not in av.formats_available:
            return None
        w, h, fps = self._mode()
        names = [f"video=@device_pnp_{device.path}"] if device.path.startswith("\\\\?\\") else []
        names.append(f"video={device.name}")
        modes = [  # MJPG at the size asked first (what most webcams need for 1080p30 / 720p60), then looser
            {"video_size": f"{w}x{h}", "framerate": str(fps), "vcodec": "mjpeg"},
            {"video_size": f"{w}x{h}", "framerate": str(fps)},
            {},
        ]
        for name in names:
            for mode in modes:
                try:
                    return av.open(name, format="dshow", options={**mode, "rtbufsize": "16M"})
                except Exception as exc:  # busy, mode not supported, name not found, ...
                    log.debug("%s: dshow %s %s failed: %s", self.id, name, mode, exc)
        log.info("%s: FFmpeg couldn't open %r, trying OpenCV", self.id, device.name)
        return None

    def _run_ffmpeg(self, device: CameraDevice) -> bool:
        container = self._open_ffmpeg(device)
        if container is None:
            return False
        try:
            stream = container.streams.video[0]
            # FFmpeg's default is a decoder thread per CPU core, each holding its own full-size frames
            # (tens of MB on a 16-core PC). Two threads decode 1080p60 MJPEG easily.
            stream.codec_context.thread_count = 2
            self.backend = "FFmpeg"
            rate = stream.average_rate or stream.base_rate
            fps = f" @ {float(rate):.0f}" if rate else ""
            self.status = f"capturing {stream.codec_context.width}x{stream.codec_context.height}{fps}"
            for packet in container.demux(stream):
                if self._stop.is_set() or self._restart.is_set():
                    break
                for frame in packet.decode():
                    self._deliver(frame.to_ndarray(format="bgr24"))
            else:
                self.status = "camera disconnected, retrying"
        except Exception as exc:  # unplugged mid-stream
            log.info("%s: camera stopped: %s", self.id, exc)
            self.status = "camera disconnected, retrying"
        finally:
            container.close()
        return True

    # OpenCV: the fallback --------------------------------------------------------------

    def _run_opencv(self, device: CameraDevice) -> bool:
        try:
            cv2 = _cv2()
        except RuntimeError as exc:
            self.status = str(exc)
            return False
        cap = cv2.VideoCapture(device.index, cv2.CAP_DSHOW)
        self.backend = "OpenCV"
        if not cap.isOpened():
            cap.release()
            self.status = "camera busy or not available, retrying"
            return False
        w, h, fps = self._mode()
        # FOURCC must be set before the size for DirectShow to pick the MJPG mode.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # always the newest frame, no backlog
        got = cap.get(cv2.CAP_PROP_FPS)
        self.status = (f"capturing {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}"
                       + (f" @ {got:.0f}" if got else ""))
        try:
            while not (self._stop.is_set() or self._restart.is_set()):
                ok, frame = cap.read()
                if not ok or frame is None:
                    self.status = "camera disconnected, retrying"
                    break
                self._deliver(frame)
        finally:
            cap.release()
        return True
