"""Which speakers and microphone Windows uses as the default right now.

PortAudio learns the default devices once, when it starts, and can only look again by restarting, which is
unsafe while any sound is being captured. So a "Default" desktop sound kept capturing the old speakers after
someone switched Windows to their headphones, until SprigCast restarted (OBS follows the switch). This asks
Windows directly (the Core Audio device enumerator, over plain COM calls) for the default device's ID and
name; the audio sources compare it every couple of seconds and move over when it changes.

Off Windows, or if Windows won't say, default_endpoint() returns None and nothing changes.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import uuid

log = logging.getLogger(__name__)

E_RENDER, E_CAPTURE = 0, 1  # EDataFlow
E_CONSOLE = 0  # ERole: Windows' "Default Device" (eMultimedia follows it on every PC we know of)
VT_LPWSTR = 31
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106: COM already started on this thread another way (fine)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text: str) -> _GUID:
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_uint32)]


class _PROPVARIANT(ctypes.Structure):  # only the string case is read; 24 bytes on 64-bit Windows
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("pwszVal", ctypes.c_void_p), ("pad", ctypes.c_void_p)]


CLSID_MMDeviceEnumerator = _GUID.of("BCDE0395-E52F-467C-8E3D-C4579291692E")
IID_IMMDeviceEnumerator = _GUID.of("A95664D2-9614-4F35-A746-DE8DB63617E6")
PKEY_Device_FriendlyName = _PROPERTYKEY(_GUID.of("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 14)
CLSCTX_ALL = 0x17
STGM_READ = 0

_local = threading.local()
_protos: dict = {}


def _method(obj: ctypes.c_void_p, index: int, *argtypes):
    """The COM method at this place in obj's table, callable with obj first. Prototypes are made once each
    (a ctypes type made per call is never freed)."""
    key = (index, argtypes)
    proto = _protos.get(key)
    if proto is None:
        proto = _protos[key] = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)  # type: ignore[attr-defined]
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return proto(table[index])


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(obj)  # type: ignore[attr-defined]


def _com_ready() -> bool:
    if getattr(_local, "ready", None) is None:
        try:
            ctypes.oledll.ole32.CoInitializeEx(None, 0)  # type: ignore[attr-defined]  # COINIT_MULTITHREADED
            _local.ready = True
        except OSError as exc:
            _local.ready = getattr(exc, "winerror", None) == _RPC_E_CHANGED_MODE
    return bool(_local.ready)


def default_endpoint(render: bool) -> tuple[str, str] | None:
    """(Windows' ID, name) of the default speakers (render=True) or microphone, or None if there is none or it
    can't be asked. The name is the one PortAudio lists (desktop sound adds " [Loopback]")."""
    if sys.platform != "win32" or not _com_ready():
        return None
    ole32 = ctypes.oledll.ole32  # type: ignore[attr-defined]
    enumerator, device, store = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL,
                               ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enumerator))
        _method(enumerator, 4, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            enumerator, E_RENDER if render else E_CAPTURE, E_CONSOLE, ctypes.byref(device))
        raw_id = ctypes.c_void_p()
        _method(device, 5, ctypes.POINTER(ctypes.c_void_p))(device, ctypes.byref(raw_id))
        device_id = ctypes.wstring_at(raw_id.value) if raw_id.value else ""
        ctypes.windll.ole32.CoTaskMemFree(raw_id)  # type: ignore[attr-defined]  # returns nothing: not oledll
        _method(device, 4, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))(device, STGM_READ, ctypes.byref(store))
        value = _PROPVARIANT()
        _method(store, 5, ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT))(
            store, ctypes.byref(PKEY_Device_FriendlyName), ctypes.byref(value))
        name = ctypes.wstring_at(value.pwszVal) if value.vt == VT_LPWSTR and value.pwszVal else ""
        ole32.PropVariantClear(ctypes.byref(value))
        return (device_id, name) if device_id else None
    except OSError as exc:  # no device of that kind (E_NOTFOUND), or Windows audio not running
        log.debug("default %s device: %s", "output" if render else "input", exc)
        return None
    finally:
        for obj in (store, device, enumerator):
            _release(obj)
