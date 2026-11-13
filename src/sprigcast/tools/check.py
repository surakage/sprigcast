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

    from sprigcast.output.recorder import HARDWARE_CODECS, codec_label, pick_codec

    for name in (*HARDWARE_CODECS, "libx264"):  # every encoder SprigCast can use, best first
        if name not in av.codecs_available:
            print(f"{name:<11} not in this PyAV build")
            continue
        error = _try_open_encoder(name)
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
