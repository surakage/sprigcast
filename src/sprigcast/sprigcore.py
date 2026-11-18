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

INTERFACE_VERSION = 24  # include/sprigcore.h SC_INTERFACE_VERSION this module was written for
DLL_NAME = "sprigcore.dll"

SC_OK = 0
DEVICE_WARP = 1
ERRORS = {-1: "bad argument", -2: "graphics device", -3: "shader", -4: "out of memory", -5: "internal",
          -6: "faulted", -7: "graphics card lost", -8: "timed out"}
FAULTED, DEVICE_LOST, TIMEOUT = -6, -7, -8  # FAULTED, DEVICE_LOST: the device is shut off (crash gate)
UPLOAD_KEEP = 1
ENGINE_CANVASES = 4
ENGINE_OUTPUTS = 4  # NV12 outputs per canvas (SC_ENGINE_OUTPUTS)
OUTPUT_SHARE = 1  # an output whose frames stay on the card as NV12 textures (SC_OUTPUT_SHARE)
SCALE_MODES = {"bilinear": 0, "area": 1, "bicubic": 2, "lanczos": 3}  # render/compositor.py SCALE_MODES
FORMATS = {"RGBA": 0, "BGRA": 1, "RGB": 2, "BGR": 3, "BGRX": 4, "pBGRA": 5, "I420": 6, "I420A": 7, "NV12": 8}
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


TRANSITION_MODES = {"fade": 1, "slide": 2, "swipe": 3, "wipe": 4, "leaf": 5, "bloom": 6}  # the rest stay in Python
DIRECTIONS = {"left": (-1.0, 0.0), "right": (1.0, 0.0), "up": (0.0, -1.0), "down": (0.0, 1.0)}


class TransitionSpec(ctypes.Structure):
    """include/sprigcore.h sc_transition."""

    _fields_ = [("mode", ctypes.c_int), ("seconds", ctypes.c_float), ("direction", ctypes.c_float * 2),
                ("softness", ctypes.c_float), ("reserved", ctypes.c_int), ("wipe_map", ctypes.c_uint64)]


def transition_spec(transition, wipe_map: str | None = None) -> TransitionSpec | None:
    """core/scene.py Transition as the engine takes it, or None for a kind it doesn't draw (cut, stinger, move)."""
    mode = TRANSITION_MODES.get(transition.kind)
    if mode is None:
        return None
    spec = TransitionSpec()
    spec.mode = mode
    spec.seconds = max(0.001, transition.duration_ms / 1000)
    spec.direction[:] = DIRECTIONS.get(transition.direction, (-1.0, 0.0))
    spec.softness = max(0.0, min(1.0, float(transition.softness)))
    spec.wipe_map = key(wipe_map) if wipe_map else 0
    return spec


class EngineStats(ctypes.Structure):
    _fields_ = [
        ("ticks", ctypes.c_uint64), ("late", ctypes.c_uint64), ("presented", ctypes.c_uint64),
        ("preview_skips", ctypes.c_uint64), ("time", ctypes.c_double), ("render_ms_last", ctypes.c_double),
        ("render_ms_avg", ctypes.c_double), ("render_ms_max", ctypes.c_double), ("running", ctypes.c_int32),
        ("faulted", ctypes.c_int32), ("hung", ctypes.c_int32), ("rebuilds", ctypes.c_int32),
        ("canvases", ctypes.c_int32), ("previews", ctypes.c_int32), ("sources", ctypes.c_int32),
        ("reserved", ctypes.c_int32),
    ]

    def as_dict(self) -> dict:
        d = {name: getattr(self, name) for name, _ in self._fields_ if name != "reserved"}
        for flag in ("running", "faulted", "hung"):
            d[flag] = bool(d[flag])
        return d


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
        "sc_canvas_render_transition": (c_int, [vp, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_double,
                                                ctypes.POINTER(TransitionSpec), c_float]),
        "sc_engine_canvas_transition": (c_int, [vp, c_int, ctypes.c_uint64, ctypes.POINTER(TransitionSpec)]),
        "sc_engine_create": (c_int, [c_int, c_int, out]),
        "sc_engine_destroy": (None, [vp]),
        "sc_engine_set_rate": (c_int, [vp, c_int, c_int]),
        "sc_engine_canvas_set": (c_int, [vp, c_int, c_int, c_int]),
        "sc_engine_canvas_show": (c_int, [vp, c_int, ctypes.c_uint64]),
        "sc_engine_source_upload": (c_int, [vp, ctypes.c_uint64, vp, ctypes.POINTER(Upload), c_int]),
        "sc_engine_source_forget": (c_int, [vp, ctypes.c_uint64]),
        "sc_engine_source_media": (c_int, [vp, ctypes.c_uint64, vp]),
        "sc_engine_source_shared": (c_int, [vp, ctypes.c_uint64, ctypes.c_wchar_p]),
        "sc_d3d_device_luid": (c_int, [vp, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_int32)]),
        "sc_page_target_create": (c_int, [ctypes.c_uint32, ctypes.c_int32, vp, c_int, c_int, ctypes.c_wchar_p, out]),
        "sc_page_target_handles": (c_int, [vp, out, out, out]),
        "sc_page_target_begin": (c_int, [vp, c_int]),
        "sc_page_target_end": (c_int, [vp, c_int]),
        "sc_page_target_read": (c_int, [vp, vp, c_int]),
        "sc_page_target_fill": (c_int, [vp, vp]),
        "sc_page_target_destroy": (None, [vp]),
        "sc_engine_look_set": (c_int, [vp, ctypes.c_uint64, vp, c_int]),
        "sc_engine_mask_set": (c_int, [vp, ctypes.c_uint64, vp, c_int, c_int, c_int]),
        "sc_engine_snapshot_set": (c_int, [vp, ctypes.POINTER(Snapshot)]),
        "sc_engine_preview_attach": (c_int, [vp, c_int, vp, c_int, ctypes.c_uint32, ctypes.POINTER(c_int)]),
        "sc_engine_preview_detach": (c_int, [vp, c_int]),
        "sc_engine_stats_get": (c_int, [vp, ctypes.POINTER(EngineStats)]),
        "sc_engine_canvas_read": (c_int, [vp, c_int, vp, c_int, c_int]),
        "sc_engine_canvas_watch": (c_int, [vp, c_int, c_int, c_int, c_int]),
        "sc_engine_canvas_take": (c_int, [vp, c_int, vp, c_int, c_int, ctypes.POINTER(ctypes.c_uint64),
                                          ctypes.POINTER(c_int), ctypes.POINTER(ctypes.c_double)]),
        "sc_engine_debug_fault": (c_int, [vp, c_int]),
        "sc_canvas_read_nv12": (c_int, [vp, vp, c_int, c_int, c_int]),
        "sc_engine_output_set": (c_int, [vp, c_int, c_int, c_int, c_int, c_int, c_int]),
        "sc_engine_device": (c_int, [vp, out, ctypes.POINTER(c_int)]),
        "sc_engine_output_take_texture": (c_int, [vp, c_int, c_int, out, ctypes.POINTER(ctypes.c_uint64),
                                                  ctypes.POINTER(c_int), ctypes.POINTER(ctypes.c_double),
                                                  ctypes.POINTER(c_int), ctypes.POINTER(ctypes.c_uint64)]),
        "sc_engine_output_give_back": (c_int, [vp, c_int, c_int, ctypes.c_uint64, c_int]),
        "sc_com_release": (None, [vp]),
        "sc_engine_output_link": (c_int, [vp, c_int, c_int, ctypes.POINTER(OutputLink)]),
        "sc_engine_output_unlink": (c_int, [vp, c_int, c_int, ctypes.POINTER(ctypes.c_int64), ctypes.POINTER(c_int)]),
        "sc_engine_output_link_camera": (c_int, [vp, c_int, c_int, ctypes.c_wchar_p, c_int, c_int, c_int]),
        "sc_engine_output_camera_frames": (c_int, [vp, c_int, c_int, ctypes.POINTER(ctypes.c_uint64)]),
        "sc_engine_output_take": (c_int, [vp, c_int, c_int, vp, ctypes.c_size_t, ctypes.POINTER(c_int),
                                          ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_uint64)]),
    }
    sigs.update(_media_sigs())
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
    lib = _lib() or _media_lib()
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
        lib = _lib() or _media_lib()
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


def _upload_args(frame, channel_order: str, size):
    """A picture as the engine takes it: contiguous bytes and their description."""
    import numpy as np

    kind, _, _rest = channel_order.partition("/")
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
    if kind.startswith("I420") or kind == "NV12":  # NV12: Y rows, then UVUV rows (the engine repacks it)
        from sprigcast.render.compositor import planar_layout

        how.yuv[:] = planar_layout("I420/" + _rest if kind == "NV12" else channel_order)[1]
    return pixels, how


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
        pixels, how = _upload_args(frame, channel_order, size)
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

    def render_transition(self, previous: str, scene: str, spec: TransitionSpec, progress: float,
                          time: float = 0.0) -> None:
        """Draws the change from `previous` to `scene` at `progress` (0 .. 1)."""
        _check(_need().sc_canvas_render_transition(self.handle, key(previous), key(scene), float(time),
                                                   ctypes.byref(spec), float(progress)))

    def read_nv12(self, width: int, height: int, scale: str = "bilinear") -> bytes:
        """The canvas scaled to width x height (even) as NV12, as render/compositor.py read_nv12 gives it.
        Waits for the card: tests only."""
        out = ctypes.create_string_buffer(width * height * 3 // 2)
        _check(_need().sc_canvas_read_nv12(self.handle, out, int(width), int(height), SCALE_MODES.get(scale, 0)))
        return out.raw

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


def device_luid(device: int) -> tuple[int, int]:
    """The LUID (low, high) of the graphics card a Direct3D 11 device pointer is on."""
    low, high = ctypes.c_uint32(), ctypes.c_int32()
    _check(_need().sc_d3d_device_luid(device, ctypes.byref(low), ctypes.byref(high)))
    return low.value, high.value


def engine_luid(engine: "Engine") -> tuple[int, int] | None:
    """The card the engine draws on, for the browser helper to draw pages on the same one; None while it has no
    device (rebuilding)."""
    device, _ = engine.device()
    if not device:
        return None
    try:
        return device_luid(device)
    finally:
        com_release(device)


class PageTarget:
    """A browser page's texture on the card (include/sprigcore.h sc_page_target), made by the browser helper on
    the engine's card and shared by ``name``: Qt Quick draws the page into it between :meth:`begin` and
    :meth:`end`, and the engine copies it into the page's source."""

    def __init__(self, luid: tuple[int, int], width: int, height: int, name: str, device: int | None = None):
        """``device``: an earlier target's :attr:`device`, so a resized page keeps drawing with the same one."""
        lib = _need()
        handle = ctypes.c_void_p()
        _check(lib.sc_page_target_create(luid[0], luid[1], device, int(width), int(height), name,
                                         ctypes.byref(handle)))
        self._handle, self.name, self.size = handle, name, (int(width), int(height))
        device, context, texture = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        _check(lib.sc_page_target_handles(handle, ctypes.byref(device), ctypes.byref(context), ctypes.byref(texture)))
        self.device, self.context, self.texture = device.value, context.value, texture.value

    def begin(self, timeout_ms: int = 0) -> bool:
        """True when drawing may start (the engine took the last picture); False: not yet."""
        code = _need().sc_page_target_begin(self._handle, int(timeout_ms))
        if code < 0:
            _check(code)
        return code == 1

    def read(self):
        """Between begin and end: the picture drawn, as straight-alpha RGBA (h, w, 4) uint8."""
        import numpy as np

        w, h = self.size
        rgba = np.empty((h, w, 4), np.uint8)
        _check(_need().sc_page_target_read(self._handle, rgba.ctypes.data, w * 4))
        return rgba

    def fill(self, rgba: tuple[float, float, float, float]) -> None:
        """Between begin and end: the whole texture one colour (premultiplied, 0..1)."""
        colour = (ctypes.c_float * 4)(*rgba)
        _check(_need().sc_page_target_fill(self._handle, colour))

    def end(self, publish: bool = True) -> None:
        """The picture is drawn. ``publish``: the engine may take it; else it was only drawn to be read back."""
        _check(_need().sc_page_target_end(self._handle, int(bool(publish))))

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            _need().sc_page_target_destroy(handle)


def com_release(pointer: int) -> None:
    """IUnknown::Release on a device or texture pointer the engine gave."""
    lib = _lib()
    if pointer and lib is not None:
        lib.sc_com_release(pointer)


class Engine:
    """The engine's own frame loop (phase 2): a frame thread in the DLL draws every canvas at the frame rate and
    shows them in preview windows. Every call only hands work over and returns at once; the frame thread takes it
    at the start of its next frame. A fault or a lost graphics card is rebuilt from the engine's own copies; when
    :meth:`stats` says ``faulted`` the engine has given up and SprigCast's Python engine should take over."""

    def __init__(self, adapter: int = -1, warp: bool = False, fps: tuple[int, int] = (60, 1)):
        lib = _need()
        handle = ctypes.c_void_p()
        _check(lib.sc_engine_create(adapter, DEVICE_WARP if warp else 0, ctypes.byref(handle)))
        self._handle = handle
        self._sizes: dict[int, tuple[int, int]] = {}
        self._previews: set[int] = set()
        self._seen: dict[int, ctypes.c_uint64] = {}
        drain_log()
        self.set_rate(*fps)

    @property
    def handle(self) -> ctypes.c_void_p:
        if not self._handle:
            raise NativeError(-1, "the engine is closed")
        return self._handle

    def set_rate(self, num: int, den: int = 1) -> None:
        _check(_need().sc_engine_set_rate(self.handle, int(num), int(den)))

    def set_canvas(self, index: int, width: int, height: int) -> None:
        """Canvas slot ``index`` at width x height; 0 x 0 removes it."""
        _check(_need().sc_engine_canvas_set(self.handle, index, int(width), int(height)))
        if width and height:
            self._sizes[index] = (int(width), int(height))
        else:
            self._sizes.pop(index, None)

    def show(self, index: int, scene: str | None) -> None:
        """Which snapshot scene canvas ``index`` draws (None: nothing)."""
        _check(_need().sc_engine_canvas_show(self.handle, index, key(scene) if scene else 0))

    def upload(self, source_id: str, frame, channel_order: str = "RGBA", size: tuple[int, int] | None = None,
               keep: bool = False) -> None:
        """As :meth:`Device.upload`. ``keep`` for pictures that don't change: the engine keeps a copy so they
        come back by themselves after a rebuild (video sends its next frame anyway)."""
        pixels, how = _upload_args(frame, channel_order, size)
        _check(_need().sc_engine_source_upload(self.handle, key(source_id), pixels.ctypes.data, ctypes.byref(how),
                                               UPLOAD_KEEP if keep else 0))

    def forget(self, source_id: str) -> None:
        _check(_need().sc_engine_source_forget(self.handle, key(source_id)))

    def attach_media(self, source_id: str, player: "MediaPlayer | None") -> None:
        """Source ``source_id`` shows what ``player`` plays: the frame thread takes each picture from the player
        itself, so a video's pictures never pass through Python. None goes back to :meth:`upload` (the source
        keeps the picture it has)."""
        lib = _need()
        if player is None:
            _check(lib.sc_engine_source_media(self.handle, key(source_id), None))
        else:
            _check(player._call(lambda media: lib.sc_engine_source_media(self.handle, key(source_id), media)))

    def attach_shared(self, source_id: str, name: str | None) -> None:
        """Source ``source_id`` shows the pictures in the shared memory block ``name`` (a browser page's, written
        by app/browser_helper.py FrameSlot): the frame thread copies each new one onto the card itself. None goes
        back to :meth:`upload` (the source keeps the picture it has)."""
        _check(_need().sc_engine_source_shared(self.handle, key(source_id), name or None))

    def set_look(self, name: str, table) -> None:
        import numpy as np

        data = np.ascontiguousarray(table, dtype=np.float32)
        _check(_need().sc_engine_look_set(self.handle, key(name), data.ctypes.data, data.shape[0]))

    def set_mask(self, name: str, rgba, luma: bool) -> None:
        import numpy as np

        data = np.ascontiguousarray(rgba, dtype=np.uint8)
        _check(_need().sc_engine_mask_set(self.handle, key(name), data.ctypes.data, data.shape[1], data.shape[0],
                                          int(luma)))

    def set_snapshot(self, snapshot: Snapshot) -> None:
        _check(_need().sc_engine_snapshot_set(self.handle, ctypes.byref(snapshot)))

    def attach_preview(self, index: int, hwnd: int, max_fps: int = 0, background: int = 0x000000) -> int:
        """Shows canvas ``index`` in the native window ``hwnd`` (int(widget.winId())), fitted with bars of
        ``background`` (0xRRGGBB). The window must stay open until :meth:`detach_preview`."""
        pid = ctypes.c_int()
        _check(_need().sc_engine_preview_attach(self.handle, index, ctypes.c_void_p(int(hwnd)), int(max_fps),
                                                int(background) & 0xFFFFFF, ctypes.byref(pid)))
        self._previews.add(pid.value)
        return pid.value

    def detach_preview(self, preview: int) -> None:
        """Stops the preview; returns once the engine has let go of the window, so it can close."""
        self._previews.discard(preview)
        _check(_need().sc_engine_preview_detach(self.handle, preview))

    def stats(self) -> dict:
        out = EngineStats()
        _check(_need().sc_engine_stats_get(self.handle, ctypes.byref(out)))
        drain_log()
        return out.as_dict()

    def read_bgra(self, index: int) -> bytes:
        """Canvas ``index`` as BGRA bytes from a frame drawn after this call. Tests and screenshots only."""
        if index not in self._sizes:
            raise NativeError(-1, f"canvas {index} isn't set")
        w, h = self._sizes[index]
        out = ctypes.create_string_buffer(w * h * 4)
        _check(_need().sc_engine_canvas_read(self.handle, index, out, w, h))
        return out.raw

    def transition(self, index: int, previous: str | None, spec: TransitionSpec | None) -> None:
        """Starts a transition on canvas ``index`` from scene ``previous`` to the one it shows, timed by the
        engine from its next frame. None stops one that is running."""
        if previous is None or spec is None:
            _check(_need().sc_engine_canvas_transition(self.handle, index, 0, None))
        else:
            _check(_need().sc_engine_canvas_transition(self.handle, index, key(previous), ctypes.byref(spec)))

    def watch(self, index: int, width: int, height: int, max_fps: int = 0) -> None:
        """Have the engine copy canvas ``index`` back shrunk to width x height (premultiplied RGBA, as previews
        take it) at most ``max_fps`` times a second, without waiting on the card. 0 x 0 stops."""
        _check(_need().sc_engine_canvas_watch(self.handle, index, int(width), int(height), int(max_fps)))

    def take(self, index: int, width: int, height: int) -> tuple[bytes, float] | None:
        """The newest watched picture of canvas ``index`` at width x height and the engine time it was drawn,
        or None when nothing newer has come back. Never waits."""
        seq = self._seen.setdefault(index, ctypes.c_uint64(0))
        out = ctypes.create_string_buffer(width * height * 4)
        fresh, drawn = ctypes.c_int(0), ctypes.c_double(0.0)
        _check(_need().sc_engine_canvas_take(self.handle, index, out, width, height, ctypes.byref(seq),
                                             ctypes.byref(fresh), ctypes.byref(drawn)))
        return (out.raw, drawn.value) if fresh.value else None

    def set_output(self, index: int, slot: int, width: int, height: int, scale: str = "bilinear",
                   share: bool = False) -> None:
        """Have the engine make NV12 frames of canvas ``index`` at width x height (even) for an encoder, in
        output slot ``slot`` (0 .. ENGINE_OUTPUTS - 1), scaled the way Canvas.downscale says. 0 x 0 stops.
        share: the frames stay on the card as NV12 textures (take_texture) instead of coming back as bytes."""
        _check(_need().sc_engine_output_set(self.handle, index, slot, int(width), int(height),
                                            SCALE_MODES.get(scale, 0), OUTPUT_SHARE if share else 0))

    def device(self) -> tuple[int, int]:
        """The engine's Direct3D 11 device for an encoder session that takes its shared frames: (an
        ID3D11Device pointer with a reference of the caller's, see com_release; how many times the engine has
        rebuilt its device)."""
        device, rebuilds = ctypes.c_void_p(), ctypes.c_int(0)
        _check(_need().sc_engine_device(self.handle, ctypes.byref(device), ctypes.byref(rebuilds)))
        return device.value or 0, rebuilds.value

    def luid(self) -> tuple[int, int] | None:
        """The graphics card the engine draws on (see engine_luid)."""
        return engine_luid(self)

    def take_texture(self, index: int, slot: int) -> tuple[int, tuple | None, int]:
        """The oldest finished frame of a shared output: (state, frame, frames dropped so far). state 1: frame
        is (NV12 texture pointer with a reference of the caller's, pool id, entry, when it was drawn in
        time.perf_counter() seconds), the caller's until give_back; 0: none waiting; -1: this device can't
        share frames. Never waits."""
        texture, pool, entry = ctypes.c_void_p(), ctypes.c_uint64(0), ctypes.c_int(0)
        drawn, state, dropped = ctypes.c_double(0.0), ctypes.c_int(0), ctypes.c_uint64(0)
        _check(_need().sc_engine_output_take_texture(
            self.handle, index, slot, ctypes.byref(texture), ctypes.byref(pool), ctypes.byref(entry),
            ctypes.byref(drawn), ctypes.byref(state), ctypes.byref(dropped)))
        frame = (texture.value, pool.value, entry.value, drawn.value) if state.value == 1 else None
        return state.value, frame, dropped.value

    def give_back(self, index: int, slot: int, pool: int, entry: int) -> None:
        """Hands a shared frame's texture back to the engine to draw into again."""
        if self._handle:
            _need().sc_engine_output_give_back(self._handle, index, slot, pool, entry)

    def take_output(self, index: int, slot: int, width: int, height: int) -> tuple[bytearray, float, int] | None:
        """The oldest finished NV12 frame of an output: (data, when it was drawn in time.perf_counter()
        seconds, frames dropped so far), or None when none is waiting. Never waits."""
        size = width * height * 3 // 2
        data = bytearray(size)
        fresh, drawn, dropped = ctypes.c_int(0), ctypes.c_double(0.0), ctypes.c_uint64(0)
        _check(_need().sc_engine_output_take(self.handle, index, slot, (ctypes.c_char * size).from_buffer(data),
                                             size, ctypes.byref(fresh), ctypes.byref(drawn), ctypes.byref(dropped)))
        return (data, drawn.value, dropped.value) if fresh.value else None

    def link_output(self, index: int, slot: int, writer: Writer, start: float, fps: tuple[int, int] | int,
                    last_pts: int = -1, keep: bool = False, fill_seconds: float = 0.0) -> None:
        """Hand every frame of a plain output straight to ``writer`` (of the output's size) on the engine's own thread,
        numbered from ``start`` (time.perf_counter() seconds of frame 0) at ``fps`` and going on after ``last_pts``.
        keep: frames are still queued for take_output (other outputs share this one). Linking the same writer
        again changes these and keeps the numbering. Raises NativeError when the output doesn't fit the writer."""
        num, den = (fps, 1) if isinstance(fps, int) else fps
        link = OutputLink(writer.handle_value(), float(start), int(num), int(den), int(last_pts), 1 if keep else 0,
                          float(fill_seconds))
        _check(_need().sc_engine_output_link(self.handle, index, slot, ctypes.byref(link)))

    def unlink_output(self, index: int, slot: int) -> tuple[int, bool]:
        """End an output's link after any frame being handed over: (the last frame number its writer took,
        -1 for none; whether it was still linked). Works on a stopped engine too."""
        last, linked = ctypes.c_int64(-1), ctypes.c_int(0)
        if self._handle:
            _check(_need().sc_engine_output_unlink(self._handle, index, slot, ctypes.byref(last), ctypes.byref(linked)))
        return last.value, bool(linked.value)

    def link_camera(self, index: int, slot: int, name: str, width: int, height: int, keep: bool = False) -> None:
        """Write every frame of a plain output into the virtual camera's shared block ``name`` (a width x height
        frame; a smaller output goes in the middle), on the engine's own thread. The engine maps the block itself and
        unmaps it when unlink_output (or another link) ends this one. Raises NativeError when it can't."""
        _check(_need().sc_engine_output_link_camera(self.handle, index, slot, name, int(width), int(height),
                                                    1 if keep else 0))

    def camera_frames(self, index: int, slot: int) -> int:
        """Frames the engine wrote into the camera's block since linking it (0: no camera link there)."""
        frames = ctypes.c_uint64(0)
        if self._handle:
            _check(_need().sc_engine_output_camera_frames(self._handle, index, slot, ctypes.byref(frames)))
        return frames.value

    def debug_fault(self, kind: int) -> None:
        """Crash gate checks (tests only): 1 exception, 2 access violation, 3 lost card, 4 a 3 s stuck frame."""
        _check(_need().sc_engine_debug_fault(self.handle, kind))

    def close(self) -> None:
        if self._handle:
            lib = _need()
            for preview in list(self._previews):
                lib.sc_engine_preview_detach(self._handle, preview)
            self._previews.clear()
            lib.sc_engine_destroy(self._handle)
            self._handle = ctypes.c_void_p()
            drain_log()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---- Video and sound files (phase 3) ------------------------------------------------------------------------------

MEDIA_STATES = ("playing", "paused", "stopped", "ended")  # SC_MEDIA_PLAYING ...
(MEDIA_PLAY, MEDIA_PAUSE, MEDIA_RESTART, MEDIA_STOP, MEDIA_SEEK, MEDIA_SLEEP, MEDIA_WAKE, MEDIA_HIDE, MEDIA_SHOW,
 MEDIA_RESPEED, MEDIA_START, MEDIA_CLOSE) = range(1, 13)
MEDIA_NO_PICTURE, MEDIA_PICTURE, MEDIA_BLANK, MEDIA_TOO_SMALL = range(4)
_FORMAT_NAMES = {code: name for name, code in FORMATS.items()}
FFMPEG_LIBRARIES = ("avutil", "avcodec", "avformat", "swscale", "swresample")


class MediaOptions(ctypes.Structure):
    _fields_ = [("loop", ctypes.c_int), ("clear_on_end", ctypes.c_int), ("restart_on_show", ctypes.c_int),
                ("speed", ctypes.c_float)]


class MediaInfo(ctypes.Structure):
    """include/sprigcore.h sc_media_info."""

    _fields_ = [("state", ctypes.c_int), ("has_video", ctypes.c_int), ("width", ctypes.c_int),
                ("height", ctypes.c_int), ("hidden", ctypes.c_int), ("thread", ctypes.c_int),
                ("faulted", ctypes.c_int), ("replaying", ctypes.c_int), ("position", ctypes.c_double),
                ("duration", ctypes.c_double), ("speed", ctypes.c_double), ("decoded", ctypes.c_uint64),
                ("passes", ctypes.c_uint64), ("picture_bytes", ctypes.c_uint64), ("status", ctypes.c_char * 96),
                ("hardware", ctypes.c_int)]

    @property
    def state_name(self) -> str:
        return MEDIA_STATES[self.state] if 0 <= self.state < len(MEDIA_STATES) else "stopped"

    @property
    def status_text(self) -> str:
        return self.status.decode("utf-8", "replace")


class MediaPicture(ctypes.Structure):
    _fields_ = [("format", ctypes.c_int), ("width", ctypes.c_int), ("height", ctypes.c_int), ("stride", ctypes.c_int),
                ("matrix", ctypes.c_int), ("full_range", ctypes.c_int), ("bytes", ctypes.c_uint64),
                ("time", ctypes.c_double)]


class OutputLink(ctypes.Structure):
    """include/sprigcore.h sc_output_link."""

    _fields_ = [("writer", ctypes.c_void_p), ("start", ctypes.c_double), ("fps_num", ctypes.c_int),
                ("fps_den", ctypes.c_int), ("last_pts", ctypes.c_int64), ("keep", ctypes.c_int),
                ("fill_seconds", ctypes.c_double)]


class WriterConfig(ctypes.Structure):
    """include/sprigcore.h sc_writer_config."""

    _fields_ = [("target", ctypes.c_char_p), ("format", ctypes.c_char_p), ("video_codec", ctypes.c_char_p),
                ("video_options", ctypes.c_char_p), ("width", ctypes.c_int), ("height", ctypes.c_int),
                ("fps_num", ctypes.c_int), ("fps_den", ctypes.c_int), ("video_kbps", ctypes.c_int),
                ("keyframe_frames", ctypes.c_int), ("audio_tracks", ctypes.c_int), ("audio_kbps", ctypes.c_int),
                ("queue_frames", ctypes.c_int), ("format_options", ctypes.c_char_p),
                ("track_titles", ctypes.c_char_p), ("b_frames", ctypes.c_int), ("d3d11_device", ctypes.c_void_p),
                ("packets", ctypes.c_int)]


class WriterPacket(ctypes.Structure):
    """include/sprigcore.h sc_writer_packet."""

    _fields_ = [("track", ctypes.c_int), ("keyframe", ctypes.c_int), ("pts", ctypes.c_int64), ("dts", ctypes.c_int64),
                ("tb_num", ctypes.c_int), ("tb_den", ctypes.c_int), ("size", ctypes.c_int)]


class WriterStats(ctypes.Structure):
    """include/sprigcore.h sc_writer_stats."""

    _fields_ = [("state", ctypes.c_int), ("queued", ctypes.c_int), ("frames_in", ctypes.c_uint64),
                ("frames_written", ctypes.c_uint64), ("frames_dropped", ctypes.c_uint64), ("bytes", ctypes.c_uint64),
                ("write_seconds", ctypes.c_double), ("longest_write", ctypes.c_double), ("error", ctypes.c_char * 256),
                ("last_pts", ctypes.c_int64), ("frames_late", ctypes.c_uint64), ("frames_repeated", ctypes.c_uint64),
                ("on_card", ctypes.c_int), ("recoveries", ctypes.c_int)]

    @property
    def state_name(self) -> str:
        return WRITER_STATES[self.state] if 0 <= self.state < len(WRITER_STATES) else "failed"

    @property
    def error_text(self) -> str:
        return self.error.decode("utf-8", "replace")


WRITER_STATES = ("opening", "running", "finished", "failed")
WRITER_DROPPED, WRITER_REFUSED, WRITER_LATE = 1, 2, 3


MIXER_BLOCK = 1024  # SC_MIXER_BLOCK
MIXER_TRACKS = ("stream", "recording", "vod", "voice", "clips", "track4", "track5", "clean")  # SC_TRACK_* order
MIXER_MONITOR = -1  # SC_MIXER_MONITOR
MIXER_SOURCE_TAP = 1000  # SC_MIXER_SOURCE_TAP
MIXER_MASTER = 0  # SC_MIXER_MASTER


class MixerSourceSettings(ctypes.Structure):
    _fields_ = [("gain", ctypes.c_float), ("mono", ctypes.c_int), ("balance", ctypes.c_float),
                ("delay_ms", ctypes.c_int), ("tracks", ctypes.c_uint32), ("monitor", ctypes.c_int),
                ("voice", ctypes.c_int), ("bloom_out", ctypes.c_int)]


class MixerDucking(ctypes.Structure):
    _fields_ = [("enabled", ctypes.c_int), ("amount_db", ctypes.c_float), ("threshold_db", ctypes.c_float),
                ("attack_ms", ctypes.c_float), ("release_ms", ctypes.c_float), ("hold_ms", ctypes.c_float)]


class MixerLevel(ctypes.Structure):
    _fields_ = [("peak_db", ctypes.c_float), ("rms_db", ctypes.c_float), ("level", ctypes.c_float),
                ("underruns", ctypes.c_uint64)]


CAPTURE_STATES = ("starting", "capturing", "not found", "lost", "failed")  # SC_CAPTURE_*
CAPTURE_TEST_TONE = "sprigcast-test-tone"


class CaptureStats(ctypes.Structure):
    _fields_ = [("state", ctypes.c_int), ("channels", ctypes.c_int), ("rate", ctypes.c_int),
                ("frames", ctypes.c_uint64), ("gaps", ctypes.c_uint64), ("device", ctypes.c_wchar * 128),
                ("error", ctypes.c_char * 256)]


def _media_sigs() -> dict:
    c_int, vp = ctypes.c_int, ctypes.c_void_p
    text = ctypes.c_char_p
    return {
        "sc_media_ffmpeg_load": (c_int, [text, text, text, text, text]),
        "sc_media_ffmpeg_ready": (c_int, []),
        "sc_media_create": (c_int, [c_int, ctypes.POINTER(vp)]),
        "sc_media_destroy": (None, [vp]),
        "sc_media_set_options": (c_int, [vp, ctypes.POINTER(MediaOptions)]),
        "sc_media_set_file": (c_int, [vp, text, c_int, c_int]),
        "sc_media_control": (c_int, [vp, c_int, ctypes.c_double]),
        "sc_media_set_shrink": (c_int, [vp, ctypes.c_float]),
        "sc_media_info_get": (c_int, [vp, ctypes.POINTER(MediaInfo)]),
        "sc_media_video_frame": (c_int, [vp, ctypes.POINTER(ctypes.c_uint64), ctypes.POINTER(MediaPicture), vp,
                                         ctypes.c_size_t]),
        "sc_media_read_audio": (c_int, [vp, vp, c_int, c_int]),
        "sc_media_set_picture": (c_int, [vp, c_int, c_int, c_int, c_int]),
        "sc_media_set_decode": (c_int, [vp, c_int]),
        "sc_media_debug_fault": (c_int, [vp, c_int]),
        "sc_writer_open": (c_int, [ctypes.POINTER(WriterConfig), ctypes.POINTER(vp)]),
        "sc_writer_video": (c_int, [vp, vp, c_int, ctypes.c_int64]),
        "sc_writer_video_span": (c_int, [vp, vp, c_int, ctypes.c_int64, ctypes.c_int64]),
        "sc_writer_audio": (c_int, [vp, c_int, vp, c_int]),
        "sc_writer_finish": (c_int, [vp]),
        "sc_writer_wait": (c_int, [vp, c_int]),
        "sc_writer_stats_get": (c_int, [vp, ctypes.POINTER(WriterStats)]),
        "sc_writer_destroy": (None, [vp]),
        "sc_writer_packet_take": (c_int, [vp, ctypes.POINTER(WriterPacket), vp, c_int]),
        "sc_writer_set_bitrate": (c_int, [vp, c_int, c_int, c_int]),
        "sc_writer_keyframe": (c_int, [vp]),
        "sc_mixer_create": (c_int, [c_int, ctypes.POINTER(vp)]),
        "sc_mixer_destroy": (None, [vp]),
        "sc_mixer_source_add": (c_int, [vp, ctypes.POINTER(MixerSourceSettings), ctypes.POINTER(c_int)]),
        "sc_mixer_source_set": (c_int, [vp, c_int, ctypes.POINTER(MixerSourceSettings)]),
        "sc_mixer_source_remove": (c_int, [vp, c_int]),
        "sc_mixer_source_push": (c_int, [vp, c_int, vp, c_int]),
        "sc_mixer_source_media": (c_int, [vp, c_int, vp, c_int]),
        "sc_mixer_set_ducking": (c_int, [vp, ctypes.POINTER(MixerDucking)]),
        "sc_mixer_tap_add": (c_int, [vp, ctypes.POINTER(c_int), c_int, ctypes.POINTER(c_int)]),
        "sc_mixer_tap_remove": (c_int, [vp, c_int]),
        "sc_mixer_tap_take": (c_int, [vp, c_int, vp, c_int, ctypes.POINTER(ctypes.c_int64),
                                      ctypes.POINTER(ctypes.c_uint64)]),
        "sc_mixer_tick": (c_int, [vp, c_int]),
        "sc_mixer_wait": (c_int, [vp, ctypes.c_int64, c_int, ctypes.POINTER(ctypes.c_int64)]),
        "sc_mixer_levels": (c_int, [vp, c_int, ctypes.POINTER(MixerLevel)]),
        "sc_capture_open": (c_int, [vp, c_int, ctypes.c_wchar_p, c_int, ctypes.POINTER(vp)]),
        "sc_capture_stats_get": (c_int, [vp, ctypes.POINTER(CaptureStats)]),
        "sc_capture_close": (None, [vp]),
    }


@functools.cache
def _media_lib():
    """The library with the media player: sprigcore.dll on Windows. Elsewhere the player alone can be built for
    its checks (tests/test_native_media.py) and named in SPRIGCAST_MEDIA_LIB; SprigCast itself never does."""
    lib = _lib()
    if lib is not None or sys.platform == "win32":
        return lib
    path = os.environ.get("SPRIGCAST_MEDIA_LIB")
    if not path or not Path(path).is_file():
        return None
    try:
        lib = ctypes.CDLL(path)
        sigs = {"sc_interface_version": (ctypes.c_int, []), "sc_last_error": (ctypes.c_char_p, []),
                "sc_log_drain": (ctypes.c_int, [ctypes.c_char_p, ctypes.c_int]), **_media_sigs()}
        for name, (restype, argtypes) in sigs.items():
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = restype, argtypes
    except (OSError, AttributeError) as e:
        log.warning("native media: couldn't load %s (%s)", path, e)
        return None
    return lib if lib.sc_interface_version() == INTERFACE_VERSION else None


def ffmpeg_libraries() -> list[str] | None:
    """The FFmpeg library files PyAV brought with it (site-packages/av.libs), in FFMPEG_LIBRARIES order, or None
    when they can't be found. The engine loads these same files: SprigCast ships one FFmpeg."""
    try:
        import av
    except Exception:  # noqa: BLE001 (no PyAV: the Python player can't play either)
        return None
    package = Path(av.__file__).resolve().parent
    folders = [package.parent / "av.libs", package, package.parent]
    found = []
    for name in FFMPEG_LIBRARIES:
        for folder in folders:
            hits = sorted(str(p) for pattern in (f"{name}-*.dll", f"lib{name}-*.so*", f"lib{name}.*.dylib")
                          for p in folder.glob(pattern))
            if hits:
                found.append(hits[0])
                break
        else:
            return None
    return found


@functools.cache
def media_ready() -> bool:
    """True once the engine has loaded FFmpeg and can play files itself. Asked once: a miss means the Python
    player for this run."""
    lib = _media_lib()
    if lib is None:
        return False
    if lib.sc_media_ffmpeg_ready():
        return True
    paths = ffmpeg_libraries()
    if paths is None:
        log.warning("native media: PyAV's FFmpeg libraries weren't found; Python plays video and sound files")
        return False
    code = lib.sc_media_ffmpeg_load(*(p.encode("utf-8") for p in paths))
    if code != SC_OK:
        log.warning("native media: %s; Python plays video and sound files",
                    lib.sc_last_error().decode("utf-8", "replace") or ERRORS.get(code, code))
    drain_log()
    return code == SC_OK


def use_native_media() -> bool:
    """True when the native engine was asked for (SPRIGCAST_ENGINE=native) and its player can play files."""
    return engine_choice() == "native" and media_ready()


class MediaPlayer:
    """One video, GIF or sound file played by the engine (include/sprigcore.h sc_media): a thread of the engine's
    own decodes ahead, and :meth:`video_frame` and :meth:`read_audio` take what is due on the file's clock. Every
    method is safe from any thread, and none waits for the decoder."""

    def __init__(self, playing: bool = True):
        if not media_ready():
            raise NativeError(-2, "the engine's media player isn't available")
        self._lib = _media_lib()
        handle = ctypes.c_void_p()
        _check(self._lib.sc_media_create(int(playing), ctypes.byref(handle)))
        self._handle = handle
        self._lock = __import__("threading").Lock()  # close() against a call still using the handle
        self._capacity = 0
        self._picture = MediaPicture()
        self._seen = ctypes.c_uint64(0)  # the picture video_frame took last (the engine keeps its own)

    def _call(self, fn, *args) -> int:
        with self._lock:
            if not self._handle:
                raise NativeError(-1, "the player is closed")
            return fn(self._handle, *args)

    def set_options(self, loop: bool, clear_on_end: bool, restart_on_show: bool, speed: float) -> None:
        options = MediaOptions(int(bool(loop)), int(bool(clear_on_end)), int(bool(restart_on_show)), float(speed))
        _check(self._call(self._lib.sc_media_set_options, ctypes.byref(options)))

    def set_picture(self, order: str = "", limit: tuple[int, int] = (0, 0), grow: bool = False) -> None:
        """Before :meth:`set_file`: every picture in ``order`` ("RGBA" or "BGRA", straight alpha; "" as the
        file has them), and videos bigger than ``limit`` shrunk to fit it while decoding ((0, 0) = 1920 a
        side). ``grow`` also enlarges smaller ones to fit it."""
        _check(self._call(self._lib.sc_media_set_picture, {"": 0, "RGBA": 1, "BGRA": 2}[order], int(limit[0]),
                          int(limit[1]), int(grow)))

    def set_decode(self, hardware: bool) -> None:
        """Before :meth:`set_file`: decode videos on the graphics card where it can (info().hardware says when it
        does), else on the CPU."""
        _check(self._call(self._lib.sc_media_set_decode, int(hardware)))

    def set_file(self, path: str, video: bool, playing: bool) -> None:
        _check(self._call(self._lib.sc_media_set_file, str(path).encode("utf-8"), int(video), int(playing)))

    def control(self, what: int, value: float = 0.0) -> None:
        _check(self._call(self._lib.sc_media_control, what, float(value)))

    def set_shrink(self, shrink: float) -> None:
        self._call(self._lib.sc_media_set_shrink, float(shrink))

    def info(self) -> MediaInfo:
        info = MediaInfo()
        _check(self._call(self._lib.sc_media_info_get, ctypes.byref(info)))
        return info

    def video_frame(self):
        """The picture on screen now, if it isn't the one this took last: (array, channel order), with the order as the compositors
        take it ("I420/709/tv", "I420A/601/pc", "RGBA"). "blank" when nothing should show from now on. None when
        nothing new is due."""
        import numpy as np

        picture = self._picture
        for _ in range(2):
            out = np.empty(max(1, self._capacity), dtype=np.uint8)
            got = self._call(self._lib.sc_media_video_frame, ctypes.byref(self._seen), ctypes.byref(picture),
                             out.ctypes.data, self._capacity)
            if got == MEDIA_TOO_SMALL:
                self._capacity = int(picture.bytes)
                continue
            if got == MEDIA_BLANK:
                return "blank"
            if got != MEDIA_PICTURE:
                if got < 0:
                    _check(got)
                return None
            self._capacity = int(picture.bytes)  # the next one is most likely this size
            kind = _FORMAT_NAMES.get(picture.format, "RGBA")
            if kind.startswith("I420"):
                pixels = out[:picture.bytes].reshape(picture.height, picture.width)
                matrix = picture.matrix if picture.matrix in (601, 709, 2020) else 601
                return pixels, f"{kind}/{matrix}/{'pc' if picture.full_range else 'tv'}"
            return out[:picture.bytes].reshape(picture.height, picture.width, 4), kind
        return None

    def read_audio(self, count: int, asleep: bool = False):
        """The next `count` stereo samples due (float32, shape (count, 2)); silence where there is none."""
        import numpy as np

        out = np.zeros((count, 2), dtype=np.float32)
        try:
            self._call(self._lib.sc_media_read_audio, out.ctypes.data, count, int(asleep))
        except NativeError:  # closed while the mixer was reading: silence
            pass
        return out

    def debug_fault(self, kind: int) -> None:
        """A crash gate check (tests only): the decoder thread faults at its next step."""
        self._call(self._lib.sc_media_debug_fault, kind)

    def before_close(self, fn) -> None:
        """Call fn() just before the player is freed (the engine's sound mixer stops reading it first)."""
        self._closers = [*getattr(self, "_closers", ()), fn]

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, ctypes.c_void_p()
        if handle:
            for fn in getattr(self, "_closers", ()):
                try:
                    fn()
                except Exception:  # noqa: BLE001 (the mixer is gone already: nothing reads the player)
                    log.exception("native media: letting the sound mixer go of a player failed")
            self._closers = []
            self._lib.sc_media_destroy(handle)
            drain_log()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 (interpreter shutting down)
            pass


class Writer:
    """A recording or stream written by a thread of the engine's own (include/sprigcore.h sc_writer): pictures
    (NV12) and sound (48 kHz stereo floats) are queued, and the writer thread encodes, muxes and writes them with
    SprigCast's FFmpeg, never holding Python's lock. Every method is safe from any thread and returns at once
    except :meth:`wait`."""

    def __init__(self, target: str, video_codec: str, width: int, height: int, fps: tuple[int, int] | int = 30,
                 video_kbps: int = 6000, keyframe_frames: int = 60, video_options: dict[str, str] | None = None,
                 audio_tracks: int = 1, audio_kbps: int = 160, container: str | None = None, queue_frames: int = 8,
                 container_options: dict[str, str] | None = None, track_titles: list[str] | None = None,
                 b_frames: int = 0, device: int | None = None, packets: bool = False):
        """packets: encode only (sc_writer_config.packets): ``target`` gets a small NUT file with the streams,
        finished once the writer runs (open it for codec templates); the packets are taken with take_packet.
        ``device``: the engine's ID3D11Device (Engine.device()) for a hardware encoder (h264_nvenc, ...) that
        takes the engine's pictures on the card from a linked share output; pictures from memory go up first."""
        if not media_ready():
            raise NativeError(-2, "the engine's writer isn't available")
        self._lib = _media_lib()
        num, den = (fps, 1) if isinstance(fps, int) else fps
        options = "\n".join(f"{k}={v}" for k, v in (video_options or {}).items())
        muxing = "\n".join(f"{k}={v}" for k, v in (container_options or {}).items())
        titles = "\n".join(t.replace("\n", " ") for t in track_titles or ())
        self._config = WriterConfig(str(target).encode("utf-8"), container.encode() if container else None,
                                    video_codec.encode(), options.encode() if options else None, int(width),
                                    int(height), int(num), int(den), int(video_kbps), int(keyframe_frames),
                                    int(audio_tracks), int(audio_kbps), int(queue_frames),
                                    muxing.encode() if muxing else None, titles.encode() if titles else None,
                                    max(0, min(4, int(b_frames))), device or None, 1 if packets else 0)
        handle = ctypes.c_void_p()
        _check(self._lib.sc_writer_open(ctypes.byref(self._config), ctypes.byref(handle)))
        self._handle = handle
        self._lock = __import__("threading").Lock()
        self.width, self.height, self.tracks = int(width), int(height), int(audio_tracks)
        self.on_card = bool(device)
        self.packets = bool(packets)
        self._packet = WriterPacket()
        self._buffer = (ctypes.c_uint8 * (1 << 20))()

    def _call(self, fn, *args) -> int:
        with self._lock:
            if not self._handle:
                raise NativeError(-1, "the writer is closed")
            return fn(self._handle, *args)

    def video(self, nv12, pts: int, stride: int | None = None) -> int:
        """Queue one NV12 picture (Y rows, then CbCr rows) at ``pts`` (in frames). SC_OK, WRITER_DROPPED (the queue
        was full) or WRITER_REFUSED (finished or failed)."""
        import numpy as np

        data = np.ascontiguousarray(np.frombuffer(nv12, np.uint8) if not isinstance(nv12, np.ndarray) else nv12)
        stride = stride or self.width
        if data.nbytes < stride * self.height * 3 // 2:
            raise NativeError(-1, "the picture is smaller than the writer's size")
        code = self._call(self._lib.sc_writer_video, data.ctypes.data, int(stride), int(pts))
        if code < 0:
            _check(code)
        return code

    def video_span(self, nv12, first: int, last: int, stride: int | None = None) -> int:
        """Queue one NV12 picture for frames first .. last (a hole filled with it): one place in the queue."""
        import numpy as np

        data = np.ascontiguousarray(np.frombuffer(nv12, np.uint8) if not isinstance(nv12, np.ndarray) else nv12)
        stride = stride or self.width
        if data.nbytes < stride * self.height * 3 // 2:
            raise NativeError(-1, "the picture is smaller than the writer's size")
        code = self._call(self._lib.sc_writer_video_span, data.ctypes.data, int(stride), int(first), int(last))
        if code < 0:
            _check(code)
        return code

    def handle_value(self) -> int:
        """The sc_writer pointer, for linking an engine output to it (Engine.link_output)."""
        with self._lock:
            if not self._handle:
                raise NativeError(-1, "the writer is closed")
            return self._handle.value

    def take_packet(self):
        """Packets mode: the oldest encoded packet as (track, keyframe, pts, dts, (tb_num, tb_den), bytes), track 0
        the video and 1.. the sound; None when none is waiting."""
        info = self._packet
        while True:
            code = self._call(self._lib.sc_writer_packet_take, ctypes.byref(info), self._buffer, len(self._buffer))
            if code == 1:
                return (info.track, bool(info.keyframe), info.pts, info.dts, (info.tb_num, info.tb_den),
                        ctypes.string_at(self._buffer, info.size))
            if code == 0:
                return None
            if code == -1 and info.size > len(self._buffer):  # SC_ERR_ARGUMENT: no room
                self._buffer = (ctypes.c_uint8 * (info.size * 2))()  # a big keyframe: room for it, then again
                continue
            _check(code)

    def set_bitrate(self, kbps: int, max_kbps: int = 0, buffer_kbits: int = 0) -> None:
        """The video bitrate from the next picture on (a stream steering itself)."""
        _check(self._call(self._lib.sc_writer_set_bitrate, int(kbps), int(max_kbps or 0), int(buffer_kbits or 0)))

    def keyframe(self) -> None:
        """The next picture is a keyframe."""
        _check(self._call(self._lib.sc_writer_keyframe))

    def audio(self, block, track: int = 0) -> int:
        """Queue sound for ``track``: (n, 2) float32, 48 kHz, following the last block."""
        import numpy as np

        data = np.ascontiguousarray(block, dtype=np.float32).reshape(-1, 2)
        code = self._call(self._lib.sc_writer_audio, int(track), data.ctypes.data, int(data.shape[0]))
        if code < 0:
            _check(code)
        return code

    def finish(self) -> None:
        """Write what is queued, end the encoders and close the file (on the writer's thread)."""
        _check(self._call(self._lib.sc_writer_finish))

    def wait(self, timeout: float) -> bool:
        """True once the writer's thread is done (finished or failed)."""
        code = self._call(self._lib.sc_writer_wait, int(max(0.0, timeout) * 1000))
        if code < 0:
            _check(code)
        return code == 1

    def stats(self) -> WriterStats:
        out = WriterStats()
        _check(self._call(self._lib.sc_writer_stats_get, ctypes.byref(out)))
        return out

    def close(self) -> None:
        """Give up whatever is still unwritten and free the writer (finish() and wait() first to keep it)."""
        with self._lock:
            handle, self._handle = self._handle, None
        if handle:
            self._lib.sc_writer_destroy(handle)
            drain_log()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 (interpreter shutting down)
            pass


class SoundCapture:
    """A microphone or desktop sound the engine reads itself (include/sprigcore.h sc_capture): WASAPI on a thread of
    its own, pushed straight into one sound of a SoundMixer. Made by SoundMixer.capture; closed before the mixer."""

    def __init__(self, lib, handle: ctypes.c_void_p, mixer: SoundMixer):
        self._lib, self._handle, self._mixer = lib, handle, mixer
        self._stats = CaptureStats()
        self._lock = __import__("threading").Lock()

    def stats(self) -> dict:
        """{"state": "capturing" ..., "device", "error", "frames", "gaps", "channels", "rate"}."""
        with self._lock:
            if not self._handle:
                return {"state": "closed", "device": "", "error": "", "frames": 0, "gaps": 0, "channels": 0,
                        "rate": 0}
            _check(self._lib.sc_capture_stats_get(self._handle, ctypes.byref(self._stats)))
            st = self._stats
            state = CAPTURE_STATES[st.state] if 0 <= st.state < len(CAPTURE_STATES) else "failed"
            return {"state": state, "device": st.device, "error": st.error.decode("utf-8", "replace"),
                    "frames": int(st.frames), "gaps": int(st.gaps), "channels": st.channels, "rate": st.rate}

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
        if handle:
            self._lib.sc_capture_close(handle)  # joins its thread: no push after this
        mixer = self._mixer
        if mixer is not None:
            captures = mixer.__dict__.get("_captures", [])
            if self in captures:
                captures.remove(self)

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 (never from a destructor)
            pass


class SoundMixer:
    """The engine's sound mixer (include/sprigcore.h sc_mixer): one 48 kHz stereo mix of every sound, mixed on a
    thread of the engine's own (clocked) so it never waits on Python. audio/native_mixer.py drives it like
    audio/mixer.py's Mixer. Every method is safe from any thread."""

    def __init__(self, clocked: bool = True):
        lib = _media_lib()
        if lib is None:
            raise NativeError(-5, "the native engine isn't available")
        self._lib = lib
        handle = ctypes.c_void_p()
        _check(lib.sc_mixer_create(int(clocked), ctypes.byref(handle)))
        self._handle = handle
        self._lock = __import__("threading").Lock()
        self._level = MixerLevel()

    def _call(self, fn, *args) -> int:
        handle = self._handle
        if not handle:
            raise NativeError(-1, "the mixer is closed")
        return fn(handle, *args)

    @staticmethod
    def _settings(gain=1.0, mono=False, balance=0.0, delay_ms=0, tracks=(), monitor=False, voice=False,
                  bloom_out=False) -> MixerSourceSettings:
        mask = 0
        for name in tracks:
            if name in MIXER_TRACKS:
                mask |= 1 << MIXER_TRACKS.index(name)
        return MixerSourceSettings(float(gain), int(bool(mono)), float(balance), int(delay_ms), mask,
                                   int(bool(monitor)), int(bool(voice)), int(bool(bloom_out)))

    def add_source(self, **settings) -> int:
        out = ctypes.c_int()
        _check(self._call(self._lib.sc_mixer_source_add, ctypes.byref(self._settings(**settings)), ctypes.byref(out)))
        return out.value

    def set_source(self, source: int, **settings) -> None:
        _check(self._call(self._lib.sc_mixer_source_set, int(source), ctypes.byref(self._settings(**settings))))

    def remove_source(self, source: int) -> None:
        _check(self._call(self._lib.sc_mixer_source_remove, int(source)))

    def push(self, source: int, block) -> None:
        """Samples a sound made: float32 (n, 2) at 48 kHz."""
        import numpy as np

        data = np.ascontiguousarray(block, dtype=np.float32)
        _check(self._call(self._lib.sc_mixer_source_push, int(source), data.ctypes.data, int(data.shape[0])))

    def link_media(self, source: int, player: MediaPlayer | None, asleep: bool = False) -> None:
        """The sound reads straight from an engine player (None: pushed again)."""
        if player is None:
            _check(self._call(self._lib.sc_mixer_source_media, int(source), None, 0))
            return
        with player._lock:  # against close(): it swaps the handle under this lock, then unlinks, then frees
            if not player._handle:
                raise NativeError(-1, "the player is closed")
            _check(self._call(self._lib.sc_mixer_source_media, int(source), player._handle, int(bool(asleep))))

    def set_ducking(self, enabled: bool, amount_db: float, threshold_db: float, attack_ms: float,
                    release_ms: float, hold_ms: float) -> None:
        d = MixerDucking(int(bool(enabled)), float(amount_db), float(threshold_db), float(attack_ms),
                         float(release_ms), float(hold_ms))
        _check(self._call(self._lib.sc_mixer_set_ducking, ctypes.byref(d)))

    def add_tap(self, what: list[int]) -> int:
        entries = (ctypes.c_int * len(what))(*what)
        out = ctypes.c_int()
        _check(self._call(self._lib.sc_mixer_tap_add, entries, len(what), ctypes.byref(out)))
        return out.value

    def remove_tap(self, tap: int) -> None:
        _check(self._call(self._lib.sc_mixer_tap_remove, int(tap)))

    def take(self, tap: int, entries: int):
        """The oldest waiting block of a tap: (index, float32 array (entries, BLOCK, 2), dropped so far) or None."""
        import numpy as np

        out = np.empty((entries, MIXER_BLOCK, 2), np.float32)
        index, dropped = ctypes.c_int64(), ctypes.c_uint64()
        got = self._call(self._lib.sc_mixer_tap_take, int(tap), out.ctypes.data, out.size, ctypes.byref(index),
                         ctypes.byref(dropped))
        _check(min(got, 0))
        return (index.value, out, dropped.value) if got == 1 else None

    def tick(self, count: int = 1) -> None:
        _check(self._call(self._lib.sc_mixer_tick, int(count)))

    def wait(self, after: int, timeout_ms: int) -> int:
        """Blocks mixed so far, once more than `after` (or the timeout passed). Python's lock is let go meanwhile."""
        now = ctypes.c_int64()
        _check(min(0, self._call(self._lib.sc_mixer_wait, ctypes.c_int64(after), int(timeout_ms), ctypes.byref(now))))
        return now.value

    def levels(self, source: int) -> tuple[float, float, float, int]:
        """(peak dB since last asked, last block's RMS dB, level, underruns); MIXER_MASTER: the stream track,
        with level = the ducking gain."""
        with self._lock:
            _check(self._call(self._lib.sc_mixer_levels, int(source), ctypes.byref(self._level)))
            lv = self._level
            return lv.peak_db, lv.rms_db, lv.level, int(lv.underruns)

    def capture(self, source: int, device: str = "", loopback: bool = False) -> SoundCapture:
        """Read a microphone or desktop sound (by name; "" = the Windows default) straight into this sound."""
        with self._lock:
            handle = self._handle
            if not handle:
                raise NativeError(-1, "the mixer is closed")
            out = ctypes.c_void_p()
            _check(self._lib.sc_capture_open(handle, int(source), device or "", int(bool(loopback)),
                                             ctypes.byref(out)))
            made = SoundCapture(self._lib, out, self)
            self.__dict__.setdefault("_captures", []).append(made)
            return made

    def close(self) -> None:
        with self._lock:
            captures, self.__dict__["_captures"] = self.__dict__.get("_captures", []), []
        for c in captures:  # they push into this mixer: they end first
            c.close()
        handle, self._handle = self._handle, None
        if handle:
            self._lib.sc_mixer_destroy(handle)

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 (never from a destructor)
            pass
