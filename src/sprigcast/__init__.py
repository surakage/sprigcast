"""SprigCast: a lightweight streaming app with horizontal and vertical canvases."""

__version__ = "0.4.0"


def display_version(version: str | None = None) -> str:
    """The version as people see it. A full release is one whole number ("1.0.0" -> "Version 1"); a beta is the
    last full release plus decimals ("0.3.0" -> "Beta 0.3", "0.3.1" -> "Beta 0.3.1"). Anything else as it is."""
    text = (__version__ if version is None else version).strip().removeprefix("v")
    parts = text.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return text
    major, minor, patch = (int(part) for part in parts)
    if major >= 1 and minor == 0 and patch == 0:
        return f"Version {major}"
    return f"Beta {major}.{minor}" + (f".{patch}" if patch else "")


def build_label() -> str:
    """The version as people and reports see it: "Beta 0.3" for a published beta, and for a test build (the
    installer workflow run without a v* tag, packaging/build_windows.ps1 writes build.json) "Beta 0.3 test build
    44932007527 (eafa358)", so logs and problem reports say which build they came from."""
    import json
    from pathlib import Path

    try:
        stamp = json.loads((Path(__file__).parent / "build.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return display_version()
    if not isinstance(stamp, dict) or stamp.get("kind") != "test":
        return display_version()
    run, commit = str(stamp.get("run") or "").strip(), str(stamp.get("commit") or "").strip()
    label = f"{display_version()} test build" + (f" {run}" if run else "")
    return label + (f" ({commit})" if commit else "")

import os as _os

# NumPy's maths library (OpenBLAS) starts a thread per CPU core as soon as it loads. SprigCast only
# copies and reshapes arrays, so those threads would just sit there next to your game.
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
