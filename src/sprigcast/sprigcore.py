"""SprigCast's native engine (native/core, sprigcore.dll), reached from Python.

The engine is moving to C++ one part at a time (docs/native-engine.md): everything that touches frames and sound
runs on the engine's own threads, which never wait on Python's lock (the GIL). Python keeps the screens,
settings, plugins and the Control API and only sends the engine commands.

This module is the only place Python talks to the DLL. ctypes lets go of the GIL for the whole of every call, and
the engine never calls back into Python: its log lines wait in a queue that :func:`drain_log` moves into
SprigCast's own log.

Which engine runs is chosen per start with ``SPRIGCAST_ENGINE`` (``python``, the default, or ``native``). Parts
that have moved check :func:`use_native`; a missing or older DLL always means the Python engine, so the app keeps
working whatever happens to the DLL.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import sys
import weakref
from pathlib import Path

log = logging.getLogger("sprigcast.native")

INTERFACE_VERSION = 2  # include/sprigcore.h SC_INTERFACE_VERSION this module was written for
DLL_NAME = "sprigcore.dll"

SC_OK = 0
DEVICE_WARP = 1
ERRORS = {-1: "bad argument", -2: "graphics device", -3: "shader", -4: "out of memory", -5: "internal",
          -6: "faulted", -7: "graphics card lost"}
FAULTED, DEVICE_LOST = -6, -7  # the device is shut off: make a new one (crash gate)
FORMATS = {"RGBA": 0, "BGRA": 1, "RGB": 2, "BGR": 3, "BGRX": 4, "pBGRA": 5, "I420": 6, "I420A": 7}
MAX_HALOS = 4
_LEVELS = {10: logging.DEBUG, 20: logging.INFO, 30: logging.WARNING, 40: logging.ERROR}
VENDORS = {0x10DE: "NVIDIA", 0x1002: "AMD", 0x1022: "AMD", 0x8086: "Intel", 0x1414: "Microsoft"}


class NativeError(RuntimeError):
    """An engine call failed; the message is the engine's own."""

    def __init__(self, code: int, message: str):
        super().__init__(f"{message or ERRORS.get(code, 'error')} ({code})")
        self.code = code


class AdapterInfo(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_wchar * 128),
        ("vendor_id", ctypes.c_uint32),
        ("device_id", ctypes.c_uint32),
        ("dedicated_video_memory", ctypes.c_uint64),
        ("shared_system_memory", ctypes.c_uint64),
        ("software", ctypes.c_int32),
        ("reserved", ctypes.c_int32),
    ]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "vendor": VENDORS.get(self.vendor_id, f"0x{self.vendor_id:04X}"),
            "vendor_id": self.vendor_id,
            "device_id": self.device_id,
            "dedicated_mb": self.dedicated_video_memory >> 20,
            "shared_mb": self.shared_system_memory >> 20,
            "software": bool(self.software),
        }


class Upload(ctypes.Structure):
    _fields_ = [("format", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int), ("stride", ctypes.c_int),
                ("layout_width", ctypes.c_int), ("layout_height", ctypes.c_int), ("yuv", ctypes.c_float * 3)]


class Halo(ctypes.Structure):
    _fields_ = [("mode", ctypes.c_int), ("color", ctypes.c_float * 3), ("alpha", ctypes.c_float),
                ("radius", ctypes.c_float), ("offset", ctypes.c_float * 2), ("boost", ctypes.c_float)]


class Effects(ctypes.Structure):
    """core/filters.py VideoEffects as the engine takes it (include/sprigcore.h sc_effects)."""

    _fields_ = [
        ("scroll", ctypes.c_float * 2), ("sharpen", ctypes.c_float), ("key_mode", ctypes.c_int),
        ("key_color", ctypes.c_float * 3), ("similarity", ctypes.c_float), ("smoothness", ctypes.c_float),
        ("spill", ctypes.c_float), ("luma", ctypes.c_float * 3), ("gamma", ctypes.c_float),
        ("contrast", ctypes.c_float), ("brightness", ctypes.c_float), ("saturation", ctypes.c_float),
        ("hue", ctypes.c_float), ("opacity", ctypes.c_float), ("tint", ctypes.c_float * 3),
        ("lut", ctypes.c_uint64), ("lut_amount", ctypes.c_float), ("shape", ctypes.c_int),
        ("roundness", ctypes.c_float), ("stretch", ctypes.c_int), ("feather", ctypes.c_float),
        ("border", ctypes.c_float), ("border_color", ctypes.c_float * 3), ("mask", ctypes.c_uint64),
        ("blur", ctypes.c_float), ("pixelate", ctypes.c_float), ("blackout", ctypes.c_float),
        ("spin_turns", ctypes.c_float), ("spin_seconds", ctypes.c_float), ("spin_run", ctypes.c_int),
        ("halo_count", ctypes.c_int), ("halos", Halo * 4),
    ]


class Item(ctypes.Structure):
    _fields_ = [
        ("source", ctypes.c_uint64), ("part", ctypes.c_uint64), ("x", ctypes.c_float), ("y", ctypes.c_float),
        ("scale", ctypes.c_float), ("scale_y", ctypes.c_float), ("rotation", ctypes.c_float), ("align", ctypes.c_int),
        ("crop", ctypes.c_int * 4), ("bounds", ctypes.c_int), ("bounds_size", ctypes.c_float * 2),
        ("bounds_align", ctypes.c_int), ("fade", ctypes.c_float), ("effects", ctypes.c_int),
    ]


class SceneEntry(ctypes.Structure):
    _fields_ = [("id", ctypes.c_uint64), ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("first_item", ctypes.c_int), ("item_count", ctypes.c_int)]


class Snapshot(ctypes.Structure):
    _fields_ = [("scenes", ctypes.POINTER(SceneEntry)), ("scene_count", ctypes.c_int),
                ("items", ctypes.POINTER(Item)), ("item_count", ctypes.c_int),
                ("effects", ctypes.POINTER(Effects)), ("effects_count", ctypes.c_int)]


def key(name: str) -> int:
    """The engine's 64-bit id for one of SprigCast's string ids (never 0)."""
    import hashlib

    return int.from_bytes(hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest(), "little") or 1


def dll_path() -> Path:
    """Where the DLL is looked for: SPRIGCAST_CORE_DLL, else sprigcast/engine/ (where the installer puts it)."""
    override = os.environ.get("SPRIGCAST_CORE_DLL")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "engine" / DLL_NAME


@functools.cache
def _lib():
    """The loaded DLL with its functions typed, or None (not Windows, not built, or an older interface)."""
    if sys.platform != "win32":
        return None
    path = dll_path()
    if not path.is_file():
        log.info("native engine: %s isn't there; using the Python engine", path)
        return None
    try:
        lib = ctypes.CDLL(str(path))
    except OSError as e:
        log.warning("native engine: couldn't load %s (%s); using the Python engine", path, e)
        return None
    c_int, c_float, vp = ctypes.c_int, ctypes.c_float, ctypes.c_void_p
    out = ctypes.POINTER(vp)
    sigs = {
        "sc_interface_version": (c_int, []),
        "sc_build_info": (ctypes.c_char_p, []),
        "sc_last_error": (ctypes.c_char_p, []),
        "sc_log_drain": (c_int, [ctypes.c_char_p, c_int]),
        "sc_adapter_count": (c_int, []),
        "sc_adapter_info_get": (c_int, [c_int, ctypes.POINTER(AdapterInfo)]),
        "sc_device_create": (c_int, [c_int, c_int, out]),
        "sc_device_destroy": (None, [vp]),
        "sc_device_info": (c_int, [vp, ctypes.POINTER(AdapterInfo), ctypes.POINTER(c_int)]),
        "sc_canvas_create": (c_int, [vp, c_int, c_int, out]),
        "sc_canvas_destroy": (None, [vp]),
        "sc_canvas_clear": (c_int, [vp, c_float, c_float, c_float, c_float]),
        "sc_canvas_draw_bgra": (c_int, [vp, ctypes.c_char_p, c_int, c_int, c_int,
                                        c_float, c_float, c_float, c_float, c_float]),
        "sc_canvas_read_bgra": (c_int, [vp, vp, c_int]),
        "sc_source_upload": (c_int, [vp, ctypes.c_uint64, vp, ctypes.POINTER(Upload)]),
        "sc_source_forget": (c_int, [vp, ctypes.c_uint64]),
        "sc_look_set": (c_int, [vp, ctypes.c_uint64, vp, c_int]),
        "sc_mask_set": (c_int, [vp, ctypes.c_uint64, vp, c_int, c_int, c_int]),
        "sc_snapshot_set": (c_int, [vp, ctypes.POINTER(Snapshot)]),
        "sc_canvas_render": (c_int, [vp, ctypes.c_uint64, ctypes.c_double]),
        "sc_debug_fault": (c_int, [vp, c_int]),
    }
    try:
        for name, (restype, argtypes) in sigs.items():
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = restype, argtypes
    except AttributeError as e:
        log.warning("native engine: %s is missing a function (%s); using the Python engine", path, e)
        return None
    version = lib.sc_interface_version()
    if version != INTERFACE_VERSION:
        log.warning("native engine: %s has interface %d, SprigCast needs %d; using the Python engine",
                    path, version, INTERFACE_VERSION)
        return None
    log.info("native engine: %s", lib.sc_build_info().decode("utf-8", "replace"))
    return lib


def available() -> bool:
    return _lib() is not None


def engine_choice() -> str:
    """"native" or "python", as SPRIGCAST_ENGINE asks (anything else means python)."""
    return "native" if os.environ.get("SPRIGCAST_ENGINE", "").strip().lower() == "native" else "python"


def use_native() -> bool:
    """True when the native engine was asked for and loads. Parts that have moved check this once at start."""
    return engine_choice() == "native" and available()


def drain_log() -> int:
    """Moves the engine's queued log lines into SprigCast's log. Returns how many lines moved."""
    lib = _lib()
    if lib is None:
        return 0
    buf = ctypes.create_string_buffer(16384)
    moved = 0
    while True:
        n = lib.sc_log_drain(buf, len(buf))
        if n <= 0:
            return moved
        for line in buf.raw[:n].decode("utf-8", "replace").splitlines():
            level, _, message = line.partition(" ")
            log.log(_LEVELS.get(int(level) if level.isdigit() else 20, logging.INFO), "engine: %s", message)
            moved += 1


def _check(code: int) -> None:
    if code != SC_OK:
        lib = _lib()
        message = lib.sc_last_error().decode("utf-8", "replace") if lib else ""
        drain_log()
        raise NativeError(code, message)


def _need():
    lib = _lib()
    if lib is None:
        raise NativeError(-5, f"the native engine isn't available ({dll_path()})")
    return lib


def adapters() -> list[dict]:
    """Every graphics card Windows lists, Windows' preferred one first."""
    lib = _lib()
    if lib is None:
        return []
    found = []
    for i in range(max(0, lib.sc_adapter_count())):
        info = AdapterInfo()
        if lib.sc_adapter_info_get(i, ctypes.byref(info)) == SC_OK:
            found.append(info.as_dict())
    drain_log()
    return found


class Device:
    """A Direct3D 11 device. ``warp=True`` is Windows' software renderer (tests)."""

    def __init__(self, adapter: int = -1, warp: bool = False):
        lib = _need()
        handle = ctypes.c_void_p()
        _check(lib.sc_device_create(adapter, DEVICE_WARP if warp else 0, ctypes.byref(handle)))
        self._handle = handle
        self._canvases: weakref.WeakSet[Canvas] = weakref.WeakSet()
        drain_log()

    @property
    def handle(self) -> ctypes.c_void_p:
        if not self._handle:
            raise NativeError(-1, "the device is closed")
        return self._handle

    def info(self) -> dict:
        info, level = AdapterInfo(), ctypes.c_int()
        _check(_need().sc_device_info(self.handle, ctypes.byref(info), ctypes.byref(level)))
        d = info.as_dict()
        d["feature_level"] = f"{level.value >> 12}.{(level.value >> 8) & 0xF}"
        return d

    def upload(self, source_id: str, frame, channel_order: str = "RGBA", size: tuple[int, int] | None = None) -> None:
        """A source's new picture, as render/compositor.py Compositor.upload takes it (a NumPy array H x W x C, or
        H x W for planar video). The copy to the card happens in the engine, without Python's lock."""
        import numpy as np

        kind, _, rest = channel_order.partition("/")
        fmt = FORMATS.get(kind)
        if fmt is None:
            raise NativeError(-1, f"unknown channel order {channel_order!r}")
        pixels = np.ascontiguousarray(frame, dtype=np.uint8)
        how = Upload()
        how.format = fmt
        how.height, how.width = pixels.shape[:2]
        how.stride = pixels.strides[0]
        if size and size[0] > 0 and size[1] > 0:
            how.layout_width, how.layout_height = int(size[0]), int(size[1])
        if kind.startswith("I420"):
            from sprigcast.render.compositor import planar_layout

            how.yuv[:] = planar_layout(channel_order)[1]
        _check(_need().sc_source_upload(self.handle, key(source_id), pixels.ctypes.data, ctypes.byref(how)))

    def forget(self, source_id: str) -> None:
        _check(_need().sc_source_forget(self.handle, key(source_id)))

    def set_look(self, name: str, table) -> None:
        """A colour look: n x n x n x 3 floats, red fastest (core/looks.py)."""
        import numpy as np

        data = np.ascontiguousarray(table, dtype=np.float32)
        _check(_need().sc_look_set(self.handle, key(name), data.ctypes.data, data.shape[0]))

    def set_mask(self, name: str, rgba, luma: bool) -> None:
        import numpy as np

        data = np.ascontiguousarray(rgba, dtype=np.uint8)
        _check(_need().sc_mask_set(self.handle, key(name), data.ctypes.data, data.shape[1], data.shape[0], int(luma)))

    def set_snapshot(self, snapshot: Snapshot) -> None:
        """What to draw from now on (render/native_snapshot.py builds it). The engine keeps its own copy."""
        _check(_need().sc_snapshot_set(self.handle, ctypes.byref(snapshot)))

    def close(self) -> None:
        """Closes the device and every canvas still open on it (a canvas must never outlive its device)."""
        if self._handle:
            for canvas in list(self._canvases):
                canvas.close()
            _need().sc_device_destroy(self._handle)
            self._handle = ctypes.c_void_p()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Canvas:
    """A BGRA picture on the graphics card that pictures are drawn onto (the compositor's first step)."""

    def __init__(self, device: Device, width: int, height: int):
        lib = _need()
        handle = ctypes.c_void_p()
        _check(lib.sc_canvas_create(device.handle, width, height, ctypes.byref(handle)))
        self._handle, self.device, self.width, self.height = handle, device, width, height
        device._canvases.add(self)

    @property
    def handle(self) -> ctypes.c_void_p:
        if not self._handle:
            raise NativeError(-1, "the canvas is closed")
        return self._handle

    def clear(self, r: float, g: float, b: float, a: float = 1.0) -> None:
        _check(_need().sc_canvas_clear(self.handle, r, g, b, a))

    def draw_bgra(self, pixels: bytes, width: int, height: int, x: float, y: float, w: float, h: float,
                  opacity: float = 1.0, stride: int | None = None) -> None:
        """Draws a straight-alpha BGRA picture into x, y, w, h (bilinear scaling, "over" blending)."""
        stride = stride or width * 4
        if len(pixels) < stride * (height - 1) + width * 4:
            raise NativeError(-1, "the picture is smaller than its size says")
        _check(_need().sc_canvas_draw_bgra(self.handle, bytes(pixels), width, height, stride, x, y, w, h, opacity))

    def render(self, scene: str, time: float = 0.0) -> None:
        """Draws the snapshot's scene (by its key name) onto this canvas, sprigs first."""
        _check(_need().sc_canvas_render(self.handle, key(scene), float(time)))

    def read_bgra(self) -> bytes:
        """The canvas as BGRA bytes, top row first. For tests and screenshots, never the frame path."""
        out = ctypes.create_string_buffer(self.width * self.height * 4)
        _check(_need().sc_canvas_read_bgra(self.handle, out, self.width * 4))
        return out.raw

    def close(self) -> None:
        if self._handle:
            _need().sc_canvas_destroy(self._handle)
            self._handle = ctypes.c_void_p()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
