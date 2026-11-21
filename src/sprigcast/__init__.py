"""SprigCast: a lightweight streaming app with horizontal and vertical canvases."""

__version__ = "0.2.0"


def build_label() -> str:
    """The version as people and reports see it: "0.2.0" for a release, and for a test build (the installer
    workflow run without a v* tag, packaging/build_windows.ps1 writes build.json) "0.2.0 test build
    44932007527 (eafa358)", so logs and problem reports say which build they came from."""
    import json
    from pathlib import Path

    try:
        stamp = json.loads((Path(__file__).parent / "build.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return __version__
    if not isinstance(stamp, dict) or stamp.get("kind") != "test":
        return __version__
    run, commit = str(stamp.get("run") or "").strip(), str(stamp.get("commit") or "").strip()
    label = f"{__version__} test build" + (f" {run}" if run else "")
    return label + (f" ({commit})" if commit else "")

import os as _os

# NumPy's maths library (OpenBLAS) starts a thread per CPU core as soon as it loads. SprigCast only
# copies and reshapes arrays, so those threads would just sit there next to your game.
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
