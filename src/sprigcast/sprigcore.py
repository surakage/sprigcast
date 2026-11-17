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

INTERFACE_VERSION = 1  # include/sprigcore.h SC_INTERFACE_VERSION this module was written for
DLL_NAME = "sprigcore.dll"

SC_OK = 0
DEVICE_WARP = 1
ERRORS = {-1: "bad argument", -2: "graphics device", -3: "shader", -4: "out of memory", -5: "internal"}
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
