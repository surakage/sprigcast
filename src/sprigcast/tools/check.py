"""M0 check: `uv run sprigcast-check`. Confirms Python, PyAV, FFmpeg and NVENC are ready."""

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
        ctx.pix_fmt = "nv12" if "nvenc" in codec_name else "yuv420p"
        ctx.time_base = Fraction(1, 30)
        ctx.framerate = 30
        frame = av.VideoFrame.from_ndarray(np.zeros((256, 256, 3), dtype=np.uint8), format="rgb24")
        frame = frame.reformat(format=ctx.pix_fmt)
        ctx.encode(frame)
        ctx.encode(None)  # flush
        return None
    except Exception as exc:  # driver missing, GPU busy, unsupported card, ...
        return str(exc) or exc.__class__.__name__


def main() -> int:
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

    has_nvenc = "h264_nvenc" in av.codecs_available
    print(f"h264_nvenc  {'listed' if has_nvenc else 'NOT in this PyAV build'}")
    if has_nvenc:
        error = _try_open_encoder("h264_nvenc")
        if error is None:
            print("            encoded a test frame on your NVIDIA GPU: OK")
        else:
            ok = False
            print(f"            listed but could not encode: {error}")
            print("            -> update the NVIDIA driver, then run this again")
    else:
        ok = False
    fallback = _try_open_encoder("libx264") if "libx264" in av.codecs_available else "not in this build"
    print(f"libx264     {'OK (CPU fallback)' if fallback is None else fallback}")

    ffmpeg = shutil.which("ffmpeg")
    print(f"ffmpeg CLI  {ffmpeg or 'not found (only needed for sprigcast-teststream)'}")

    print()
    print("All set for M1." if ok else "Not quite ready: see the notes above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
