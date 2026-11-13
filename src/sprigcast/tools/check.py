"""M0 check: `uv run sprigcast-check`. Confirms Python, PyAV, FFmpeg and a video encoder are ready."""

from __future__ import annotations

import platform
from fractions import Fraction
import shutil
import sys


def _try_open_encoder(codec_name: str) -> str | None:
    """Open the encoder for one tiny frame. Returns None on success, or the error text."""
    import av
    import numpy as np

    try:
        ctx = av.CodecContext.create(codec_name, "w")
        ctx.width, ctx.height = 256, 256
        ctx.pix_fmt = "yuv420p" if codec_name == "libx264" else "nv12"
        ctx.time_base = Fraction(1, 30)
        ctx.framerate = 30
        frame = av.VideoFrame.from_ndarray(np.zeros((256, 256, 3), dtype=np.uint8), format="rgb24")
        frame = frame.reformat(format=ctx.pix_fmt)
        ctx.encode(frame)
        ctx.encode(None)  # flush
        return None
    except Exception as exc:  # driver missing, GPU busy, unsupported card, ...
        return str(exc) or exc.__class__.__name__


def _direct_try(label: str) -> None:
    from sprigcast.output.nvenc_d3d11 import D3D11Encoder

    try:
        enc = D3D11Encoder(1280, 720, 30, bitrate_kbps=4000)
        try:
            size = len(enc.encode(bytes(1280 * 720 * 3 // 2), keyframe=True))
        finally:
            enc.close()
        print(f"  {label:<44} OK ({size} bytes)")
    except Exception as exc:
        print(f"  {label:<44} FAILED: {exc}")


def nvenc_order() -> int:
    """sprigcast-check --nvenc-order: does SprigCast's own NVENC still open once FFmpeg's NVENC is running?

    The soak test (stream + recording) saw our encoder fail with INVALID_VERSION after the stream had
    opened FFmpeg's NVENC. This runs the same order in a fresh process to see if that order is the cause.
    """
    import ctypes
    import gc

    import av

    print(f"PyAV {av.__version__}, libavcodec {'.'.join(map(str, av.library_versions['libavcodec']))}")
    try:
        lib = ctypes.WinDLL("nvEncodeAPI64.dll")
        supported = ctypes.c_uint32()
        lib.NvEncodeAPIGetMaxSupportedVersion(ctypes.byref(supported))
        print(f"Driver supports NVENC API {supported.value >> 4}.{supported.value & 0xF}")
    except Exception as exc:
        print(f"No NVENC driver here ({exc})")
        return 1
    _direct_try("1. SprigCast NVENC, nothing else open")
    ctx = av.CodecContext.create("h264_nvenc", "w")
    ctx.width, ctx.height, ctx.pix_fmt = 1280, 720, "nv12"
    ctx.time_base, ctx.framerate = Fraction(1, 30), 30
    frame = av.VideoFrame(1280, 720, "nv12")
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    try:
        ctx.encode(frame)
        print(f"  {'2. FFmpeg NVENC opened and kept open':<44} OK")
    except Exception as exc:
        print(f"  {'2. FFmpeg NVENC opened and kept open':<44} FAILED: {exc}")
    _direct_try("3. SprigCast NVENC while FFmpeg's is open")
    try:
        ctx.encode(None)
    except Exception:
        pass
    del ctx, frame
    gc.collect()
    _direct_try("4. SprigCast NVENC after FFmpeg's closed")
    return 0


def main() -> int:
    if "--nvenc-order" in sys.argv:
        return nvenc_order()
    ok = True
    print(f"Python      {sys.version.split()[0]} ({platform.architecture()[0]}) on {platform.system()} {platform.release()}")
    if sys.version_info[:2] != (3, 12):
        print("            note: SprigCast is tested on Python 3.12")

    try:
        import av
    except ImportError:
        print("PyAV        MISSING  -> run `uv sync`")
        return 1
    print(f"PyAV        {av.__version__} (FFmpeg {'.'.join(map(str, av.library_versions['libavcodec']))} libavcodec)")

    from sprigcast.output.recorder import HARDWARE_CODECS, codec_label, encoder_works, pick_codec

    for name in (*HARDWARE_CODECS, "libx264"):  # every encoder SprigCast can use, best first
        if name not in av.codecs_available:
            print(f"{name:<11} not in this PyAV build")
            continue
        error = _try_open_encoder(name)
        if error is None and name in HARDWARE_CODECS and not encoder_works(name):
            error = "only its software fallback works here, so SprigCast skips it"  # e.g. Media Foundation, no GPU
        print(f"{name:<11} {'encoded a test frame: OK' if error is None else f'not usable here ({error})'}")
    picked = pick_codec()
    print(f"Encoder     SprigCast will use {codec_label(picked)}")
    if picked == "libx264":
        if "libx264" not in av.codecs_available or _try_open_encoder("libx264") is not None:
            ok = False
            print("            -> no video encoder works at all: run `uv sync`, then this again")
        else:
            print("            -> your CPU does the encoding. That works, but a graphics-card encoder is lighter:")
            print("               update the graphics driver (NVIDIA, AMD or Intel), then run this again")

    ffmpeg = shutil.which("ffmpeg")
    print(f"ffmpeg CLI  {ffmpeg or 'not found (only needed for sprigcast-teststream)'}")

    print()
    print("All set for M1." if ok else "Not quite ready: see the notes above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
