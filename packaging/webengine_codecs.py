"""Qt WebEngine with H.264 and AAC, for web page sources.

PySide6's own Qt WebEngine (from pip) is built without "proprietary codecs": a page can't play an MP4 clip
(H.264 video, AAC sound) in a <video> or through MSE, while VP9, AV1 and Opus play. Overlays that play Twitch
clips (StreamBridge's Random Clip Player) or MP4 alerts showed nothing (2026-11-20). OBS's browser source ships
Chromium with them, so the same pages play there.

SprigCast builds the same Qt WebEngine version PySide6 ships (its source from download.qt.io, against the same
official Qt) with FEATURE_webengine_proprietary_codecs=ON, in .github/workflows/webengine-codecs.yml, and the
installer lays it over PySide6's copy (build_windows.ps1). The build is published as a GitHub release asset
(a pre-release, never "latest", so the app's updater and the website never see it); webengine_codecs.json pins
its address and SHA-256, and the Qt version it matches. Nothing else in Qt changes.

Licensing: Qt WebEngine is LGPL/GPL; this is an unmodified Qt source release built with one switch, and the
workflow is the build recipe. H.264/AAC decoding is the same kind of code SprigCast already ships in FFmpeg (PyAV).

Usage:
  webengine_codecs.py pack <install prefix> <out.zip> [<vc redist folder>]   (CI, after cmake --install)
  webengine_codecs.py overlay <zip> <PySide6 folder> [<sha256>]              (the installer build)
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

PIN = Path(__file__).with_name("webengine_codecs.json")

# Where each part of a Qt install goes in PySide6's folder on Windows (DLLs and the helper sit at its top).
DLLS = ("Qt6WebEngineCore.dll", "Qt6WebEngineWidgets.dll", "Qt6WebEngineQuick.dll",
        "Qt6WebEngineQuickDelegatesQml.dll")
HELPER = "QtWebEngineProcess.exe"
FOLDERS = ("resources", "translations/qtwebengine_locales", "qml/QtWebEngine")
# Microsoft's C++ runtime from the build machine: laid over PySide6's only when it is newer (a newer runtime
# runs everything an older one did; an older one can lack what the new DLLs call).
RUNTIME = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
           "msvcp140_codecvt_ids.dll", "vcruntime140.dll", "vcruntime140_1.dll", "concrt140.dll")
RUNTIME_DIR = "vcruntime"  # their folder in the zip


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def members(prefix: Path) -> dict[str, Path]:
    """Zip name (its place in PySide6's folder) -> file in the Qt install at `prefix`."""
    out: dict[str, Path] = {}
    for name in (*DLLS, HELPER):
        found = next((p for p in (prefix / "bin" / name, prefix / name) if p.is_file()), None)
        if found is None:
            raise FileNotFoundError(f"{name} isn't in {prefix}")
        out[name] = found
    for folder in FOLDERS:
        base = prefix / folder
        if not base.is_dir():
            raise FileNotFoundError(f"{folder} isn't in {prefix}")
        for f in sorted(base.rglob("*")):
            if f.is_file() and ".debug." not in f.name and f.suffix not in (".pdb", ".lib", ".prl", ".cmake"):
                out[f"{folder}/{f.relative_to(base).as_posix()}"] = f
    return out


def pack(prefix: Path, out: Path, runtime: Path | None = None) -> None:
    files = members(prefix)
    if runtime is not None:
        for name in RUNTIME:
            if (runtime / name).is_file():
                files[f"{RUNTIME_DIR}/{name}"] = runtime / name
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name, path in sorted(files.items()):
            z.write(path, name)
    print(f"{out.name}: {len(files)} files, {out.stat().st_size / 1e6:.0f} MB, sha256 {sha256(out)}")


def file_version(path: Path) -> tuple[int, int, int, int] | None:
    """A Windows file's version (None when it has none, or not on Windows)."""
    if sys.platform != "win32" or not path.is_file():
        return None
    import ctypes
    from ctypes import wintypes

    ver = ctypes.WinDLL("version")
    n = ver.GetFileVersionInfoSizeW(str(path), None)
    if not n:
        return None
    data = ctypes.create_string_buffer(n)
    if not ver.GetFileVersionInfoW(str(path), 0, n, data):
        return None
    info, length = ctypes.c_void_p(), wintypes.UINT()
    if not ver.VerQueryValueW(data, "\\", ctypes.byref(info), ctypes.byref(length)) or not length.value:
        return None
    words = ctypes.cast(info, ctypes.POINTER(wintypes.DWORD * 13)).contents
    ms, ls = words[2], words[3]  # VS_FIXEDFILEINFO.dwFileVersionMS / LS
    return ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF


def overlay(archive: Path, pyside: Path, expected: str | None = None, version=file_version) -> list[str]:
    """Lay the zip's files over PySide6's. Every engine file must replace one PySide6 has (a missing one means
    the zip doesn't fit this PySide6); the C++ runtime is copied only where it is newer. Returns what changed."""
    if expected and sha256(archive) != expected.lower():
        raise ValueError(f"{archive.name}: SHA-256 isn't the pinned one")
    if not (pyside / HELPER).is_file():
        raise FileNotFoundError(f"{pyside} isn't PySide6's folder (no {HELPER})")
    changed: list[str] = []
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        for name in names:
            if ".." in Path(name).parts or Path(name).is_absolute():
                raise ValueError(f"{archive.name}: bad name {name}")
        engine = [n for n in names if not n.startswith(RUNTIME_DIR + "/")]
        missing = [n for n in engine if n in (*DLLS, HELPER) and not (pyside / n).is_file()]
        if missing:
            raise FileNotFoundError(f"PySide6 has no {', '.join(missing)}: the zip doesn't fit this PySide6")
        for name in engine:
            target = pyside / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            changed.append(name)
        for name in names:
            if not name.startswith(RUNTIME_DIR + "/"):
                continue
            target = pyside / Path(name).name
            new = pyside / (Path(name).name + ".new")
            with z.open(name) as src, open(new, "wb") as dst:
                shutil.copyfileobj(src, dst)
            old_v, new_v = version(target), version(new)
            if not target.exists() or (old_v is not None and new_v is not None and new_v > old_v):
                new.replace(target)
                changed.append(Path(name).name)
            else:
                new.unlink()
    # SprigCast's page_video.MARKER: this engine plays H.264/AAC, so page MP4s aren't converted to WebM.
    (pyside / "webengine-codecs.txt").write_text(f"{archive.name}\n", "utf-8")
    return changed


def pinned() -> dict | None:
    """webengine_codecs.json: {"qt": "6.12.0", "url": ..., "sha256": ...}, or None before a build is pinned."""
    if not PIN.is_file():
        return None
    pin = json.loads(PIN.read_text("utf-8"))
    return pin if pin.get("url") and pin.get("sha256") and pin.get("qt") else None


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "pack":
        pack(Path(argv[1]), Path(argv[2]), Path(argv[3]) if len(argv) > 3 else None)
        return 0
    if len(argv) >= 3 and argv[0] == "overlay":
        changed = overlay(Path(argv[1]), Path(argv[2]), argv[3] if len(argv) > 3 else None)
        print(f"Qt WebEngine with H.264/AAC laid over PySide6: {len(changed)} files")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
