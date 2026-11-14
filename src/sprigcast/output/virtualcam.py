"""SprigCast Camera: shows a canvas as a webcam in Zoom, Teams, Discord, the Camera app...

Windows 11 only. The camera itself is native/camera/sprigcast_camera.dll, a media source that Windows'
Frame Server runs. Setting it up copies the DLL to Program Files and registers it, once, with an
administrator prompt. After that SprigCast adds the camera while it runs (SC_Start) and removes it when
it closes.

Frames travel through a shared-memory block the camera creates while an app is watching
(layout in sprigcast_camera.cpp). The camera writes the size it wants and a heartbeat; SprigCast then
taps the canvas whose shape matches (landscape: horizontal, portrait: vertical) as NV12 at that size,
scaled on the GPU, and copies frames in. When nothing watches, SprigCast does no camera work at all.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

NAME = "SprigCast Camera"
CLSID = "{6F3C5A0E-2B9D-4C6B-9F4E-5A1D7C2E8B31}"
SHARED_NAME = "Global\\SprigCastCamera"
MAGIC = 0x43565053
HEADER = 64
MAX_W = 1920
BLOCK_SIZE = HEADER + MAX_W * MAX_W * 3 // 2
STALE_MS = 2000  # no heartbeat for this long: nobody is watching
DLL_NAME = "sprigcast_camera.dll"


# Where things are -------------------------------------------------------------------------

def bundled_dll() -> Path | None:
    """The DLL shipped with SprigCast (installer), or a local build (native/camera → build/camera)."""
    here = Path(__file__).resolve().parent.parent
    for path in (here / "camera" / DLL_NAME,
                 here.parent.parent / "build" / "camera" / "Release" / DLL_NAME):
        if path.exists():
            return path
    return None


def install_dir() -> Path:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / NAME


def windows_build() -> int:
    if sys.platform != "win32":
        return 0
    try:
        return sys.getwindowsversion().build
    except Exception:  # noqa: BLE001
        return 0


def supported() -> tuple[bool, str]:
    """(can this PC have the camera, why not)."""
    if sys.platform != "win32":
        return False, "The SprigCast camera needs Windows 11."
    if windows_build() < 22000:
        return False, "The SprigCast camera needs Windows 11 (this PC has Windows 10)."
    if bundled_dll() is None:
        return False, "This copy of SprigCast came without the camera. Reinstall from the latest installer."
    return True, ""


def registered_path() -> Path | None:
    """The DLL Windows will load for the camera, if it has been set up."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"Software\Classes\CLSID\{CLSID}\InprocServer32") as key:
            value, _ = winreg.QueryValueEx(key, "")
    except OSError:
        return None
    path = Path(value)
    return path if path.exists() else None


def installed() -> bool:
    return registered_path() is not None


def needs_update() -> bool:
    """Set up, but with an older DLL than the one this SprigCast ships."""
    src, dst = bundled_dll(), registered_path()
    if not src or not dst:
        return False
    try:
        return src.read_bytes() != dst.read_bytes()
    except OSError:
        return False


def _elevated(command: str) -> bool:
    """Run a cmd.exe command as administrator (Windows asks first). True if it started."""
    shell = ctypes.windll.shell32  # type: ignore[attr-defined]
    result = shell.ShellExecuteW(None, "runas", "cmd.exe", f"/c {command}", None, 0)
    return result > 32


def set_up() -> bool:
    """Copy the camera to Program Files and register it (one administrator prompt)."""
    src = bundled_dll()
    if src is None:
        return False
    target = install_dir()
    dll = target / DLL_NAME
    # A loaded DLL can't be overwritten: unregister first, rename the old file aside, then copy.
    return _elevated(f'mkdir "{target}" 2>nul & regsvr32 /u /s "{dll}" 2>nul & '
                     f'move /y "{dll}" "{dll}.old" 2>nul & copy /y "{src}" "{dll}" && regsvr32 /s "{dll}"')


def remove() -> bool:
    dll = install_dir() / DLL_NAME
    return _elevated(f'regsvr32 /u /s "{dll}"')


# The shared block -----------------------------------------------------------------------------

class SharedBlock:
    """The camera's shared memory, at a raw address (a Windows file mapping, or a test buffer)."""

    def __init__(self, address: int, size: int = BLOCK_SIZE, close=None):
        self.address, self.size = address, size
        self._close = close

    def _u32(self, offset: int) -> ctypes.c_uint32:
        return ctypes.c_uint32.from_address(self.address + offset)

    def _u64(self, offset: int) -> ctypes.c_uint64:
        return ctypes.c_uint64.from_address(self.address + offset)

    @property
    def valid(self) -> bool:
        return self._u32(0).value == MAGIC

    def wanted(self) -> tuple[int, int]:
        return self._u32(8).value, self._u32(12).value

    def heartbeat(self) -> int:
        return self._u64(32).value

    def write(self, width: int, height: int, nv12: bytes) -> bool:
        """Copy one NV12 frame in (seqlock: the count is odd while writing)."""
        n = width * height * 3 // 2
        if len(nv12) < n or HEADER + n > self.size:
            return False
        seq = self._u64(24)
        seq.value += 1
        self._u32(16).value, self._u32(20).value = width, height
        ctypes.memmove(self.address + HEADER, nv12, n)
        seq.value += 1
        return True

    def close(self) -> None:
        if self._close:
            self._close()
            self._close = None


def open_block() -> SharedBlock | None:
    """The camera's block if an app has the camera open right now, else None."""
    if sys.platform != "win32":
        return None
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k32.OpenFileMappingW.restype = ctypes.c_void_p
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
    k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    access = 0x0002 | 0x0004  # FILE_MAP_WRITE | FILE_MAP_READ
    handle = k32.OpenFileMappingW(access, False, SHARED_NAME)
    if not handle:
        return None
    view = k32.MapViewOfFile(handle, access, 0, 0, 0)
    if not view:
        k32.CloseHandle(handle)
        return None

    def close():
        k32.UnmapViewOfFile(view)
        k32.CloseHandle(handle)
    return SharedBlock(view, BLOCK_SIZE, close)


def now_ms() -> int:
    if sys.platform == "win32":
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        k32.GetTickCount64.restype = ctypes.c_uint64
        return int(k32.GetTickCount64())
    import time

    return int(time.monotonic() * 1000)


# The camera while SprigCast runs ----------------------------------------------------------------

class VirtualCamera:
    """Adds the camera (SC_Start) and feeds it frames while an app watches.

    poll() runs once a second on the UI thread; frames are copied on the render thread.
    """

    def __init__(self, engine, canvases, open_block=open_block, clock=now_ms, choice=lambda: "auto",
                 scenes=lambda: []):
        self.engine = engine
        self.canvases = canvases  # callable: the collection's canvases (id, width, height, enabled)
        # callable: what to show: "auto" (the canvas shaped like the app's request), "canvas:<id>",
        # "bloom:<scene id>" or "source:<source id>" (Settings → Output → Virtual camera)
        self.choice = choice
        self.scenes = scenes  # callable: the collection's scenes (id, canvas), to find a bloom's canvas
        self._pad = None  # (size, numpy NV12 buffer) for letter/pillarboxing a picture of another shape
        self._open_block = open_block
        self._clock = clock
        self._dll = None
        self.block: SharedBlock | None = None
        # (tap id, picture size, camera size): "horizontal", "view:camera"...; picture fits inside camera size
        self.tap: tuple[str, tuple[int, int], tuple[int, int]] | None = None
        self._view = self._shown = None  # (canvas id, "bloom"|"source", ref) for a view tap
        self._retired: SharedBlock | None = None  # unmapped a poll later, so a frame mid-copy can't fault
        self.error = ""
        self.frames = 0

    # On and off ------------------------------------------------------------------------

    def start(self) -> bool:
        """Add SprigCast Camera to Windows for as long as SprigCast runs."""
        path = registered_path()
        if path is None:
            self.error = "Set up the camera first."
            return False
        try:
            self._dll = ctypes.WinDLL(str(path))  # type: ignore[attr-defined]
            self._dll.SC_Start.argtypes = [ctypes.c_wchar_p]
            self._dll.SC_Start.restype = ctypes.c_long
            hr = self._dll.SC_Start(NAME)
        except OSError as exc:
            self.error = f"Couldn't load the camera: {exc}"
            return False
        if hr < 0:
            self.error = f"Windows refused the camera (0x{hr & 0xFFFFFFFF:08X})."
            log.warning("virtual camera: SC_Start failed 0x%08X at step %s", hr & 0xFFFFFFFF, self._stage())
            return False
        self.error = ""
        log.info("virtual camera on")
        return True

    def _stage(self) -> str:
        """Which step of SC_Start went wrong (newer DLLs only), for the log."""
        try:
            stage = self._dll.SC_Stage
        except (AttributeError, OSError):
            return "?"
        stage.restype = ctypes.c_uint32
        return {1: "startup", 2: "create", 3: "start"}.get(stage(), str(stage()))

    def stop(self) -> None:
        self._untap()
        if self.block is not None:
            self.block.close()
            self.block = None
        if self._dll is not None:
            try:
                self._dll.SC_Stop()
            except OSError:
                pass
            self._dll = None
            log.info("virtual camera off")

    @property
    def watched(self) -> bool:
        return self.tap is not None

    # Once a second ------------------------------------------------------------------------

    def poll(self) -> None:
        if self._retired is not None:
            self._retired.close()
            self._retired = None
        if self.block is None:
            self.block = self._open_block()
            if self.block is None:
                return
        block = self.block
        width, height = block.wanted()
        fresh = block.valid and width and height and self._clock() - block.heartbeat() < STALE_MS
        if not fresh:
            self._untap()
            if not block.valid or not (width and height):
                self._retired, self.block = block, None  # the camera closed it; open again when watched
            return
        want = self._want(width, height)
        if want != self.tap or self._view != self._shown:  # a bloom or source view keeps the same tap id
            self._untap()
            if want is not None:
                tap_id, picture, _camera = want
                if tap_id == VIEW_TAP:
                    self.engine.set_view(VIEW, *self._view)
                self.engine.add_frame_tap(tap_id, self._on_frame, picture)
                self.tap, self._shown = want, self._view
                log.info("virtual camera: an app is watching at %dx%d, showing %s", width, height, self.choice())

    def _canvas_for(self, width: int, height: int):
        """The canvas with the same shape as what the app asked for, else any canvas that's on."""
        on = [c for c in self.canvases() if getattr(c, "enabled", True)]
        portrait = height > width
        for canvas in on:
            if (canvas.height > canvas.width) == portrait:
                return canvas
        return on[0] if on else None

    def _want(self, width: int, height: int):
        """(tap id, picture size, camera size) for the current choice, or None when there's nothing to show."""
        kind, _, ref = (self.choice() or "auto").partition(":")
        canvases = {c.id: c for c in self.canvases()}
        self._view = None
        if kind == "canvas" and ref in canvases:
            canvas, tap_id = canvases[ref], ref
        elif kind == "bloom":
            scene = next((sc for sc in self.scenes() if sc.id == ref), None)
            canvas = canvases.get(scene.canvas) if scene is not None else None
            if canvas is None:
                return None
            tap_id, self._view = VIEW_TAP, (canvas.id, "bloom", ref)
        elif kind == "source":
            canvas = self._canvas_for(width, height)
            if canvas is None:
                return None
            tap_id, self._view = VIEW_TAP, (canvas.id, "source", ref)
        else:
            canvas = self._canvas_for(width, height)
            if canvas is None:
                return None
            tap_id = canvas.id
        return tap_id, fit_inside((canvas.width, canvas.height), (width, height)), (width, height)

    def _untap(self) -> None:
        if self.tap is not None:
            self.engine.remove_frame_tap(self.tap[0], self._on_frame)
            if self.tap[0] == VIEW_TAP:
                self.engine.set_view(VIEW, None)
            self.tap = self._shown = None
            log.info("virtual camera: nobody watching")

    # Render thread -----------------------------------------------------------------------

    def _on_frame(self, frame) -> None:
        block, tap = self.block, self.tap
        if block is None or tap is None or getattr(frame, "fmt", "") != "nv12":
            return
        width, height = tap[2]
        data = frame.data
        if (frame.width, frame.height) != (width, height):
            data = self._boxed(frame, width, height)
            if data is None:
                return
        if block.write(width, height, data):
            self.frames += 1

    def _boxed(self, frame, width: int, height: int):
        """A picture of another shape in the middle of the camera's frame, with black bars around it."""
        import numpy as np

        fw, fh = frame.width, frame.height
        if fw > width or fh > height:
            return None
        if self._pad is None or self._pad[0] != (width, height):
            buf = np.empty(width * height * 3 // 2, np.uint8)
            buf[: width * height] = 16  # black, limited range
            buf[width * height:] = 128
            self._pad = ((width, height), buf)
        buf = self._pad[1]
        x0, y0 = (width - fw) // 4 * 2, (height - fh) // 4 * 2  # even, so chroma lines up
        src = np.frombuffer(frame.data, np.uint8)
        y = buf[: width * height].reshape(height, width)
        y[y0:y0 + fh, x0:x0 + fw] = src[: fw * fh].reshape(fh, fw)
        uv = buf[width * height:].reshape(height // 2, width)
        uv[y0 // 2:(y0 + fh) // 2, x0:x0 + fw] = src[fw * fh: fw * fh * 3 // 2].reshape(fh // 2, fw)
        return buf.tobytes()


VIEW = "camera"
VIEW_TAP = f"view:{VIEW}"


def fit_inside(picture: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """The biggest even size with the picture's shape that fits in the box (the box itself if same shape)."""
    pw, ph = picture
    bw, bh = box
    k = min(bw / max(1, pw), bh / max(1, ph))
    w, h = min(bw, round(pw * k / 2) * 2), min(bh, round(ph * k / 2) * 2)
    if abs(w - bw) <= 2 and abs(h - bh) <= 2:
        return bw, bh
    return max(2, w), max(2, h)
