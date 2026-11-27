"""A webcam's own pan, tilt and zoom, for the Move plugin's camera moves (Windows only).

Windows lets an app turn a camera's controls through DirectShow (IAMCameraControl) while another part of the app
reads its picture: the controls belong to the device, not to whoever opened it. Written from Microsoft's public
DirectShow interface descriptions with ctypes, so there's nothing extra to install.

Use it from one worker thread (open_camera, then range/get/set, then close on that same thread): the calls can
block for a moment while the camera turns.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import uuid
from ctypes import POINTER, byref, c_long, c_ulong, c_void_p

log = logging.getLogger(__name__)

PAN, TILT, ROLL, ZOOM, EXPOSURE, IRIS, FOCUS = range(7)  # CameraControlProperty
_MANUAL = 2  # CameraControl_Flags_Manual


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]


def _guid(text: str) -> _GUID:
    g = _GUID()
    ctypes.memmove(byref(g), uuid.UUID(text).bytes_le, 16)
    return g


_CLSID_SYSTEM_DEVICE_ENUM = "62BE5D10-60EB-11d0-BD3B-00A0C911CE86"
_IID_CREATE_DEV_ENUM = "29840822-5B84-11D0-BD3B-00A0C911CE86"
_CLSID_VIDEO_INPUT_CATEGORY = "860BB310-5D01-11d0-BD3B-00A0C911CE86"
_IID_BASE_FILTER = "56A86895-0AD4-11CE-B03A-0020AF0BA770"
_IID_CAMERA_CONTROL = "C6E13370-30AC-11d0-A18C-00A0C9118956"


def _method(ptr: c_void_p, index: int, restype, *argtypes):
    """A COM method by its place in the interface's table (the first argument, the interface, is added)."""
    table = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
    return ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)(table[index])


def _release(ptr: c_void_p) -> None:
    if ptr and ptr.value:
        try:
            _method(ptr, 2, c_ulong)(ptr)
        except OSError:
            pass


def _plain_path(text: str) -> str:
    """A device path as both DirectShow's names and the camera list spell it, for comparing."""
    text = text.lower().replace("@device:pnp:", "")
    return text.split("#{")[0].lstrip("\\?")


def path_matches(display_name: str, path: str) -> bool:
    if not path:
        return False
    a, b = _plain_path(display_name), _plain_path(path)
    return bool(a) and bool(b) and (a in b or b in a)


class CameraControl:
    """One camera's pan, tilt and zoom (and the other IAMCameraControl properties)."""

    def __init__(self, control: c_void_p, filt: c_void_p):
        self._control, self._filter = control, filt

    def range(self, prop: int) -> tuple[int, int, int, int] | None:
        """(lowest, highest, step, default), or None when this camera can't do it."""
        lo, hi, step, default, caps = c_long(), c_long(), c_long(), c_long(), c_long()
        try:
            _method(self._control, 3, ctypes.HRESULT, c_long, *(POINTER(c_long),) * 5)(
                self._control, prop, byref(lo), byref(hi), byref(step), byref(default), byref(caps))
        except OSError:
            return None
        if hi.value <= lo.value:
            return None
        return lo.value, hi.value, max(1, step.value), default.value

    def get(self, prop: int) -> int | None:
        value, flags = c_long(), c_long()
        try:
            _method(self._control, 5, ctypes.HRESULT, c_long, POINTER(c_long), POINTER(c_long))(
                self._control, prop, byref(value), byref(flags))
        except OSError:
            return None
        return value.value

    def set(self, prop: int, value: int) -> bool:
        try:
            _method(self._control, 4, ctypes.HRESULT, c_long, c_long, c_long)(self._control, prop, int(value), _MANUAL)
        except OSError:
            return False
        return True

    def close(self) -> None:
        _release(self._control)
        _release(self._filter)
        self._control = self._filter = c_void_p()


def open_camera(name: str, path: str, index: int | None) -> CameraControl | None:
    """The camera with this device path (else the index-th video device, the order the camera list uses).
    None off Windows, or when the camera has no controls Windows can turn."""
    if sys.platform != "win32":
        return None
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx(None, 0)  # this worker thread joins the multi-threaded apartment
    devices, listing, context = c_void_p(), c_void_p(), c_void_p()
    try:
        if ole32.CoCreateInstance(byref(_guid(_CLSID_SYSTEM_DEVICE_ENUM)), None, 1,
                                  byref(_guid(_IID_CREATE_DEV_ENUM)), byref(devices)) != 0:
            return None
        _method(devices, 3, ctypes.HRESULT, POINTER(_GUID), POINTER(c_void_p), c_ulong)(
            devices, byref(_guid(_CLSID_VIDEO_INPUT_CATEGORY)), byref(listing), 0)
        if not listing.value:
            return None  # no cameras at all
        ole32.CreateBindCtx(0, byref(context))
        by_index = None
        at = 0
        while True:
            moniker, fetched = c_void_p(), c_ulong()
            _method(listing, 3, ctypes.HRESULT, c_ulong, POINTER(c_void_p), POINTER(c_ulong))(
                listing, 1, byref(moniker), byref(fetched))
            if not fetched.value:
                break
            shown = c_void_p()
            text = ""
            try:
                _method(moniker, 20, ctypes.HRESULT, c_void_p, c_void_p, POINTER(c_void_p))(
                    moniker, context, None, byref(shown))
                text = ctypes.wstring_at(shown.value) if shown.value else ""
                ole32.CoTaskMemFree(shown)
            except OSError:
                pass
            if path_matches(text, path):
                _release(by_index)
                return _controls(moniker, context)
            if index is not None and at == index and by_index is None:
                by_index = moniker
            else:
                _release(moniker)
            at += 1
        if by_index is not None:
            return _controls(by_index, context)
        return None
    except OSError as exc:
        log.info("camera controls: %s could not be opened (%s)", name or "camera", exc)
        return None
    finally:
        _release(context)
        _release(listing)
        _release(devices)


def _controls(moniker: c_void_p, context: c_void_p) -> CameraControl | None:
    filt, control = c_void_p(), c_void_p()
    try:
        _method(moniker, 8, ctypes.HRESULT, c_void_p, c_void_p, POINTER(_GUID), POINTER(c_void_p))(
            moniker, context, None, byref(_guid(_IID_BASE_FILTER)), byref(filt))
        _method(filt, 0, ctypes.HRESULT, POINTER(_GUID), POINTER(c_void_p))(
            filt, byref(_guid(_IID_CAMERA_CONTROL)), byref(control))
    except OSError:
        _release(filt)
        return None  # this camera has no pan, tilt or zoom
    finally:
        _release(moniker)
    return CameraControl(control, filt)
