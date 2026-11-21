"""Webcam and capture-card source through DirectShow.

Frames are read with FFmpeg's DirectShow input (PyAV, already loaded for encoding). OpenCV is only
a fallback, imported if FFmpeg can't open the camera: on its own it adds about 90 MB of memory.

Devices are remembered by their system path and name rather than by index, because Windows
renumbers cameras when one is plugged in or out. A capture thread reads frames as they arrive;
if the camera is unplugged it keeps the last picture and tries to reopen it every two seconds.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from sprigcast import sprigcore
from sprigcast.core.frames import picture_array, reformat
from sprigcast.core.mailbox import Mailbox
from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

RETRY_SECONDS = 2.0

# Sizes offered in the picker. The camera is asked for the smallest mode that covers the size picked.
SIZES = [(1920, 1080), (1280, 720), (640, 480)]


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


OWN_CAMERA = "SprigCast Camera"  # our own virtual camera: showing it in SprigCast would loop the picture


def list_cameras() -> list[CameraDevice]:
    """Cameras visible to DirectShow, in DirectShow's order, without SprigCast's own camera.

    Each keeps its DirectShow index, so leaving ours out doesn't shift the others.
    """
    return [c for c in _all_cameras() if c.name != OWN_CAMERA]


def _all_cameras() -> list[CameraDevice]:
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


_VIRTUAL = ("virtual", "vcam", "camera hub")
_NOT_A_CAMERA = ("capture", "hd60", "4k60", "4k x", "game", "screen")


def _plain(name: str) -> str:
    return name.replace("Ɩ", "l").replace("ı", "i").lower()  # Elgato spells its virtual camera "EƖgato"


def is_virtual(name: str) -> bool:
    """A camera another app makes (Camera Hub, OBS, Broadcast...): it costs that app's GPU work on top."""
    plain = _plain(name)
    return any(word in plain for word in _VIRTUAL) and name != OWN_CAMERA


_CAPTURE = ("capture", "hd60", "4k60", "4k x", "4k s", "live gamer", "avermedia", "magewell")


def is_capture_card(name: str) -> bool:
    """A capture card (Elgato HD60 X, AVerMedia Live Gamer...): a console or second PC, not a face."""
    plain = _plain(name)
    return any(word in plain for word in _CAPTURE) and not is_virtual(name)


def real_camera(cameras: list[CameraDevice], virtual_name: str) -> CameraDevice | None:
    """The physical camera most likely behind a virtual one: a cam of the same brand first, never a
    capture card. None when there's no physical camera."""
    brand = _plain(virtual_name).split()[0] if virtual_name.strip() else ""
    best, best_score = None, 0
    for cam in cameras:
        plain = _plain(cam.name)
        if is_virtual(cam.name) or cam.name == OWN_CAMERA or any(w in plain for w in _NOT_A_CAMERA):
            continue
        score = 2 if "cam" in plain else 1
        score += 2 if brand and plain.split()[:1] == [brand] else 0
        if score > best_score:
            best, best_score = cam, score
    return best


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
    """A frame rate for FFmpeg's dshow, which matches a mode by its frame interval (100 ns units).

    A camera listed at fps=59.9999 (the Elgato Virtual Camera) has an interval of 166667; "60" and
    "59.9999" both reach dshow as 166666, which it refused, so the camera fell back to its default
    mode (YUYV, a third more data than its NV12). Whole rates stay as they are; others are written
    as the interval itself.
    """
    if value == round(value):
        return str(round(value))
    return f"10000000/{max(1, round(10_000_000 / value))}"


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
    def exact_rate(m) -> str:  # the rate asked when the mode really covers it, else the mode's own nearest
        return str(fps) if m[4] <= fps <= m[5] else _rate(min(max(fps, m[4]), m[5]))

    for group, rate in ((exact, exact_rate), (others, lambda m: _rate(m[5]))):
        for m in sorted(group, key=lambda m: _FORMAT_ORDER.get(m[1], 9)):
            option = {"video_size": f"{size[0]}x{size[1]}", "framerate": rate(m)}
            option["vcodec" if m[0] == "vcodec" else "pixel_format"] = m[1]
            if option not in out:
                out.append(option)
    # Some cameras (the Elgato Virtual Camera) refuse every frame rate asked of them, even their own
    # listed interval, yet open in a listed size and format when no rate is asked: try those next,
    # so NV12 still beats the camera's default YUYV.
    for option in list(out):
        plain = {k: v for k, v in option.items() if k != "framerate"}
        if plain not in out:
            out.append(plain)
    return out


_BYTES_PER_PIXEL = {"nv12": 1.5, "yuyv422": 2.0, "mjpeg": 0.3}  # mjpeg: compressed, roughly


def mode_cost(option: dict[str, str], fps: int) -> float:
    """Roughly how many bytes a second a dshow option takes in (copied, converted, shrunk)."""
    w, h = (int(v) for v in option["video_size"].split("x"))
    fmt = option.get("pixel_format") or option.get("vcodec", "")
    num, _, den = option.get("framerate", str(fps)).partition("/")
    rate = float(num) / float(den) if den else float(num)
    return w * h * _BYTES_PER_PIXEL.get(fmt, 3.0) * rate


def plain_mode(option: dict[str, str]) -> str:
    """e.g. "1920 x 1080 at 60 fps (NV12)"."""
    w, h = option["video_size"].split("x")
    fmt = (option.get("pixel_format") or option.get("vcodec", "")).upper().replace("YUYV422", "YUY2")
    if "framerate" not in option:
        return f"{w} x {h} at its own rate ({fmt})" if fmt else f"{w} x {h} at its own rate"
    num, _, den = option["framerate"].partition("/")
    rate = round(float(num) / float(den) if den else float(num))
    return f"{w} x {h} at {rate} fps ({fmt})" if fmt else f"{w} x {h} at {rate} fps"


def shrunk_size(src: tuple[int, int], want: tuple[int, int]) -> tuple[int, int] | None:
    """The size to shrink camera frames to so they fit `want` (aspect kept, even numbers), or None."""
    scale = min(want[0] / src[0], want[1] / src[1])
    if scale >= 0.95:
        return None
    return max(2, round(src[0] * scale / 2) * 2), max(2, round(src[1] * scale / 2) * 2)


PACKS_AS_IS = ("yuv420p", "yuvj420p", "nv12")  # pixel formats media.pack_i420 takes without a conversion
PLANAR = not os.environ.get("SPRIGCAST_CAMERA_RGB")  # escape hatch: BGR from the CPU, as before


def camera_picture(frame, target: tuple[int, int] | None) -> tuple[np.ndarray, str, tuple[int, int]]:
    """A decoded camera frame for the GPU: (array, channel_order, picture size).

    The camera's own YUV planes go up as they are (4:2:0, like video files: see media.pack_i420) and the
    GPU turns them into colour, so the CPU no longer converts each frame to BGR. That is half the bytes to
    copy and upload (1080p: 3 MB a frame instead of 6) and most of the camera's CPU time.
    """
    w, h = target or (frame.width, frame.height)
    if PLANAR and w % 2 == 0 and h % 2 == 0:
        from sprigcast.sources.media import pack_i420

        name = frame.format.name
        full = name.startswith("yuvj") or int(getattr(frame, "color_range", 0) or 0) == 2
        # 4:2:0 the camera already sends (NV12 raw, or MJPEG's yuvj420p) is packed as it is: converting it to
        # yuv420p first was one more whole-picture pass and copy per frame (1080p60: 180 MB a second) for nothing.
        if name not in PACKS_AS_IS or (frame.width, frame.height) != (w, h):
            # MJPEG decodes to full-range YUV: keep that range rather than squeeze it (it's labelled below).
            frame = reformat(frame, width=w, height=h, format="yuv420p", interpolation="BILINEAR",
                             **({"src_color_range": 2, "dst_color_range": 2} if full else {}))
        packed = pack_i420(frame, (w, h))
        if packed is not None:
            array, order = packed
            if full and order.endswith("/tv"):
                order = order[:-3] + "/pc"
            return array, order, (w, h)
    picture = picture_array(frame, format="bgr24", **({"width": w, "height": h, "interpolation": "BILINEAR"}
                                                      if target else {}))
    return picture, "BGR", (picture.shape[1], picture.shape[0])


def mirror_picture(frame: np.ndarray, order: str, size: tuple[int, int]) -> np.ndarray:
    """Flip a camera picture left to right, whether BGR or packed 4:2:0 planes (Y, then U | V rows)."""
    if not order.startswith("I420"):
        return np.ascontiguousarray(frame[:, ::-1])
    w, h = size
    cw, ch = w // 2, h // 2
    out = np.empty_like(frame)
    out[:h] = frame[:h, ::-1]
    out[h:h + ch, :cw] = frame[h:h + ch, :cw][:, ::-1]
    out[h:h + ch, cw:] = frame[h:h + ch, cw:][:, ::-1]
    if frame.shape[0] > h + ch:
        out[h + ch:] = frame[h + ch:, ::-1]
    return out


def _mode_of(settings: dict[str, Any]) -> tuple[int, int, int]:
    return int(settings.get("width", 1280)), int(settings.get("height", 720)), int(settings.get("fps", 30))


class _Feed:
    """One open camera, shared by every camera source set to the same device, whatever mode each asks for.

    Two sources showing the same webcam (a wide shot in one scene, a cropped one in another) used to
    open the device twice: two capture threads, two decoders, two sets of frames. Now they subscribe
    to one feed and each gets the same frame. Sources of one device in different modes shared nothing
    either: the PC's Gameplay bloom (2026-11-21) had the HD60 X saved at 1080p60 and at 720p30, so it
    was opened twice; the second open was refused every mode the device offers while the first ran
    ("Could not set video options" 15 times) and came up in its own mode, and SprigCast went from 10%
    to 24% of the PC. Now the feed opens the device once, in the biggest size and fastest rate any of
    its sources asks for (see widen), and each source shows that picture at its own size.
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
        # With the native engine its own player reads the camera (sprigcore.use_native_camera): render/engine.py
        # hands it to the engine, and no picture passes through Python. None while Python reads it.
        self.player: sprigcore.MediaPlayer | None = None
        self._engine_refused = False  # the engine's player couldn't: Python reads it from now on

    def start(self, after: "_Feed | None" = None) -> None:
        """after: the same camera still closing after a sleep(); this feed opens it once that's done."""
        self._thread = threading.Thread(target=self._run, args=(after,), name=f"camera-{self.id}", daemon=True)
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
        """New device or mode for the same (only) subscriber: close and reopen on the capture thread. Only the
        mirror changed, the engine's player flips its next pictures without reopening."""
        same = {k: v for k, v in settings.items() if k != "mirror"} == {
            k: v for k, v in self.settings.items() if k != "mirror"}
        self.settings = dict(settings)
        player = self.player
        if same and player is not None:
            try:
                player.set_mirror(bool(settings.get("mirror")))
                return
            except sprigcore.NativeError:
                pass
        self._restart.set()

    # Capture thread ------------------------------------------------------------------

    def _mode(self) -> tuple[int, int, int]:
        return _mode_of(self.settings)

    def widen(self) -> bool:
        """Grow the mode to the biggest size and fastest rate its sources ask for, reopening the camera if that
        grew (never shrinks while shared: a source going to sleep and back would reopen it each time)."""
        w, h, fps = self._mode()
        modes = [_mode_of(s.settings) for s in list(self.subscribers)]
        big = max([(w, h, fps), *modes], key=lambda m: m[0] * m[1])
        want = (big[0], big[1], max([fps, *(m[2] for m in modes)]))
        if want == (w, h, fps):
            return False
        log.info("%s: one camera for %d sources, now opened at %dx%d %d fps (was %dx%d %d fps)",
                 self.id, len(modes), *want, w, h, fps)
        self.settings = {**self.settings, "width": want[0], "height": want[1], "fps": want[2]}
        self._restart.set()
        return True

    def _run(self, after: "_Feed | None" = None) -> None:
        if after is not None:
            after.join()  # here, not on the render thread: waiting there froze the picture
        while not self._stop.is_set():
            self._restart.clear()
            device = find_camera(str(self.settings.get("name", "")), str(self.settings.get("path", "")))
            if device is not None and not self._engine_refused and sprigcore.use_native_camera():
                self._run_engine(device)
                if not self._engine_refused:
                    continue  # stopped, retuned, or the engine's player gave up on this camera
            opened = device is not None and (self._run_ffmpeg(device) or self._run_opencv(device))
            if self._stop.is_set() or self._restart.is_set():
                continue
            if not opened:
                picked = self.settings.get("name") or self.settings.get("path")
                if device is None:  # a Video Capture just added from + Add has none picked yet
                    self.status = "camera not found, retrying" if picked else "no camera picked"
            self._restart.wait(RETRY_SECONDS)

    def _deliver(self, frame: np.ndarray, order: str = "BGR", size: tuple[int, int] | None = None,
                 full: tuple[int, int] | None = None) -> None:
        """full: the picture's size before it was made smaller for being shown small (sources keep it)."""
        frame = np.ascontiguousarray(frame)
        for source in list(self.subscribers):
            source._receive(frame, order, size, full)

    def shrink(self) -> float:
        """The least any source of this camera may be shrunk (one shown big keeps it full size for all). A source
        asking a smaller mode than the feed's is already that much smaller."""
        width = max(1, self._mode()[0])
        return max((min(1.0, getattr(s, "shrink", 1.0) * min(1.0, _mode_of(s.settings)[0] / width))
                    for s in list(self.subscribers)), default=1.0)

    # The engine's own player (SPRIGCAST_ENGINE=native) ----------------------------------

    def _run_engine(self, device: CameraDevice) -> None:
        """Read the camera in the engine's player until stopped or retuned. It reopens an unplugged or busy camera
        by itself every 2 s; if it can't be made at all, or faults, Python reads the camera instead."""
        w, h, fps = self._mode()
        try:
            format_name, choices = _engine_choices(self, device)
            player = sprigcore.MediaPlayer(playing=True)
        except (sprigcore.NativeError, OSError) as exc:
            log.info("%s: the engine can't read cameras (%s); Python reads it", self.id, exc)
            self._engine_refused = True
            return
        try:
            player.set_picture("", (w, h))  # a camera bigger than the mode asked for is shrunk while decoding
            player.set_device(format_name, choices, fps, bool(self.settings.get("mirror")))
        except sprigcore.NativeError as exc:
            player.close()
            log.info("%s: the engine can't read this camera (%s); Python reads it", self.id, exc)
            self._engine_refused = True
            return
        self.backend = "Engine (FFmpeg)"
        self.opened_with = None
        log.info("%s: the engine reads %r", self.id, device.name)
        self.player = player
        lost = None  # what the engine said when the camera went (the log says when it comes back)
        try:
            while not (self._stop.is_set() or self._restart.is_set()):
                info = player.info()
                if info.faulted:
                    log.warning("%s: the engine's camera reader faulted; Python reads it", self.id)
                    self._engine_refused = True
                    break
                text = info.status_text
                if text == "ok" and info.width > 0:
                    self.status = f"capturing {info.width}x{info.height}"
                    for source in list(self.subscribers):
                        source._size = source.fit((info.width, info.height))
                    if lost is not None:
                        log.info("%s: %r is back (%dx%d)", self.id, device.name, info.width, info.height)
                        lost = None
                elif text not in ("ok", "starting"):
                    self.status = text
                    if lost is None:
                        log.warning("%s: %r went away: %s", self.id, device.name, text)
                    lost = text
                player.set_shrink(self.shrink())
                self._restart.wait(0.25)
        finally:
            self.player = None  # the render thread lets go of it; the engine keeps it until then
            player.close()

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
                if not mode and self.open_errors:  # say why its own modes didn't open
                    log.info("%s: modes offered %s; tried first: %s", self.id, self.modes_seen[:4], self.open_errors[:2])
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
            if target is not None:
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
                    base = target or (frame.width, frame.height)
                    shrink = self.shrink()
                    if shrink < 1.0:  # shown small everywhere: made that small here, as videos are
                        from sprigcast.sources.media import shrunk

                        self._deliver(*camera_picture(frame, shrunk(base, shrink)), full=base)
                    else:
                        self._deliver(*camera_picture(frame, target))
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


def _engine_choices(feed: _Feed, device: CameraDevice) -> tuple[str, list[tuple[str, dict]]]:
    """The input device and the (name, options) the engine's player tries, best first: the camera's own modes for
    the size and rate asked for, then guesses from strict to loose (as _Feed._open_ffmpeg)."""
    w, h, fps = feed._mode()
    names = [f"video=@device_pnp_{device.path}"] if device.path.startswith("\\\\?\\") else []
    names.append(f"video={device.name}")
    listed = []
    for name in names:
        feed.modes_seen = dshow_modes(name)
        listed = choose_modes(feed.modes_seen, w, h, fps)
        if feed.modes_seen:
            break
    modes = [*listed,
             {"video_size": f"{w}x{h}", "framerate": str(fps), "vcodec": "mjpeg"},
             {"video_size": f"{w}x{h}", "framerate": str(fps)},
             {"video_size": f"{w}x{h}"},
             {}]
    return "dshow", [(name, mode) for name in names for mode in modes]


_feeds: dict[tuple, _Feed] = {}  # device + mode -> the feed sources share
_closing: dict[tuple, _Feed] = {}  # feeds told to stop without waiting (a source went to sleep)
_feeds_lock = threading.Lock()


_PER_SOURCE = ("pause_off_screen", "width", "height", "fps")  # a shared feed takes the biggest mode (_Feed.widen)


def _feed_key(settings: dict[str, Any]) -> tuple:
    """Sources with the same device share a feed, whatever size and rate each asks for; mirroring is done per
    source (with the engine its player mirrors, so a mirrored source has a feed of its own)."""
    skip = _PER_SOURCE if sprigcore.use_native_camera() else ("mirror", *_PER_SOURCE)
    return tuple(sorted((k, repr(v)) for k, v in settings.items() if k not in skip))


@register("camera")
class CameraSource(Source):
    """Settings: name, path, width, height, fps, mirror (bool), pause_off_screen (bool)."""

    channel_order = "BGR"  # of the picture video_frame() last returned: BGR (OpenCV) or packed YUV planes
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
            if not new:
                feed.widen()
        if new:
            feed.start(after=old)  # old: still closing the device after a sleep(), let it finish first

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
                    # Remembered while it closes, waited for or not (a removed camera closes on the closing
                    # helper now): the same camera opened again meanwhile waits for it on its own thread.
                    _closing[key] = feed
        if last:
            feed.stop(wait)
            if wait and key is not None:
                with _feeds_lock:
                    if _closing.get(key) is feed:
                        del _closing[key]

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
        new_mode = _mode_of(settings) != _mode_of(self.settings)
        super().update(settings)
        if not self._attached:
            return
        feed = self._feed
        if not reopen:
            if new_mode:
                with _feeds_lock:
                    alone = feed.subscribers == [self]
                    if alone:  # its own camera: the mode asked, smaller too
                        feed.retune(settings)
                    else:
                        feed.widen()
            return
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

    def fit(self, picture: tuple[int, int]) -> tuple[int, int]:
        """This source's size for the feed's picture: the picture shrunk to fit the mode this source asks for, as
        if it had the camera to itself (a shared feed may be open at a bigger mode, for another source)."""
        w, h, _ = _mode_of(self.settings)
        return shrunk_size(picture, (w, h)) or picture

    @property
    def engine_player(self) -> sprigcore.MediaPlayer | None:
        """The engine's player reading this camera (with the native engine): render/engine.py hands it over, and
        the render thread then only calls :meth:`engine_tick`."""
        feed = self._feed
        return feed.player if self._attached and feed is not None else None

    def engine_tick(self) -> None:
        """Render thread, on a tick the engine showed the picture itself: nothing to do (the feed keeps the size)."""

    def video_frame(self) -> np.ndarray | None:
        player = self.engine_player
        if player is not None:  # read back for a frame Python draws itself
            try:
                got = player.video_frame()
            except sprigcore.NativeError:
                return None
            if got is None or isinstance(got, str):
                return None
            frame, self.channel_order = got
            return frame
        item = self._frames.take()
        if item is None:
            return None
        frame, self.channel_order = item
        return frame

    shrinks = True  # the renderer says how small it's shown (set_shrink); `size` stays the full picture's
    shrink = 1.0

    def set_shrink(self, shrink: float) -> None:
        """Render thread: shown at most `shrink` of its size; the feed makes its next frames that small (unless
        another source of the same camera is shown bigger)."""
        self.shrink = shrink

    def _receive(self, frame: np.ndarray, order: str = "BGR", size: tuple[int, int] | None = None,
                 full: tuple[int, int] | None = None) -> None:
        """A frame from the feed's capture thread. The array is shared with other sources: read only."""
        size = size or (frame.shape[1], frame.shape[0])
        if self.settings.get("mirror"):
            frame = mirror_picture(frame, order, size)
        self._size = self.fit(full or size)
        self._frames.put((frame, order))
