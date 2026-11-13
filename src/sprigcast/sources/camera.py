"""Webcam and capture-card source through DirectShow.

Frames are read with FFmpeg's DirectShow input (PyAV, already loaded for encoding). OpenCV is only
a fallback, imported if FFmpeg can't open the camera: on its own it adds about 90 MB of memory.

Devices are remembered by their system path and name rather than by index, because Windows
renumbers cameras when one is plugged in or out. A capture thread reads frames as they arrive;
if the camera is unplugged it keeps the last picture and tries to reopen it every two seconds.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
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


_MODE_LINE = re.compile(r"(vcodec|pixel_format)=(\S+)\s+min s=(\d+)x(\d+) fps=([\d.]+)\s+max s=(\d+)x(\d+) fps=([\d.]+)")


def parse_dshow_modes(lines) -> list[tuple[str, str, int, int, float, float]]:
    """(kind, name, width, height, min fps, max fps) from FFmpeg's `list_options` output.

    Newer FFmpeg prints the format and its sizes on separate log lines, so they're read as one text.
    """
    modes = []
    for m in _MODE_LINE.finditer(" ".join(str(line).strip() for line in lines)):
        if m.group(3, 4) == m.group(6, 7):
            modes.append((m.group(1), m.group(2), int(m.group(3)), int(m.group(4)), float(m.group(5)),
                          float(m.group(8))))
    return modes


listing_errors: list[str] = []  # the last listing's error and captured lines, for memcheck
listing_lines: list[str] = []


def dshow_modes(name: str) -> list[tuple[str, str, int, int, float, float]]:
    """Ask DirectShow which formats, sizes and frame rates a camera offers (empty if it can't say)."""
    try:
        import av
        import av.logging as avlog
    except ImportError:
        return []
    old = avlog.get_level()
    listing_errors.clear()
    avlog.set_level(avlog.INFO)  # the list comes as log lines, which PyAV drops by default
    try:
        with avlog.Capture(False) as logs:  # FFmpeg may log from its own threads too
            try:
                av.open(name, format="dshow", options={"list_options": "true"}).close()
            except Exception as exc:  # listing always "fails": there is nothing to open
                listing_errors.append(str(exc))
    finally:
        avlog.set_level(old)
    listing_lines[:] = [str(entry[-1]) if isinstance(entry, tuple) else str(entry) for entry in logs]
    return parse_dshow_modes(listing_lines)


def _rate(value: float) -> str:
    return f"{value:g}"


_FORMAT_ORDER = {"mjpeg": 0, "nv12": 1, "yuyv422": 2}  # least data per frame first


def choose_modes(modes, w: int, h: int, fps: int) -> list[dict[str, str]]:
    """dshow options to try, best first.

    At the size asked if the camera has it, else the smallest size above it (frames are shrunk to the
    size asked as they arrive), else whatever it has. Within that: the rate asked before faster ones
    (extra frames are skipped later), and MJPEG before NV12 before other raw formats.
    """
    usable = [m for m in modes if m[0] == "pixel_format" or m[1] == "mjpeg"]
    sizes = sorted({(m[2], m[3]) for m in usable}, key=lambda s: s[0] * s[1])
    size = ((w, h) if (w, h) in sizes else next((s for s in sizes if s[0] >= w and s[1] >= h), None)
            or (sizes[-1] if sizes else None))
    at_size = [m for m in usable if (m[2], m[3]) == size]
    exact = [m for m in at_size if m[4] - 0.01 <= fps <= m[5] + 0.01]
    others = sorted((m for m in at_size if m not in exact), key=lambda m: (m[5] < fps, abs(m[5] - fps)))
    out: list[dict[str, str]] = []
    for group, rate in ((exact, lambda m: str(fps)), (others, lambda m: _rate(m[5]))):
        for m in sorted(group, key=lambda m: _FORMAT_ORDER.get(m[1], 9)):
            option = {"video_size": f"{size[0]}x{size[1]}", "framerate": rate(m)}
            option["vcodec" if m[0] == "vcodec" else "pixel_format"] = m[1]
            if option not in out:
                out.append(option)
    return out


def shrunk_size(src: tuple[int, int], want: tuple[int, int]) -> tuple[int, int] | None:
    """The size to shrink camera frames to so they fit `want` (aspect kept, even numbers), or None."""
    scale = min(want[0] / src[0], want[1] / src[1])
    if scale >= 0.95:
        return None
    return max(2, round(src[0] * scale / 2) * 2), max(2, round(src[1] * scale / 2) * 2)


class _Feed:
    """One open camera, shared by every camera source set to the same device and mode.

    Two sources showing the same webcam (a wide shot in one scene, a cropped one in another) used to
    open the device twice: two capture threads, two decoders, two sets of frames. Now they subscribe
    to one feed and each gets the same frame.
    """

    def __init__(self, feed_id: str, settings: dict[str, Any]):
        self.id = feed_id  # for log lines: the first source that asked for it
        self.settings = dict(settings)
        self.subscribers: list[CameraSource] = []
        self.backend = ""  # "FFmpeg" or "OpenCV" once a camera opened: memcheck reports it
        self.modes_seen: list = []  # what DirectShow said the camera offers (for memcheck)
        self.opened_with: dict | None = None  # the dshow options that worked
        self.open_errors: list[str] = []  # why the other options didn't (for memcheck)
        self.status = "starting"
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.id}", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True) -> None:
        self._stop.set()
        self._restart.set()
        if wait:
            self.join()

    def join(self) -> None:
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)

    def retune(self, settings: dict[str, Any]) -> None:
        """New device or mode for the same (only) subscriber: close and reopen on the capture thread."""
        self.settings = dict(settings)
        self._restart.set()

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
        frame = np.ascontiguousarray(frame)
        for source in list(self.subscribers):
            source._receive(frame)

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
        # The camera's own list of modes first: it says exactly which format gives the size and rate
        # asked for (MJPEG is ~10x less data than raw frames; a raw mode at 30 fps beats one at 120).
        listed = []
        self.open_errors = []
        for name in names:
            self.modes_seen = dshow_modes(name)
            listed = choose_modes(self.modes_seen, w, h, fps)
            if self.modes_seen:
                break
        modes = [*listed,  # then guesses, from strict to loose
                 {"video_size": f"{w}x{h}", "framerate": str(fps), "vcodec": "mjpeg"},
                 {"video_size": f"{w}x{h}", "framerate": str(fps)},
                 {"video_size": f"{w}x{h}"},  # e.g. a camera whose 60 is really 59.94: its own rate, paced below
                 {}]
        for name in names:
            for mode in modes:
                try:
                    container = av.open(name, format="dshow", options={**mode, "rtbufsize": "16M"})
                except Exception as exc:  # busy, mode not supported, name not found, ...
                    log.debug("%s: dshow %s %s failed: %s", self.id, name, mode, exc)
                    self.open_errors.append(f"{name[:24]}… {mode}: {exc}")
                    continue
                log.info("%s: opened %s with %s", self.id, name, mode or "the camera's default mode")
                self.opened_with = mode
                return container
        log.info("%s: FFmpeg couldn't open %r, trying OpenCV", self.id, device.name)
        return None

    def _run_ffmpeg(self, device: CameraDevice) -> bool:
        container = self._open_ffmpeg(device)
        if container is None:
            return False
        try:
            stream = container.streams.video[0]
            # FFmpeg's default is a decoder thread per CPU core, each holding its own full-size frames
            # (tens of MB on a 16-core PC). One thread decodes 1080p60 MJPEG in a few ms per frame.
            stream.codec_context.thread_count = 1
            self.backend = f"FFmpeg, {stream.codec_context.name} {stream.codec_context.pix_fmt or ''}".strip()
            rate = stream.average_rate or stream.base_rate
            fps = f" @ {float(rate):.0f}" if rate else ""
            self.status = f"capturing {stream.codec_context.width}x{stream.codec_context.height}{fps}"
            # Cameras that only offer a faster mode (a Facecam at 120 fps) are paced down to the fps you
            # chose. MJPEG and raw frames stand alone, so the extra ones are skipped before decoding.
            skippable = stream.codec_context.name in ("mjpeg", "rawvideo")
            wanted = max(1, self._mode()[2])
            gap, last = 0.95 / wanted, 0.0
            if skippable and rate and float(rate) > wanted * 1.1:
                self.status += f", using {wanted}"
            # A camera bigger than the size you chose (it offers nothing smaller) is shrunk as each frame
            # is converted: less to copy, keep in memory and send to the GPU.
            target = shrunk_size((stream.codec_context.width, stream.codec_context.height), self._mode()[:2])
            convert = {"format": "bgr24"}
            if target is not None:
                convert.update(width=target[0], height=target[1], interpolation="BILINEAR")
                self.status += f", shrunk to {target[0]}x{target[1]}"
            for packet in container.demux(stream):
                if self._stop.is_set() or self._restart.is_set():
                    break
                if skippable and packet.size:
                    now = time.monotonic()
                    if now - last < gap:
                        continue
                    last = now
                for frame in packet.decode():
                    self._deliver(frame.to_ndarray(**convert))
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


_feeds: dict[tuple, _Feed] = {}  # device + mode -> the feed sources share
_closing: dict[tuple, _Feed] = {}  # feeds told to stop without waiting (a source went to sleep)
_feeds_lock = threading.Lock()


def _feed_key(settings: dict[str, Any]) -> tuple:
    """Sources with the same device, size and rate share a feed; mirroring is done per source."""
    return tuple(sorted((k, repr(v)) for k, v in settings.items() if k != "mirror"))


@register("camera")
class CameraSource(Source):
    """Settings: name, path, width, height, fps, mirror (bool)."""

    channel_order = "BGR"  # OpenCV delivers 3-channel BGR; the compositor swizzles on the GPU
    sleeps = True  # only used when "Sources that aren't on screen" is set to pause cameras too

    def __init__(self, source_id: str, settings: dict[str, Any]):
        self._feed: _Feed | None = None  # the last feed this source used, attached or not
        self._attached = False
        self._status = "starting"
        super().__init__(source_id, settings)
        self._frames: Mailbox[np.ndarray] = Mailbox()
        self._size = (int(settings.get("width", 1280)), int(settings.get("height", 720)))
        self.status = "starting"

    # What the camera is doing comes from the shared feed while this source is attached to one.
    @property
    def status(self) -> str:
        return self._feed.status if self._attached and self._feed is not None else self._status

    @status.setter
    def status(self, value: str) -> None:
        self._status = value

    @property
    def backend(self) -> str:
        return self._feed.backend if self._feed is not None else ""

    @property
    def modes_seen(self) -> list:
        return self._feed.modes_seen if self._feed is not None else []

    @property
    def opened_with(self) -> dict | None:
        return self._feed.opened_with if self._feed is not None else None

    @property
    def open_errors(self) -> list[str]:
        return self._feed.open_errors if self._feed is not None else []

    @property
    def shared(self) -> bool:
        """True while another source uses the same open camera."""
        return self._attached and self._feed is not None and len(self._feed.subscribers) > 1

    def start(self) -> None:
        if self._attached:
            return
        key = _feed_key(self.settings)
        with _feeds_lock:
            feed = _feeds.get(key)
            new = feed is None
            if new:
                old = _closing.pop(key, None)
                feed = _feeds[key] = _Feed(self.id, self.settings)
            feed.subscribers.append(self)
            self._feed, self._attached = feed, True
        if new:
            if old is not None:
                old.join()  # still closing the device after a sleep(): let it finish before reopening
            feed.start()

    def _leave(self, wait: bool) -> None:
        if not self._attached or self._feed is None:
            return
        feed = self._feed
        with _feeds_lock:
            self._attached = False
            if self in feed.subscribers:
                feed.subscribers.remove(self)
            last = not feed.subscribers
            if last:
                key = next((k for k, f in _feeds.items() if f is feed), None)
                if key is not None:
                    del _feeds[key]
                    if not wait:
                        _closing[key] = feed
        if last:
            feed.stop(wait)

    def stop(self) -> None:
        self._leave(wait=True)

    def sleep(self) -> None:
        self._leave(wait=False)  # the camera thread closes the device on its own; don't wait for it here
        self._frames = Mailbox()
        self.status = "paused (not on screen)"

    def wake(self) -> None:
        self.status = "starting"
        self.start()

    def update(self, settings: dict[str, Any]) -> None:
        reopen = _feed_key(settings) != _feed_key(self.settings)
        super().update(settings)
        if not (reopen and self._attached):
            return
        feed = self._feed
        with _feeds_lock:
            alone = feed.subscribers == [self] and _feed_key(settings) not in _feeds
            if alone:  # keep the thread: it closes the camera and opens the new mode in order
                for key in [k for k, f in _feeds.items() if f is feed]:
                    del _feeds[key]
                _feeds[_feed_key(settings)] = feed
                feed.retune(settings)
        if not alone:
            self._leave(wait=False)
            self.start()

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    def video_frame(self) -> np.ndarray | None:
        return self._frames.take()

    def _receive(self, frame: np.ndarray) -> None:
        """A frame from the feed's capture thread. The array is shared with other sources: read only."""
        if self.settings.get("mirror"):
            frame = np.ascontiguousarray(frame[:, ::-1])
        self._size = (frame.shape[1], frame.shape[0])
        self._frames.put(frame)
