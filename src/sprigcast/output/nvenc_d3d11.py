"""NVENC through Direct3D 11, talking to the driver directly (Windows + NVIDIA only).

This is how OBS reaches NVENC: a Direct3D 11 device instead of a CUDA context. FFmpeg (through PyAV)
can only use CUDA here, and a CUDA context costs about 100 MB of RAM on Windows. For now this is a
measuring tool (`sprigcast-memcheck --encoders`); if it proves much leaner it becomes an encoder path.

The structures follow nvEncodeAPI.h from NVIDIA's Video Codec SDK 12.1 and 13.0: every field we touch
sits at the same place in both, only the version tags differ (see API_LAYOUTS).

One process can only speak one NVENC version: the driver keeps the version of the most recent
NvEncodeAPICreateInstance call. FFmpeg 8 (PyAV 19) speaks 13.0, so once a stream had opened FFmpeg's
NVENC, our 12.1 sessions failed with INVALID_VERSION. We now speak the newest version the driver has
(13.0, 12.2, 12.1) and try the next one (calling CreateInstance again) when a session open says
INVALID_VERSION. The 12.2 and 13.0 struct layouts match 12.1 for every field used here.
Frames go in as NV12 bytes and come out as H.264 Annex B packets. Synchronous mode, no B-frames.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_char_p,
    c_int,
    c_uint8,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
)

# Struct version numbers for each API version we speak (NV_ENC_*_VER in nvEncodeAPI.h), newest first.
API_LAYOUTS: dict[tuple[int, int], dict[str, int]] = {
    (13, 0): {"init": 7, "config": 9, "preset": 5, "pic": 7, "lock_bitstream": 2, "create_input": 2},
    (12, 2): {"init": 7, "config": 9, "preset": 5, "pic": 7, "lock_bitstream": 2, "create_input": 2},
    (12, 1): {"init": 6, "config": 8, "preset": 4, "pic": 6, "lock_bitstream": 1, "create_input": 1},
}
API_MAJOR, API_MINOR = min(API_LAYOUTS)  # the oldest we can speak: the driver must support at least this
API_VERSION = API_MAJOR | (API_MINOR << 24)


def _api_version(api: tuple[int, int]) -> int:
    return api[0] | (api[1] << 24)


def _struct_version(ver: int, high_bit: bool = False, api: tuple[int, int] = (API_MAJOR, API_MINOR)) -> int:
    v = _api_version(api) | (ver << 16) | (0x7 << 28)
    return v | (1 << 31) if high_bit else v


class GUID(Structure):
    _fields_ = [("Data1", c_uint32), ("Data2", c_uint16), ("Data3", c_uint16), ("Data4", c_uint8 * 8)]


def _guid(d1, d2, d3, d4) -> GUID:
    return GUID(d1, d2, d3, (c_uint8 * 8)(*d4))


H264 = _guid(0x6BC82762, 0x4E63, 0x4CA4, (0xAA, 0x85, 0x1E, 0x50, 0xF3, 0x21, 0xF6, 0xBF))
PRESETS = {
    "p1": _guid(0xFC0A8D3E, 0x45F8, 0x4CF8, (0x80, 0xC7, 0x29, 0x88, 0x71, 0x59, 0x0E, 0xBF)),
    "p2": _guid(0xF581CFB8, 0x88D6, 0x4381, (0x93, 0xF0, 0xDF, 0x13, 0xF9, 0xC2, 0x7D, 0xAB)),
    "p3": _guid(0x36850110, 0x3A07, 0x441F, (0x94, 0xD5, 0x36, 0x70, 0x63, 0x1F, 0x91, 0xF6)),
    "p4": _guid(0x90A7B826, 0xDF06, 0x4862, (0xB9, 0xD2, 0xCD, 0x6D, 0x73, 0xA0, 0x86, 0x81)),
    "p5": _guid(0x21C6E6B4, 0x297A, 0x4CBA, (0x99, 0x8F, 0xB6, 0xCB, 0xDE, 0x72, 0xAD, 0xE3)),
    "p6": _guid(0x8E75C279, 0x6299, 0x4AB6, (0x83, 0x02, 0x0B, 0x21, 0x5A, 0x33, 0x5C, 0xF5)),
    "p7": _guid(0x84848C12, 0x6F71, 0x4C13, (0x93, 0x1B, 0x53, 0xE2, 0x83, 0xF5, 0x79, 0x74)),
}
PRESET_P1, PRESET_P4 = PRESETS["p1"], PRESETS["p4"]
TUNINGS = {"hq": 1, "ll": 2, "ull": 3}
RATE_CONTROL = {"cqp": 0, "vbr": 1, "cbr": 2}

DEVICE_TYPE_DIRECTX = 0
BUFFER_FORMAT_NV12 = 1
TUNING_LOW_LATENCY = 2
PIC_STRUCT_FRAME = 1
PIC_FLAG_FORCEIDR, PIC_FLAG_OUTPUT_SPSPPS, PIC_FLAG_EOS = 0x2, 0x4, 0x8
PIC_TYPE_IDR = 3
# Byte offsets in NV_ENC_PRESET_CONFIG (5128 bytes) from nvEncodeAPI.h 12.1, checked in tests/test_nvenc_d3d11.py.
PRESET_CONFIG_SIZE, CONFIG_AT = 5128, 8
CFG_GOP, CFG_FRAME_INTERVAL_P, CFG_RC, CFG_H264 = 20, 24, 40, 168
RC_MODE, RC_CONST_QP, RC_AVG_BITRATE, RC_MAX_BITRATE, RC_VBV_SIZE, RC_VBV_DELAY = 4, 8, 20, 24, 28, 32
H264_IDR_PERIOD = 8
STATUS_NAMES = [
    "SUCCESS", "NO_ENCODE_DEVICE", "UNSUPPORTED_DEVICE", "INVALID_ENCODERDEVICE", "INVALID_DEVICE",
    "DEVICE_NOT_EXIST", "INVALID_PTR", "INVALID_EVENT", "INVALID_PARAM", "INVALID_CALL", "OUT_OF_MEMORY",
    "ENCODER_NOT_INITIALIZED", "UNSUPPORTED_PARAM", "LOCK_BUSY", "NOT_ENOUGH_BUFFER", "INVALID_VERSION",
    "MAP_FAILED", "NEED_MORE_INPUT", "ENCODER_BUSY", "EVENT_NOT_REGISTERD", "GENERIC",
    "INCOMPATIBLE_CLIENT_KEY", "UNIMPLEMENTED", "RESOURCE_REGISTER_FAILED", "RESOURCE_NOT_REGISTERED",
    "RESOURCE_NOT_MAPPED", "NEED_MORE_OUTPUT",
]


class FunctionList(Structure):
    _fields_ = [("version", c_uint32), ("reserved", c_uint32), ("fn", c_void_p * 319)]


# Positions in FunctionList.fn (the order of NV_ENCODE_API_FUNCTION_LIST).
F_INITIALIZE, F_CREATE_INPUT, F_DESTROY_INPUT, F_CREATE_BITSTREAM, F_DESTROY_BITSTREAM = 11, 12, 13, 14, 15
F_ENCODE, F_LOCK_BITSTREAM, F_UNLOCK_BITSTREAM, F_LOCK_INPUT, F_UNLOCK_INPUT = 16, 17, 18, 19, 20
F_DESTROY_ENCODER, F_OPEN_SESSION_EX, F_LAST_ERROR, F_PRESET_CONFIG_EX = 27, 29, 37, 39


class OpenSessionParams(Structure):
    _fields_ = [("version", c_uint32), ("deviceType", c_uint32), ("device", c_void_p), ("reserved", c_void_p),
                ("apiVersion", c_uint32), ("reserved1", c_uint32 * 253), ("reserved2", c_void_p * 64)]


class MEHintCounts(Structure):
    _fields_ = [("bits", c_uint32), ("reserved1", c_uint32 * 3)]


class InitializeParams(Structure):
    _fields_ = [
        ("version", c_uint32), ("encodeGUID", GUID), ("presetGUID", GUID),
        ("encodeWidth", c_uint32), ("encodeHeight", c_uint32), ("darWidth", c_uint32), ("darHeight", c_uint32),
        ("frameRateNum", c_uint32), ("frameRateDen", c_uint32), ("enableEncodeAsync", c_uint32),
        ("enablePTD", c_uint32), ("bits", c_uint32), ("privDataSize", c_uint32), ("privData", c_void_p),
        ("encodeConfig", c_void_p), ("maxEncodeWidth", c_uint32), ("maxEncodeHeight", c_uint32),
        ("maxMEHintCountsPerBlock", MEHintCounts * 2), ("tuningInfo", c_int), ("bufferFormat", c_int),
        ("numStateBuffers", c_uint32), ("outputStatsLevel", c_int),
        ("reserved", c_uint32 * 285), ("reserved2", c_void_p * 64),
    ]


class CreateInputBuffer(Structure):
    _fields_ = [("version", c_uint32), ("width", c_uint32), ("height", c_uint32), ("memoryHeap", c_int),
                ("bufferFmt", c_int), ("reserved", c_uint32), ("inputBuffer", c_void_p),
                ("pSysMemBuffer", c_void_p), ("reserved1", c_uint32 * 57), ("reserved2", c_void_p * 63)]


class CreateBitstreamBuffer(Structure):
    _fields_ = [("version", c_uint32), ("size", c_uint32), ("memoryHeap", c_int), ("reserved", c_uint32),
                ("bitstreamBuffer", c_void_p), ("bitstreamBufferPtr", c_void_p),
                ("reserved1", c_uint32 * 58), ("reserved2", c_void_p * 64)]


class LockInputBuffer(Structure):
    _fields_ = [("version", c_uint32), ("bits", c_uint32), ("inputBuffer", c_void_p),
                ("bufferDataPtr", c_void_p), ("pitch", c_uint32),
                ("reserved1", c_uint32 * 251), ("reserved2", c_void_p * 64)]


class LockBitstream(Structure):
    _fields_ = [
        ("version", c_uint32), ("bits", c_uint32), ("outputBitstream", c_void_p), ("sliceOffsets", c_void_p),
        ("frameIdx", c_uint32), ("hwEncodeStatus", c_uint32), ("numSlices", c_uint32),
        ("bitstreamSizeInBytes", c_uint32), ("outputTimeStamp", c_uint64), ("outputDuration", c_uint64),
        ("bitstreamBufferPtr", c_void_p), ("pictureType", c_int), ("pictureStruct", c_int),
        ("frameAvgQP", c_uint32), ("frameSatd", c_uint32), ("ltrFrameIdx", c_uint32),
        ("ltrFrameBitmap", c_uint32), ("temporalId", c_uint32), ("intraMBCount", c_uint32),
        ("interMBCount", c_uint32), ("averageMVX", c_int), ("averageMVY", c_int),
        ("alphaLayerSizeInBytes", c_uint32), ("outputStatsPtrSize", c_uint32), ("outputStatsPtr", c_void_p),
        ("frameIdxDisplay", c_uint32), ("reserved1", c_uint32 * 220), ("reserved2", c_void_p * 63),
        ("reservedInternal", c_uint32 * 8),
    ]


class PicParamsHead(Structure):
    """The first fields of NV_ENC_PIC_PARAMS; the rest (codec-specific, all zero here) follows in a padded buffer."""
    _fields_ = [("version", c_uint32), ("inputWidth", c_uint32), ("inputHeight", c_uint32),
                ("inputPitch", c_uint32), ("encodePicFlags", c_uint32), ("frameIdx", c_uint32),
                ("inputTimeStamp", c_uint64), ("inputDuration", c_uint64), ("inputBuffer", c_void_p),
                ("outputBitstream", c_void_p), ("completionEvent", c_void_p), ("bufferFmt", c_int),
                ("pictureStruct", c_int), ("pictureType", c_int)]


class PicParams(Structure):
    _fields_ = [("head", PicParamsHead), ("rest", c_uint8 * 16384)]  # far more than the real struct needs


class NvencError(RuntimeError):
    pass


_api = None  # the function list from the latest NvEncodeAPICreateInstance
_api_for: tuple[int, int] | None = None  # ... and the API version it was made for
_lib = None
_supported: tuple[int, int] | None = None
_preferred: tuple[int, int] | None = None  # the version that last opened a session
_opening = threading.Lock()  # one session open (and CreateInstance switch) at a time


def speakable() -> list[tuple[int, int]]:
    """The API versions to try, best first: the one that worked last, then newest to oldest."""
    _load()
    order = [api for api in API_LAYOUTS if api <= (_supported or (0, 0))]
    if _preferred in order:
        order.remove(_preferred)
        order.insert(0, _preferred)
    return order


def _load(api: tuple[int, int] | None = None):
    """The NVENC entry points. Raises NvencError where NVENC can't be used.

    With `api`, (re)creates the function list for that version, which also tells the driver which
    version this process speaks from now on.
    """
    global _api, _api_for, _lib, _supported
    if _api is not None and (api is None or api == _api_for):
        return _api
    if _lib is None:
        _lib, _supported = _open_library()
    if api is None:
        api = next(a for a in API_LAYOUTS if a <= _supported)
    functions = FunctionList(version=_struct_version(2, api=api))
    status = _lib.NvEncodeAPICreateInstance(byref(functions))
    if status != 0:
        raise NvencError(f"NvEncodeAPICreateInstance failed: {_status_name(status)}")
    _api, _api_for = functions, api
    return _api


def _open_library():
    if sys.platform != "win32":
        raise NvencError("needs Windows")
    try:
        lib = ctypes.WinDLL("nvEncodeAPI64.dll")
    except OSError as exc:
        raise NvencError(f"no NVIDIA encoder driver ({exc})") from exc
    supported = c_uint32()
    if lib.NvEncodeAPIGetMaxSupportedVersion(byref(supported)) != 0:
        raise NvencError("the driver didn't report its NVENC version")
    if (supported.value >> 4, supported.value & 0xF) < (API_MAJOR, API_MINOR):
        raise NvencError(f"driver supports NVENC {supported.value >> 4}.{supported.value & 0xF}, "
                         f"needs {API_MAJOR}.{API_MINOR} (update the NVIDIA driver)")
    return lib, (supported.value >> 4, supported.value & 0xF)


def _status_name(status: int) -> str:
    return STATUS_NAMES[status] if 0 <= status < len(STATUS_NAMES) else str(status)


def _fn(index: int, *argtypes):
    pointer = _load().fn[index]
    if not pointer:
        raise NvencError(f"the driver has no NVENC function #{index}")
    return ctypes.WINFUNCTYPE(c_int, *argtypes)(pointer)


def d3d11_device() -> c_void_p:
    """A Direct3D 11 device on the default adapter (the device NVENC is opened on)."""
    d3d11 = ctypes.WinDLL("d3d11.dll")
    device, context, level = c_void_p(), c_void_p(), c_uint32()
    D3D_DRIVER_TYPE_HARDWARE, D3D11_SDK_VERSION = 1, 7
    hr = d3d11.D3D11CreateDevice(None, D3D_DRIVER_TYPE_HARDWARE, None, 0, None, 0, D3D11_SDK_VERSION,
                                 byref(device), byref(level), byref(context))
    if hr != 0:
        raise NvencError(f"D3D11CreateDevice failed (0x{hr & 0xFFFFFFFF:08x})")
    _release(context)
    return device


def _release(com: c_void_p) -> None:
    """IUnknown::Release (vtable slot 2)."""
    if com:
        vtable = ctypes.cast(com, POINTER(POINTER(c_void_p))).contents
        ctypes.WINFUNCTYPE(c_uint32, c_void_p)(vtable[2])(com)


class D3D11Encoder:
    """One H.264 encoder on its own Direct3D 11 device. encode() takes NV12 bytes, returns packets.

    Never uses B-frames, so every packet comes out as soon as its frame went in, in order.
    """

    def __init__(self, width: int, height: int, fps: int, preset: GUID | str = PRESET_P4, *,
                 bitrate_kbps: int = 0, rate_control: str = "vbr", max_kbps: int = 0, buffer_kbits: int = 0,
                 cq_level: int = 23, gop_frames: int = 0, tuning: str = "ll"):
        self.width, self.height = width, height
        self.device = c_void_p()
        self.encoder = c_void_p()
        self.input = self.output = None
        self.frame = 0
        self.api = (API_MAJOR, API_MINOR)
        _load()  # says why NVENC can't be used (not Windows, no NVIDIA driver, old driver) before anything else
        preset = PRESETS.get(preset, PRESET_P4) if isinstance(preset, str) else preset
        tuning_info = TUNINGS.get(tuning, TUNINGS["ll"])
        self.device = d3d11_device()
        try:
            self._open_session()
            config = None
            if bitrate_kbps or gop_frames or rate_control == "cqp":
                config = self._config(preset, tuning_info, bitrate_kbps, rate_control, max_kbps, buffer_kbits,
                                      cq_level, gop_frames)
            init = InitializeParams(
                version=self._v("init", True), encodeGUID=H264, presetGUID=preset,
                encodeWidth=width, encodeHeight=height, darWidth=width, darHeight=height,
                frameRateNum=fps, frameRateDen=1, enableEncodeAsync=0, enablePTD=1, tuningInfo=tuning_info,
                encodeConfig=ctypes.addressof(config) + CONFIG_AT if config is not None else None,
            )
            self._check(_fn(F_INITIALIZE, c_void_p, c_void_p)(self.encoder, ctypes.addressof(init)), "initialising")
            buf = CreateInputBuffer(version=self._v("create_input"), width=width, height=height,
                                    bufferFmt=BUFFER_FORMAT_NV12)
            self._check(_fn(F_CREATE_INPUT, c_void_p, c_void_p)(self.encoder, ctypes.addressof(buf)),
                        "making an input buffer")
            self.input = buf.inputBuffer
            out = CreateBitstreamBuffer(version=_struct_version(1, api=self.api))
            self._check(_fn(F_CREATE_BITSTREAM, c_void_p, c_void_p)(self.encoder, ctypes.addressof(out)),
                        "making an output buffer")
            self.output = out.bitstreamBuffer
        except Exception:
            self.close()
            raise

    def _open_session(self) -> None:
        """Open the session in a version the driver accepts in this process (see the module notes)."""
        with _opening:
            self._open_session_locked()

    def _open_session_locked(self) -> None:
        global _preferred
        failure = None
        for api in speakable():
            if _api_for != api:
                _load(api)
            params = OpenSessionParams(version=_struct_version(1, api=api), deviceType=DEVICE_TYPE_DIRECTX,
                                       device=self.device, apiVersion=_api_version(api))
            status = _fn(F_OPEN_SESSION_EX, c_void_p, c_void_p)(ctypes.addressof(params),
                                                               ctypes.addressof(self.encoder))
            if status == 0:
                self.api = _preferred = api
                return
            failure = status
            self.encoder = c_void_p()
            if _status_name(status) != "INVALID_VERSION":
                break
        self._check(failure if failure is not None else 1, "opening the session")

    def _v(self, name: str, high_bit: bool = False) -> int:
        """The version tag of struct `name` for this session's API version."""
        return _struct_version(API_LAYOUTS[self.api][name], high_bit, self.api)

    def _config(self, preset, tuning_info, kbps, rate_control, max_kbps, buffer_kbits, cq_level, gop_frames):
        """The preset's own settings with ours on top: rate control, bitrate, keyframe interval, no B-frames."""
        import struct

        buf = (c_uint8 * PRESET_CONFIG_SIZE)()
        struct.pack_into("<I", buf, 0, self._v("preset", True))  # NV_ENC_PRESET_CONFIG_VER
        struct.pack_into("<I", buf, CONFIG_AT, self._v("config", True))  # NV_ENC_CONFIG_VER
        get = _fn(F_PRESET_CONFIG_EX, c_void_p, GUID, GUID, c_int, c_void_p)
        self._check(get(self.encoder, H264, preset, tuning_info, ctypes.addressof(buf)), "reading the preset")
        cfg = CONFIG_AT
        struct.pack_into("<i", buf, cfg + CFG_FRAME_INTERVAL_P, 1)  # 1 = I and P frames only
        if gop_frames:
            struct.pack_into("<I", buf, cfg + CFG_GOP, gop_frames)
            struct.pack_into("<I", buf, cfg + CFG_H264 + H264_IDR_PERIOD, gop_frames)
        rc = cfg + CFG_RC
        struct.pack_into("<I", buf, rc + RC_MODE, RATE_CONTROL.get(rate_control, 1))
        if rate_control == "cqp":
            struct.pack_into("<III", buf, rc + RC_CONST_QP, cq_level, cq_level, cq_level)
        elif kbps:
            struct.pack_into("<I", buf, rc + RC_AVG_BITRATE, kbps * 1000)
            struct.pack_into("<I", buf, rc + RC_MAX_BITRATE, (max_kbps or kbps) * 1000)
            if buffer_kbits:
                struct.pack_into("<I", buf, rc + RC_VBV_SIZE, buffer_kbits * 1000)
                struct.pack_into("<I", buf, rc + RC_VBV_DELAY, buffer_kbits * 1000)
        return buf

    def _check(self, status: int, what: str) -> None:
        if status == 0:
            return
        detail = ""
        if self.encoder:
            try:
                last = ctypes.WINFUNCTYPE(c_char_p, c_void_p)(_load().fn[F_LAST_ERROR])(self.encoder)
                detail = f" ({last.decode(errors='replace')})" if last else ""
            except Exception:  # the message is only a nicety
                pass
        raise NvencError(f"NVENC {what} failed: {_status_name(status)}{detail}")

    def encode(self, nv12: bytes | memoryview | None, keyframe: bool = False) -> bytes:
        """Encode one NV12 frame (width*height*3/2 bytes); None flushes at the end. Returns Annex B bytes."""
        data, _ = self.encode_frame(nv12, keyframe)
        return data

    def encode_frame(self, nv12: bytes | memoryview | None, keyframe: bool = False) -> tuple[bytes, bool]:
        """Like encode(), and also says whether the output is a keyframe (IDR)."""
        pic = PicParams()
        pic.head.version = self._v("pic", True)
        pic.head.inputWidth, pic.head.inputHeight = self.width, self.height
        if nv12 is None:
            pic.head.encodePicFlags = PIC_FLAG_EOS
        else:
            pitch = self._fill(nv12)
            pic.head.inputPitch = pitch
            pic.head.inputBuffer, pic.head.outputBitstream = self.input, self.output
            pic.head.bufferFmt, pic.head.pictureStruct = BUFFER_FORMAT_NV12, PIC_STRUCT_FRAME
            pic.head.frameIdx = pic.head.inputTimeStamp = self.frame
            if keyframe:
                pic.head.encodePicFlags = PIC_FLAG_FORCEIDR | PIC_FLAG_OUTPUT_SPSPPS
            self.frame += 1
        self._check(_fn(F_ENCODE, c_void_p, c_void_p)(self.encoder, ctypes.addressof(pic)), "encoding")
        if nv12 is None:
            return b"", False
        lock = LockBitstream(version=self._v("lock_bitstream", True), outputBitstream=self.output)
        self._check(_fn(F_LOCK_BITSTREAM, c_void_p, c_void_p)(self.encoder, ctypes.addressof(lock)),
                    "reading the output")
        try:
            return ctypes.string_at(lock.bitstreamBufferPtr, lock.bitstreamSizeInBytes), lock.pictureType == PIC_TYPE_IDR
        finally:
            _fn(F_UNLOCK_BITSTREAM, c_void_p, c_void_p)(self.encoder, self.output)

    def _fill(self, nv12) -> int:
        lock = LockInputBuffer(version=_struct_version(1, api=self.api), inputBuffer=self.input)
        self._check(_fn(F_LOCK_INPUT, c_void_p, c_void_p)(self.encoder, ctypes.addressof(lock)), "locking the input")
        try:
            src = nv12 if isinstance(nv12, bytes) else bytes(nv12)  # memmove reads bytes in place, no copy
            w, pitch, dst = self.width, lock.pitch, lock.bufferDataPtr
            rows = self.height * 3 // 2  # Y rows then interleaved UV rows, each `w` bytes wide
            if pitch == w:
                ctypes.memmove(dst, src, min(len(src), w * rows))
            else:
                base = ctypes.cast(ctypes.c_char_p(src), c_void_p).value
                for row in range(rows):
                    ctypes.memmove(dst + row * pitch, base + row * w, w)
            return pitch
        finally:
            _fn(F_UNLOCK_INPUT, c_void_p, c_void_p)(self.encoder, self.input)

    def close(self) -> None:
        if self.encoder:
            if self.input:
                _fn(F_DESTROY_INPUT, c_void_p, c_void_p)(self.encoder, self.input)
            if self.output:
                _fn(F_DESTROY_BITSTREAM, c_void_p, c_void_p)(self.encoder, self.output)
            _fn(F_DESTROY_ENCODER, c_void_p)(self.encoder)
            self.encoder = c_void_p()
        if self.device:
            _release(self.device)
            self.device = c_void_p()
