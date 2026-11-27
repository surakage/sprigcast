"""SprigCast: a lightweight streaming app with horizontal and vertical canvases."""

__version__ = "0.5.0"


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


def build_stamp() -> dict:
    """build.json, which packaging/build_windows.ps1 writes into a test build ({} for a release or source)."""
    import json
    from pathlib import Path

    try:
        stamp = json.loads((Path(__file__).parent / "build.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return stamp if isinstance(stamp, dict) and stamp.get("kind") == "test" else {}


def test_build(stamp: dict | None = None) -> int:
    """The test build number (0.5 test build 3 -> 3), or 0 for a release, source or an older run-id test build."""
    value = (build_stamp() if stamp is None else stamp).get("test")
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def short_version(version: str | None = None, test: int = 0) -> str:
    """What the update pop-up shows: "Beta 0.5 test build 3", or display_version() when it isn't a test build."""
    return f"{display_version(version)} test build {test}" if test else display_version(version)


def build_label() -> str:
    """The version as people and reports see it: "Beta 0.3" for a published beta, "Beta 0.5 test build 3
    (eafa358)" for a numbered test build (tag test-0.5-3), and for an older test build (the installer workflow run
    without a numbered tag) "Beta 0.3 test build 44932007527 (eafa358)", so logs and problem reports say which
    build they came from."""
    stamp = build_stamp()
    if not stamp:
        return display_version()
    commit = str(stamp.get("commit") or "").strip()
    number = test_build(stamp)
    run = str(number or stamp.get("run") or "").strip()
    label = f"{display_version()} test build" + (f" {run}" if run else "")
    return label + (f" ({commit})" if commit else "")

import os as _os

# NumPy's maths library (OpenBLAS) starts a thread per CPU core as soon as it loads. SprigCast only
# copies and reshapes arrays, so those threads would just sit there next to your game.
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
