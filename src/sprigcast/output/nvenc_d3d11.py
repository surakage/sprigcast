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
Frames go in as NV12 bytes and come out as H.264 or HEVC Annex B packets. Synchronous mode, no B-frames.
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
    (13, 0): {"init": 7, "config": 9, "preset": 5, "pic": 7, "lock_bitstream": 2, "create_input": 2,
              "reconfigure": 2, "init_size": 1800, "register": 5, "map": 4},
    (12, 2): {"init": 7, "config": 9, "preset": 5, "pic": 7, "lock_bitstream": 2, "create_input": 2,
              "reconfigure": 2, "init_size": 1800, "register": 5, "map": 4},
    (12, 1): {"init": 6, "config": 8, "preset": 4, "pic": 6, "lock_bitstream": 1, "create_input": 1,
              "reconfigure": 1, "init_size": 1808, "register": 4, "map": 4},
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
HEVC = _guid(0x790CDC88, 0x4522, 0x4D7B, (0x94, 0x25, 0xBD, 0xA9, 0x97, 0x5F, 0x76, 0x03))
CODECS = {"h264": H264, "hevc": HEVC}
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
RC_FLAGS, RC_FLAG_AQ, RC_MULTIPASS = 36, 0x8, 100  # the enableAQ bit; NV_ENC_MULTI_PASS (0 off, 1 quarter, 2 full)
MULTIPASS = {"disabled": 0, "qres": 1, "fullres": 2}
RECONFIGURE_INIT_AT = 8  # NV_ENC_RECONFIGURE_PARAMS: version, then the initialise params, then the flag bits
# idrPeriod inside the codec part of NV_ENC_CONFIG (at CFG_H264): NV_ENC_CONFIG_H264 has one word of
# flags and level before it; NV_ENC_CONFIG_HEVC has level, tier, minCUSize, maxCUSize and a word of flags.
H264_IDR_PERIOD, HEVC_IDR_PERIOD = 8, 20
IDR_PERIOD = {"h264": H264_IDR_PERIOD, "hevc": HEVC_IDR_PERIOD}
# enableFillerDataInsertion: (offset of the flags word in the codec part, bit). H.264's flags come first
# (bit 17, after useConstrainedIntraPred); HEVC's sit after level, tier and the two CU sizes (bit 14,
# after chromaFormatIDC and the 3 bit-depth bits). Without filler, NVENC's CBR is only a ceiling: a
# still scene went out at 200-900 kbps of 4000 on Twitch (2026-11-15), where OBS pads up to the bitrate.
FILLER_BIT = {"h264": (0, 17), "hevc": (16, 14)}
STATUS_NAMES = [
    "SUCCESS", "NO_ENCODE_DEVICE", "UNSUPPORTED_DEVICE", "INVALID_ENCODERDEVICE", "INVALID_DEVICE",
    "DEVICE_NOT_EXIST", "INVALID_PTR", "INVALID_EVENT", "INVALID_PARAM", "INVALID_CALL", "OUT_OF_MEMORY",
    "ENCODER_NOT_INITIALIZED", "UNSUPPORTED_PARAM", "LOCK_BUSY", "NOT_ENOUGH_BUFFER", "INVALID_VERSION",
    "MAP_FAILED", "NEED_MORE_INPUT", "ENCODER_BUSY", "EVENT_NOT_REGISTERD", "GENERIC",
    "INCOMPATIBLE_CLIENT_KEY", "UNIMPLEMENTED", "RESOURCE_REGISTER_FAILED", "RESOURCE_NOT_REGISTERED",
    "RESOURCE_NOT_MAPPED", "NEED_MORE_OUTPUT",
]


def readable_error(raw: bytes | None) -> str:
    """NVENC's last-error text, or "" when there's none worth showing. After a failed call on a device that went
    away the driver hands back a buffer it never wrote (crash run 2, 2026-11-17: "INVALID_CALL (\ufffd\ufffd)")."""
    if not raw:
        return ""
    text = raw.split(b"\0", 1)[0].decode("ascii", errors="replace").strip()
    if not text or any(not (ch.isprintable() and ch.isascii()) or ch == "\ufffd" for ch in text):
        return ""
    return text[:300]


class FunctionList(Structure):
    _fields_ = [("version", c_uint32), ("reserved", c_uint32), ("fn", c_void_p * 319)]


# Positions in FunctionList.fn (the order of NV_ENCODE_API_FUNCTION_LIST).
F_INITIALIZE, F_CREATE_INPUT, F_DESTROY_INPUT, F_CREATE_BITSTREAM, F_DESTROY_BITSTREAM = 11, 12, 13, 14, 15
F_ENCODE, F_LOCK_BITSTREAM, F_UNLOCK_BITSTREAM, F_LOCK_INPUT, F_UNLOCK_INPUT = 16, 17, 18, 19, 20
F_DESTROY_ENCODER, F_OPEN_SESSION_EX, F_RECONFIGURE, F_LAST_ERROR, F_PRESET_CONFIG_EX = 27, 29, 32, 37, 39
F_MAP_INPUT, F_UNMAP_INPUT, F_REGISTER, F_UNREGISTER = 25, 26, 30, 31
RESOURCE_TYPE_DIRECTX, BUFFER_USAGE_INPUT = 0, 0


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


class RegisterResource(Structure):
    """NV_ENC_REGISTER_RESOURCE (1,536 bytes in 12.1 to 13.0; 13.0 adds chromaOffsetIn inside the reserved part)."""
    _fields_ = [("version", c_uint32), ("resourceType", c_int), ("width", c_uint32), ("height", c_uint32),
                ("pitch", c_uint32), ("subResourceIndex", c_uint32), ("resourceToRegister", c_void_p),
                ("registeredResource", c_void_p), ("bufferFormat", c_int), ("bufferUsage", c_int),
                ("pInputFencePoint", c_void_p), ("chromaOffset", c_uint32 * 2), ("reserved1", c_uint32 * 245),
                ("reserved2", c_void_p * 61)]


class MapInputResource(Structure):
    _fields_ = [("version", c_uint32), ("subResourceIndex", c_uint32), ("inputResource", c_void_p),
                ("registeredResource", c_void_p), ("mappedResource", c_void_p), ("mappedBufferFmt", c_int),
                ("reserved1", c_uint32 * 251), ("reserved2", c_void_p * 63)]


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


_shared_device: c_void_p | None = None
_shared_lock = threading.Lock()
MULTITHREAD_IID = (0x9B7E4E00, 0x342C, 0x4106, (0xA1, 0x9F, 0x4F, 0x27, 0x04, 0xF6, 0x89, 0xF0))  # ID3D10Multithread


def shared_device() -> c_void_p:
    """The process's one Direct3D 11 device for frames handed over on the card (render/gpu_share.py): OpenGL
    writes into its textures and the NVENC sessions that read them are opened on it. Safe to use from several
    threads (multithread protection on), made once and kept for the run. Each caller gets its own reference."""
    global _shared_device
    with _shared_lock:
        if _shared_device is None:
            device = d3d11_device()
            try:
                guard = c_void_p()
                iid = _guid(*MULTITHREAD_IID)
                vtable = ctypes.cast(device, POINTER(POINTER(c_void_p))).contents
                hr = ctypes.WINFUNCTYPE(c_int, c_void_p, c_void_p, c_void_p)(vtable[0])(device, byref(iid), byref(guard))
                if hr < 0 or not guard:
                    raise NvencError(f"no multithread guard on the Direct3D device (0x{hr & 0xFFFFFFFF:08x})")
                gtable = ctypes.cast(guard, POINTER(POINTER(c_void_p))).contents
                ctypes.WINFUNCTYPE(c_int, c_void_p, c_int)(gtable[5])(guard, 1)  # SetMultithreadProtected(TRUE)
                _release(guard)
            except Exception:
                _release(device)
                raise
            _shared_device = device
        _add_ref(_shared_device)
        return c_void_p(_shared_device.value)


def _add_ref(com: c_void_p) -> None:
    """IUnknown::AddRef (vtable slot 1)."""
    vtable = ctypes.cast(com, POINTER(POINTER(c_void_p))).contents
    ctypes.WINFUNCTYPE(c_uint32, c_void_p)(vtable[1])(com)


def _release(com: c_void_p) -> None:
    """IUnknown::Release (vtable slot 2)."""
    if com:
        vtable = ctypes.cast(com, POINTER(POINTER(c_void_p))).contents
        ctypes.WINFUNCTYPE(c_uint32, c_void_p)(vtable[2])(com)


def set_filler(buf, codec: str) -> None:
    """Turn on filler data in a preset config buffer, so CBR really sends the bitrate on still scenes."""
    import struct

    where, bit = FILLER_BIT[codec]
    at = CONFIG_AT + CFG_H264 + where
    struct.pack_into("<I", buf, at, struct.unpack_from("<I", buf, at)[0] | 1 << bit)


class D3D11Encoder:
    """One H.264 or HEVC encoder on its own Direct3D 11 device. encode() takes NV12 bytes, returns packets.

    Never uses B-frames, so every packet comes out as soon as its frame went in, in order.

    shared=True opens it on the process's shared device (shared_device), so it can also encode NV12 textures
    handed over on the card (encode_texture, render/gpu_share.py) besides bytes. device (a c_void_p holding a
    reference this encoder takes over) opens it on that Direct3D 11 device instead: the native engine's, whose
    frames it then takes on the card (render/native_bridge.py CardFrame).
    """

    def __init__(self, width: int, height: int, fps: int, preset: GUID | str = PRESET_P4, *,
                 bitrate_kbps: int = 0, rate_control: str = "vbr", max_kbps: int = 0, buffer_kbits: int = 0,
                 cq_level: int = 23, gop_frames: int = 0, tuning: str = "ll", adaptive_quant: bool = False,
                 multipass: str = "disabled", codec: str = "h264", shared: bool = False,
                 device: c_void_p | None = None):
        self.device = device if device is not None else c_void_p()  # ours to let go from here on
        self.encoder = c_void_p()
        self.input = self.output = None
        self._registered = {}
        self._pool = None
        try:
            self._start(width, height, fps, preset, bitrate_kbps, rate_control, max_kbps, buffer_kbits, cq_level,
                        gop_frames, tuning, adaptive_quant, multipass, codec, shared, device is not None)
        except Exception:
            self.close()
            raise

    def _start(self, width, height, fps, preset, bitrate_kbps, rate_control, max_kbps, buffer_kbits, cq_level,
               gop_frames, tuning, adaptive_quant, multipass, codec, shared, given) -> None:
        if codec not in CODECS:
            raise NvencError(f"no {codec} in SprigCast's own NVENC encoder")
        self.codec = codec
        self.width, self.height = width, height
        self.encoder = c_void_p()
        self.input = self.output = None
        self._settings = self._init = None
        self.frame = 0
        self.api = (API_MAJOR, API_MINOR)
        self.shared = shared or given
        # (pool id, texture index) -> (registered resource, the texture with a reference held), for encode_texture
        self._registered: dict = {}
        self._pool = None  # the texture pool those belong to (kept alive while registered)
        _load()  # says why NVENC can't be used (not Windows, no NVIDIA driver, old driver) before anything else
        preset = PRESETS.get(preset, PRESET_P4) if isinstance(preset, str) else preset
        tuning_info = TUNINGS.get(tuning, TUNINGS["ll"])
        if not given:
            self.device = shared_device() if shared else d3d11_device()
        try:
            self._open_session()
            config = None
            if bitrate_kbps or gop_frames or rate_control == "cqp" or adaptive_quant or multipass != "disabled":
                config = self._config(preset, tuning_info, bitrate_kbps, rate_control, max_kbps, buffer_kbits,
                                      cq_level, gop_frames, adaptive_quant, multipass)
            self._settings = config  # kept: the encoder reads it again on reconfigure (set_bitrate)
            self._init = init = InitializeParams(
                version=self._v("init", True), encodeGUID=CODECS[codec], presetGUID=preset,
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

    def _config(self, preset, tuning_info, kbps, rate_control, max_kbps, buffer_kbits, cq_level, gop_frames,
                adaptive_quant=False, multipass="disabled"):
        """The preset's own settings with ours on top: rate control, bitrate, keyframe interval, no B-frames."""
        import struct

        buf = (c_uint8 * PRESET_CONFIG_SIZE)()
        struct.pack_into("<I", buf, 0, self._v("preset", True))  # NV_ENC_PRESET_CONFIG_VER
        struct.pack_into("<I", buf, CONFIG_AT, self._v("config", True))  # NV_ENC_CONFIG_VER
        get = _fn(F_PRESET_CONFIG_EX, c_void_p, GUID, GUID, c_int, c_void_p)
        self._check(get(self.encoder, CODECS[self.codec], preset, tuning_info, ctypes.addressof(buf)),
                    "reading the preset")
        cfg = CONFIG_AT
        struct.pack_into("<i", buf, cfg + CFG_FRAME_INTERVAL_P, 1)  # 1 = I and P frames only
        if gop_frames:
            struct.pack_into("<I", buf, cfg + CFG_GOP, gop_frames)
            struct.pack_into("<I", buf, cfg + CFG_H264 + IDR_PERIOD[self.codec], gop_frames)
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
        if rate_control == "cbr" and kbps:
            set_filler(buf, self.codec)
        if adaptive_quant:
            flags = struct.unpack_from("<I", buf, rc + RC_FLAGS)[0]
            struct.pack_into("<I", buf, rc + RC_FLAGS, flags | RC_FLAG_AQ)
        struct.pack_into("<I", buf, rc + RC_MULTIPASS, MULTIPASS.get(multipass, 0))
        return buf

    def set_bitrate(self, kbps: int, max_kbps: int = 0, buffer_kbits: int = 0) -> None:
        """Change the bitrate mid-stream (NvEncReconfigureEncoder), without a keyframe or a new session."""
        import struct

        if self._settings is None:
            raise NvencError("this encoder was opened without its own settings, so its bitrate can't change")
        rc = CONFIG_AT + CFG_RC
        struct.pack_into("<I", self._settings, rc + RC_AVG_BITRATE, kbps * 1000)
        struct.pack_into("<I", self._settings, rc + RC_MAX_BITRATE, (max_kbps or kbps) * 1000)
        if buffer_kbits:
            struct.pack_into("<I", self._settings, rc + RC_VBV_SIZE, buffer_kbits * 1000)
            struct.pack_into("<I", self._settings, rc + RC_VBV_DELAY, buffer_kbits * 1000)
        layout = API_LAYOUTS[self.api]
        params = (c_uint8 * (RECONFIGURE_INIT_AT + layout["init_size"] + 4))()
        struct.pack_into("<I", params, 0, self._v("reconfigure", True))
        ctypes.memmove(ctypes.addressof(params) + RECONFIGURE_INIT_AT, ctypes.addressof(self._init),
                       layout["init_size"])  # flag bits after it stay 0: no reset, no forced keyframe
        self._check(_fn(F_RECONFIGURE, c_void_p, c_void_p)(self.encoder, ctypes.addressof(params)),
                    "changing the bitrate")

    def _check(self, status: int, what: str) -> None:
        if status == 0:
            return
        detail = ""
        if self.encoder:
            try:
                last = ctypes.WINFUNCTYPE(c_char_p, c_void_p)(_load().fn[F_LAST_ERROR])(self.encoder)
                detail = f" ({text})" if (text := readable_error(last)) else ""
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

    def encode_texture(self, frame, keyframe: bool = False) -> tuple[bytes, bool]:
        """Encode a GpuFrame (an NV12 texture on the shared device, render/gpu_share.py), like encode_frame.

        Each texture is registered with this session once; a frame from another pool (the output's size
        changed) lets go of the old pool's registrations first."""
        if not self.shared:
            raise NvencError("this encoder isn't on the shared device")
        pool = frame.pool
        if pool is None:
            raise NvencError("the frame was already handed back")
        if self._pool is not None and self._pool is not pool:
            self._unregister_all()
        key = frame.key
        held = self._registered.get(key)
        registered = held[0] if held is not None else None
        if registered is None:
            reg = RegisterResource(version=self._v("register"), resourceType=RESOURCE_TYPE_DIRECTX,
                                   width=frame.width, height=frame.height, pitch=0,
                                   resourceToRegister=frame.texture, bufferFormat=BUFFER_FORMAT_NV12,
                                   bufferUsage=BUFFER_USAGE_INPUT)
            self._check(_fn(F_REGISTER, c_void_p, c_void_p)(self.encoder, ctypes.addressof(reg)),
                        "registering a texture")
            # NVENC keeps the texture's pointer but no reference: hold one until it's unregistered. Crash on the
            # PC (native run 2026-11-17, 20:23:46): the engine made its outputs again, freed the old pool, and the
            # next frame's _unregister_all unregistered textures that were gone (access violation in the driver).
            texture = c_void_p(frame.texture)
            _add_ref(texture)
            registered = reg.registeredResource
            self._registered[key] = (registered, texture)
            self._pool = pool
        mapped = MapInputResource(version=self._v("map"), registeredResource=registered)
        self._check(_fn(F_MAP_INPUT, c_void_p, c_void_p)(self.encoder, ctypes.addressof(mapped)), "mapping a texture")
        try:
            pic = PicParams()
            pic.head.version = self._v("pic", True)
            pic.head.inputWidth, pic.head.inputHeight, pic.head.inputPitch = self.width, self.height, self.width
            pic.head.inputBuffer, pic.head.outputBitstream = mapped.mappedResource, self.output
            pic.head.bufferFmt, pic.head.pictureStruct = mapped.mappedBufferFmt, PIC_STRUCT_FRAME
            pic.head.frameIdx = pic.head.inputTimeStamp = self.frame
            if keyframe:
                pic.head.encodePicFlags = PIC_FLAG_FORCEIDR | PIC_FLAG_OUTPUT_SPSPPS
            self.frame += 1
            self._check(_fn(F_ENCODE, c_void_p, c_void_p)(self.encoder, ctypes.addressof(pic)), "encoding")
            lock = LockBitstream(version=self._v("lock_bitstream", True), outputBitstream=self.output)
            self._check(_fn(F_LOCK_BITSTREAM, c_void_p, c_void_p)(self.encoder, ctypes.addressof(lock)),
                        "reading the output")
            try:
                return (ctypes.string_at(lock.bitstreamBufferPtr, lock.bitstreamSizeInBytes),
                        lock.pictureType == PIC_TYPE_IDR)
            finally:
                _fn(F_UNLOCK_BITSTREAM, c_void_p, c_void_p)(self.encoder, self.output)
        finally:
            _fn(F_UNMAP_INPUT, c_void_p, c_void_p)(self.encoder, mapped.mappedResource)

    def _unregister_all(self) -> None:
        if self.encoder:
            unregister = _fn(F_UNREGISTER, c_void_p, c_void_p)
            for registered, _texture in self._registered.values():
                try:
                    unregister(self.encoder, registered)
                except Exception:  # noqa: BLE001 (closing anyway)
                    pass
        for _registered, texture in self._registered.values():
            try:
                _release(texture)  # after unregistering: only now may the engine's texture go
            except Exception:  # noqa: BLE001 (closing anyway)
                pass
        self._registered.clear()
        self._pool = None

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
                # One strided copy. A memmove per row (1,620 at 1080p) gave up Python's lock and had to win it
                # back 1,620 times a frame: with other threads busy, each could wait its turn (5 ms).
                import numpy as np

                into = np.ctypeslib.as_array((ctypes.c_uint8 * (pitch * rows)).from_address(dst))
                into.reshape(rows, pitch)[:, :w] = np.frombuffer(src, np.uint8, count=w * rows).reshape(rows, w)
            return pitch
        finally:
            _fn(F_UNLOCK_INPUT, c_void_p, c_void_p)(self.encoder, self.input)

    def close(self) -> None:
        self._unregister_all()
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
