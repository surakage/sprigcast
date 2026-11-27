"""SprigCast Camera: shows a canvas as a webcam in TikTok LIVE Studio, Zoom, Discord, browsers...

The camera is native/camera/sprigcast_camera.dll (plus sprigcast_camera32.dll for 32-bit apps). Setting it
up copies them to Program Files and registers them, once, with an administrator prompt. That adds
"SprigCast Camera" to DirectShow's video capture sources, the way OBS's virtual camera works, so every app
that lists webcams sees it while it is set up. The DLL also holds a Windows 11 media source
(MFCreateVirtualCamera), used only with an older camera that has no DirectShow part; Windows refuses that
kind on some PCs.

Frames travel through a shared-memory block the camera creates while an app is watching (layout in
sprigcast_camera.cpp; Local\\SprigCastCamera for DirectShow). The camera writes the size it wants and a
heartbeat; SprigCast then taps what Settings says to show at that size as NV12, scaled on the GPU, and
copies frames in. When nothing watches, SprigCast does no camera work at all.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

NAME = "SprigCast Camera"
CLSID = "{6F3C5A0E-2B9D-4C6B-9F4E-5A1D7C2E8B31}"  # the Windows 11 media source
DSHOW_CLSID = "{622E03C7-86C4-4F91-82C0-6EB79B42DCBF}"  # the DirectShow camera (what apps list, like OBS's)
SHARED_NAME = "Global\\SprigCastCamera"  # made by the media source (Windows' camera service)
LOCAL_SHARED_NAME = "Local\\SprigCastCamera"  # made by the DirectShow camera inside the app using it
MAGIC = 0x43565053
HEADER = 64
MAX_W = 1920
BLOCK_SIZE = HEADER + MAX_W * MAX_W * 3 // 2
STALE_MS = 2000  # no heartbeat for this long: nobody is watching
# An app that opens the camera twice at two sizes (TikTok LIVE Studio: 1080x1920 and 1920x1080), or two apps
# at two sizes, have two copies of the camera writing their size into the one block in turn. The size being
# fed stays while it was asked for within this long. Two copies writing 30 times a second can fall into step
# so that one of them is the last writer for seconds on end: with 2 s the camera still flipped shape every
# ten seconds or so in the 60-minute soak (670 log lines an hour). The copy at the other size keeps its
# last picture until the first one closes.
STILL_WANTED_MS = 15_000
DLL_NAME = "sprigcast_camera.dll"
DLL32_NAME = "sprigcast_camera32.dll"  # for 32-bit apps


# Where things are -------------------------------------------------------------------------

def bundled_dll(name: str = DLL_NAME) -> Path | None:
    """The DLL shipped with SprigCast (installer), or a local build (native/camera → build/camera[32])."""
    here = Path(__file__).resolve().parent.parent
    build = "camera32" if name == DLL32_NAME else "camera"
    for path in (here / "camera" / name, here.parent.parent / "build" / build / "Release" / name):
        if path.exists():
            return path
    return None


def install_dir() -> Path:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / NAME


def windows_build() -> int:
    from sprigcast.winver import windows_build as real_build

    return real_build()


def supported() -> tuple[bool, str]:
    """(can this PC have the camera, why not)."""
    if sys.platform != "win32":
        return False, "The SprigCast camera needs Windows."
    if bundled_dll() is None and registered_path() is None:  # a dev checkout can use the installed camera
        return False, "This copy of SprigCast came without the camera. Reinstall from the latest installer."
    return True, ""


def registered_path(clsid: str = CLSID) -> Path | None:
    """The DLL Windows will load for the camera, if it has been set up."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"Software\Classes\CLSID\{clsid}\InprocServer32") as key:
            value, _ = winreg.QueryValueEx(key, "")
    except OSError:
        return None
    path = Path(value)
    return path if path.exists() else None


def has_directshow() -> bool:
    """Set up with a camera new enough to be a DirectShow camera (apps list it while it's registered)."""
    return registered_path(DSHOW_CLSID) is not None


class _State:
    privacy_failed = False  # the last start failed the way a privacy block does (Settings offers the page)


state = _State()
PRIVACY_PAGE = "ms-settings:privacy-webcam"
PRIVACY_HINT = "Windows blocked the camera."
_CONSENT = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"


def privacy_block(read=None) -> str:
    """Which Windows camera privacy switch is off ('' when none is). SprigCast is a desktop app, and Windows'
    camera service refuses to add a virtual camera for an app that may not use cameras."""
    if read is None:
        if sys.platform != "win32":
            return ""
        read = _read_reg
    checks = (
        ("HKLM", r"Software\Policies\Microsoft\Windows\AppPrivacy", "LetAppsAccessCamera", "2",
         "Your organization turned camera access off."),
        ("HKLM", _CONSENT, "Value", "Deny", "Camera access is off for this PC."),
        ("HKCU", _CONSENT, "Value", "Deny", "Camera access is off."),
        ("HKCU", _CONSENT + r"\NonPackaged", "Value", "Deny", "Desktop apps may not use the camera."),
    )
    for hive, key, name, deny, why in checks:
        if str(read(hive, key, name)) == deny:
            return why
    return ""


def _read_reg(hive: str, key: str, name: str):
    import winreg

    root = winreg.HKEY_LOCAL_MACHINE if hive == "HKLM" else winreg.HKEY_CURRENT_USER
    try:
        with winreg.OpenKey(root, key) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def open_privacy_settings() -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl(PRIVACY_PAGE))


def camera_log() -> Path:
    """Where the camera DLL writes its steps (made writable for Windows' Frame Server when registered)."""
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / NAME / "camera.log"


def installed() -> bool:
    return registered_path() is not None


_compared: dict[tuple, bool] = {}


def _same(a: Path, b: Path) -> bool:
    """Same bytes? Different sizes say no at once; otherwise read once per pair of files as they are now
    (reading both DLLs each time the camera window opened held SprigCast up 0.4 s while live)."""
    sa, sb = a.stat(), b.stat()
    if sa.st_size != sb.st_size:
        return False
    key = (str(a), sa.st_mtime_ns, str(b), sb.st_mtime_ns, sa.st_size)
    if key not in _compared:
        _compared[key] = a.read_bytes() == b.read_bytes()
    return _compared[key]


_versions: dict[tuple, int | None] = {}
_FIXED_INFO = b"\xbd\x04\xef\xfe"  # VS_FIXEDFILEINFO's signature, 0xFEEF04BD, as it sits in the file


def camera_version(path: Path) -> int | None:
    """SprigCast Camera's own version (native/camera/version.h, FILEVERSION's first number), read from the
    DLL's version resource; None for a camera built before it had one (2026-11-27). Read once per file as it
    is now. Works on any system (it reads the bytes), so Setup's and the app's checks can be tested."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key not in _versions:
        data = path.read_bytes()
        at = data.find(_FIXED_INFO)
        found = None
        if at >= 0 and at + 12 <= len(data):
            file_version_ms = int.from_bytes(data[at + 8:at + 12], "little")
            found = file_version_ms >> 16 or None
        _versions[key] = found
    return _versions[key]


def _older(src: Path, dst: Path) -> bool:
    """Is the installed `dst` older than the bundled `src`? By the camera's own version when this build has one:
    the bytes differ on every build even when nothing changed, so comparing them asked for Windows' permission
    on every update (PC, test builds 6 and 7, 2026-11-27). A camera with no version is older than one with."""
    shipped = camera_version(src)
    if shipped is None:  # a build without the version (a developer's): same bytes or not
        return not _same(src, dst)
    have = camera_version(dst)
    return have is None or have < shipped


def needs_update() -> bool:
    """Set up, but with an older camera than the one this SprigCast ships."""
    src, dst = bundled_dll(), registered_path()
    if not src or not dst:
        return False
    src32, dst32 = bundled_dll(DLL32_NAME), dst.with_name(DLL32_NAME)
    try:
        if _older(src, dst):
            return True
        return bool(src32) and (not dst32.exists() or _older(src32, dst32))
    except OSError:
        return False


_looked = False


def look_ahead() -> None:
    """Compare the DLLs on a worker (once, at start), so the camera window opens without reading them."""
    global _looked
    if sys.platform == "win32" and not _looked:
        _looked = True
        from sprigcast.core import workers

        workers.run(needs_update, name="camera-dll")


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
    steps = [f'mkdir "{target}" 2>nul', _install_step(src, target / DLL_NAME)]
    src32 = bundled_dll(DLL32_NAME)
    if src32:
        steps.append(_install_step(src32, target / DLL32_NAME))  # regsvr32 hands a 32-bit DLL to its 32-bit self
    return _elevated(" & ".join(steps))


def _install_step(src: Path, dll: Path, stamp: str | None = None) -> str:
    """cmd steps that put one DLL in place and register it, even while an app has the old one loaded.

    A loaded DLL can't be overwritten or deleted, but it can be renamed. So: clear out earlier
    leftovers that are no longer loaded, stage the new copy, rename the current DLL aside under a name
    that's new every time (a fixed ".old" name may itself still be loaded, which made the rename fail
    and left the old DLL in place), move the new one in, and always register what's there.
    """
    if stamp is None:
        import uuid

        stamp = uuid.uuid4().hex[:8]
    new = f"{dll}.new"
    return (f'del /q "{dll}.*.old" "{dll}.old" "{dll.parent}\\*.inuse-*" 2>nul & '
            f'copy /y "{src}" "{new}" && (move /y "{dll}" "{dll}.{stamp}.old" 2>nul & move /y "{new}" "{dll}") & '
            f'regsvr32 /s "{dll}"')


def remove() -> bool:
    dll, dll32 = install_dir() / DLL_NAME, install_dir() / DLL32_NAME
    return _elevated(f'regsvr32 /u /s "{dll}" & regsvr32 /u /s "{dll32}" 2>nul')


# The shared block -----------------------------------------------------------------------------

class SharedBlock:
    """The camera's shared memory, at a raw address (a Windows file mapping, or a test buffer)."""

    def __init__(self, address: int, size: int = BLOCK_SIZE, close=None, name: str = ""):
        self.address, self.size = address, size
        self.name = name  # the mapping's name: the native engine maps it itself (VirtualCamera.link_camera)
        self._close = close
        # The render thread copies frames in while the UI thread may close it (camera turned off): closing
        # waits for a copy in progress, and nothing is read or written once it's closed (unmapped memory).
        self._lock = threading.Lock()
        self.closed = False

    def _u32(self, offset: int) -> ctypes.c_uint32:
        return ctypes.c_uint32.from_address(self.address + offset)

    def _u64(self, offset: int) -> ctypes.c_uint64:
        return ctypes.c_uint64.from_address(self.address + offset)

    @property
    def valid(self) -> bool:
        with self._lock:
            return not self.closed and self._u32(0).value == MAGIC

    def wanted(self) -> tuple[int, int]:
        with self._lock:
            return (0, 0) if self.closed else (self._u32(8).value, self._u32(12).value)

    def heartbeat(self) -> int:
        with self._lock:
            return 0 if self.closed else self._u64(32).value

    def write(self, width: int, height: int, nv12) -> bool:
        """Copy one NV12 frame (bytes or a numpy array) in (seqlock: the count is odd while writing)."""
        n = width * height * 3 // 2
        if len(nv12) < n or HEADER + n > self.size:
            return False
        with self._lock:
            if self.closed:
                return False
            self._copy(width, height, nv12, n)
        return True

    def _copy(self, width: int, height: int, nv12, n: int) -> None:
        seq = self._u64(24)
        seq.value += 1
        self._u32(16).value, self._u32(20).value = width, height
        # A numpy array is copied straight from its own memory, with no bytes copy in between. So is a bytearray
        # (the native engine's frames): ctypes refuses one as a pointer, and on the PC (2026-11-17) every native
        # frame failed here, leaving the camera on its "no picture" green.
        if isinstance(nv12, bytearray) or (isinstance(nv12, memoryview) and not nv12.readonly):
            source = ctypes.addressof((ctypes.c_char * n).from_buffer(nv12))
        elif isinstance(nv12, memoryview):
            source = bytes(nv12[:n])
        else:
            source = getattr(getattr(nv12, "ctypes", None), "data", nv12)
        ctypes.memmove(self.address + HEADER, source, n)
        seq.value += 1

    def close(self) -> None:
        with self._lock:
            self.closed = True
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
    handle = None
    for name in (LOCAL_SHARED_NAME, SHARED_NAME):  # the DirectShow camera first, then the media source
        handle = k32.OpenFileMappingW(access, False, name)
        if handle:
            break
    if not handle:
        return None
    view = k32.MapViewOfFile(handle, access, 0, 0, 0)
    if not view:
        k32.CloseHandle(handle)
        return None

    def close():
        k32.UnmapViewOfFile(view)
        k32.CloseHandle(handle)
    return SharedBlock(view, BLOCK_SIZE, close, name)


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
        # "bloom:<scene id>" or "source:<source id>" (Settings → Video → Virtual camera)
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
        self.privacy = False  # the last start failed because of Windows camera privacy
        self._retired: SharedBlock | None = None  # unmapped a poll later, so a frame mid-copy can't fault
        self.error = ""
        self.frames = 0
        self._asked: dict[tuple[int, int], int] = {}  # camera size -> when an app last asked for it (ms)

    # On and off ------------------------------------------------------------------------

    def start(self) -> bool:
        """Add SprigCast Camera to Windows for as long as SprigCast runs."""
        path = registered_path()
        if path is None:
            self.error = "Set up the camera first."
            return False
        if has_directshow():
            # A DirectShow camera is always listed once set up (like OBS's); SprigCast only feeds it frames.
            # Adding the Windows 11 media source too would list "SprigCast Camera" twice.
            self.error, self.privacy = "", False
            state.privacy_failed = False
            log.info("virtual camera on (DirectShow)")
            return True
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
            # Access denied, or the camera service gave up on it (MF_E_SHUTDOWN): Windows itself said no. A privacy
            # switch is one cause; some Windows builds refuse virtual cameras with every switch on.
            self.privacy = bool(privacy_block()) or (hr & 0xFFFFFFFF) in (0x80070005, 0xC00D3E85)
            state.privacy_failed = self.privacy
            if needs_update():
                self.privacy = False
                self.error += " Click Update the camera, then restart SprigCast."
            elif self.privacy:
                self.error = PRIVACY_HINT
            log.warning("virtual camera: SC_Start failed 0x%08X at step %s; in-process self test %s; "
                        "camera log: %s", hr & 0xFFFFFFFF, self._stage(), self._self_test(), camera_log())
            return False
        self.error, self.privacy = "", False
        state.privacy_failed = False
        log.info("virtual camera on")
        return True

    def _self_test(self) -> str:
        """Can this process make and describe the camera's source itself (newer DLLs only)?"""
        try:
            test = self._dll.SC_SelfTest
        except (AttributeError, OSError):
            return "unavailable"
        test.restype = ctypes.c_long
        hr = test()
        return "ok" if hr >= 0 else f"0x{hr & 0xFFFFFFFF:08X}"

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

    # The native engine writes the frames itself (render/native_bridge.py link_taps) ------------------------------

    linked = False

    def link_camera(self):
        """(block name, camera width, camera height) for the native engine to write this camera's frames into the
        block itself, on its own thread: no Python per frame. None while it can't (no app watching, a test's block
        with no name). Asked on the render thread each tick."""
        block, tap = self.block, self.tap
        if block is None or tap is None or block.closed or not block.name or os.environ.get("SPRIGCAST_NO_LINK"):
            return None
        return block.name, tap[2][0], tap[2][1]

    def unlinked(self, _last_pts: int = -1) -> None:
        """The engine stopped writing the frames: they come through _on_frame again."""
        self.linked = False

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
        self._asked[(width, height)] = self._clock()
        if self.tap is not None and (width, height) != self.tap[2] and self._still_asked(self.tap[2]):
            width, height = self.tap[2]  # another copy of the camera at another size: keep feeding this one
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

    def _still_asked(self, size: tuple[int, int]) -> bool:
        return self._clock() - self._asked.get(size, -STILL_WANTED_MS) < STILL_WANTED_MS

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
        self._asked[block.wanted()] = self._clock()  # every frame: catches both copies of a camera opened twice
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
        return buf  # reused every frame: write() copies it straight into the camera


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
