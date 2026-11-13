"""NVENC through Direct3D 11, talking to the driver directly (Windows + NVIDIA only).

This is how OBS reaches NVENC: a Direct3D 11 device instead of a CUDA context. FFmpeg (through PyAV)
can only use CUDA here, and a CUDA context costs about 100 MB of RAM on Windows. For now this is a
measuring tool (`sprigcast-memcheck --encoders`); if it proves much leaner it becomes an encoder path.

The structures follow nvEncodeAPI.h from NVIDIA's Video Codec SDK 12.1 (driver 531 or newer).
Frames go in as NV12 bytes and come out as H.264 Annex B packets. Synchronous mode, no B-frames.
"""

from __future__ import annotations

import ctypes
import sys
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

API_MAJOR, API_MINOR = 12, 1
API_VERSION = API_MAJOR | (API_MINOR << 24)


def _struct_version(ver: int, high_bit: bool = False) -> int:
    v = API_VERSION | (ver << 16) | (0x7 << 28)
    return v | (1 << 31) if high_bit else v


class GUID(Structure):
    _fields_ = [("Data1", c_uint32), ("Data2", c_uint16), ("Data3", c_uint16), ("Data4", c_uint8 * 8)]


def _guid(d1, d2, d3, d4) -> GUID:
    return GUID(d1, d2, d3, (c_uint8 * 8)(*d4))


H264 = _guid(0x6BC82762, 0x4E63, 0x4CA4, (0xAA, 0x85, 0x1E, 0x50, 0xF3, 0x21, 0xF6, 0xBF))
PRESET_P1 = _guid(0xFC0A8D3E, 0x45F8, 0x4CF8, (0x80, 0xC7, 0x29, 0x88, 0x71, 0x59, 0x0E, 0xBF))
PRESET_P4 = _guid(0x90A7B826, 0xDF06, 0x4862, (0xB9, 0xD2, 0xCD, 0x6D, 0x73, 0xA0, 0x86, 0x81))

DEVICE_TYPE_DIRECTX = 0
BUFFER_FORMAT_NV12 = 1
TUNING_LOW_LATENCY = 2
PIC_STRUCT_FRAME = 1
PIC_FLAG_EOS = 0x8
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
F_DESTROY_ENCODER, F_OPEN_SESSION_EX, F_LAST_ERROR = 27, 29, 37


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


_api = None


def _load():
    """The NVENC entry points, loaded once. Raises NvencError where NVENC can't be used."""
    global _api
    if _api is not None:
        return _api
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
    functions = FunctionList(version=_struct_version(2))
    status = lib.NvEncodeAPICreateInstance(byref(functions))
    if status != 0:
        raise NvencError(f"NvEncodeAPICreateInstance failed: {_status_name(status)}")
    _api = functions
    return _api


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
    """One H.264 encoder on its own Direct3D 11 device. encode() takes NV12 bytes, returns packets."""

    def __init__(self, width: int, height: int, fps: int, preset: GUID = PRESET_P4):
        self.width, self.height = width, height
        self.device = c_void_p()
        _load()  # says why NVENC can't be used (not Windows, no NVIDIA driver, old driver) before anything else
        self.device = d3d11_device()
        self.encoder = c_void_p()
        self.input = self.output = None
        self.frame = 0
        try:
            params = OpenSessionParams(version=_struct_version(1), deviceType=DEVICE_TYPE_DIRECTX,
                                       device=self.device, apiVersion=API_VERSION)
            self._check(_fn(F_OPEN_SESSION_EX, c_void_p, c_void_p)(ctypes.addressof(params), ctypes.addressof(self.encoder)),
                        "opening the session")
            init = InitializeParams(
                version=_struct_version(6, True), encodeGUID=H264, presetGUID=preset,
                encodeWidth=width, encodeHeight=height, darWidth=width, darHeight=height,
                frameRateNum=fps, frameRateDen=1, enableEncodeAsync=0, enablePTD=1,
                tuningInfo=TUNING_LOW_LATENCY,  # low latency presets never use B-frames
            )
            self._check(_fn(F_INITIALIZE, c_void_p, c_void_p)(self.encoder, ctypes.addressof(init)), "initialising")
            buf = CreateInputBuffer(version=_struct_version(1), width=width, height=height,
                                    bufferFmt=BUFFER_FORMAT_NV12)
            self._check(_fn(F_CREATE_INPUT, c_void_p, c_void_p)(self.encoder, ctypes.addressof(buf)), "making an input buffer")
            self.input = buf.inputBuffer
            out = CreateBitstreamBuffer(version=_struct_version(1))
            self._check(_fn(F_CREATE_BITSTREAM, c_void_p, c_void_p)(self.encoder, ctypes.addressof(out)),
                        "making an output buffer")
            self.output = out.bitstreamBuffer
        except Exception:
            self.close()
            raise

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

    def encode(self, nv12: bytes | memoryview | None) -> bytes:
        """Encode one NV12 frame (width*height*3/2 bytes); None flushes at the end. Returns Annex B bytes."""
        pic = PicParams()
        pic.head.version = _struct_version(6, True)
        pic.head.inputWidth, pic.head.inputHeight = self.width, self.height
        if nv12 is None:
            pic.head.encodePicFlags = PIC_FLAG_EOS
        else:
            pitch = self._fill(nv12)
            pic.head.inputPitch = pitch
            pic.head.inputBuffer, pic.head.outputBitstream = self.input, self.output
            pic.head.bufferFmt, pic.head.pictureStruct = BUFFER_FORMAT_NV12, PIC_STRUCT_FRAME
            pic.head.frameIdx = pic.head.inputTimeStamp = self.frame
            self.frame += 1
        self._check(_fn(F_ENCODE, c_void_p, c_void_p)(self.encoder, ctypes.addressof(pic)), "encoding")
        if nv12 is None:
            return b""
        lock = LockBitstream(version=_struct_version(1, True), outputBitstream=self.output)
        self._check(_fn(F_LOCK_BITSTREAM, c_void_p, c_void_p)(self.encoder, ctypes.addressof(lock)), "reading the output")
        try:
            return ctypes.string_at(lock.bitstreamBufferPtr, lock.bitstreamSizeInBytes)
        finally:
            _fn(F_UNLOCK_BITSTREAM, c_void_p, c_void_p)(self.encoder, self.output)

    def _fill(self, nv12) -> int:
        lock = LockInputBuffer(version=_struct_version(1), inputBuffer=self.input)
        self._check(_fn(F_LOCK_INPUT, c_void_p, c_void_p)(self.encoder, ctypes.addressof(lock)), "locking the input")
        try:
            src = memoryview(nv12).cast("B")
            w, pitch, dst = self.width, lock.pitch, lock.bufferDataPtr
            rows = self.height * 3 // 2  # Y rows then interleaved UV rows, each `w` bytes wide
            if pitch == w:
                ctypes.memmove(dst, (c_uint8 * len(src)).from_buffer_copy(src), len(src))
            else:
                for row in range(rows):
                    chunk = src[row * w:(row + 1) * w]
                    ctypes.memmove(dst + row * pitch, (c_uint8 * w).from_buffer_copy(chunk), w)
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
