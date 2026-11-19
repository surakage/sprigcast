"""Leave out what SprigCast never loads, so the install is smaller.

Usage: prune.py <python home>   (the bundled Python, with SprigCast installed in Lib/site-packages)

PySide6 ships every Qt module (Quick, 3D, Designer, Multimedia, PDF and more). SprigCast uses a few,
so this keeps their Python modules, every Qt DLL those need (followed through each DLL's imports with
pefile), the Qt plugins a widgets app with web pages uses, and Qt WebEngine's helper and resources.
Everything else in PySide6 goes, along with Python's Tk and pip, and OpenCV's FFmpeg (cameras open
through DirectShow, and videos play through PyAV). So do libraries' test folders and type hints, and
the parts of Python only developers use.

Prints the size before and after. Needs pefile (run it with `uv run --with pefile`).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pefile

# The PySide6 modules SprigCast imports (grep "PySide6.Qt" in src).
QT_MODULES = {"QtCore", "QtGui", "QtWidgets", "QtNetwork", "QtSvg",
              "QtWebEngineCore", "QtWebEngineWidgets", "QtWebChannel", "QtWebSockets",
              # Browser pages drawn on the native engine's card (app/browser_quick.py): a Qt Quick page.
              "QtQml", "QtQuick", "QtWebEngineQuick"}
# The QML modules browser_quick.py's page imports (QtQuick, QtWebEngine, QtWebChannel) and QtQml under them.
# QtQuick's sub-folders (Controls, Dialogs, ...) go: the page uses none of them.
QML_KEEP = {"QtQml": True, "QtQuick": False, "QtWebEngine": True, "QtWebChannel": True}  # True: with sub-folders
# Qt plugin folders a widgets app with web pages loads: windows, pictures, icons, the style,
# secure connections and network status. (Windows typing, IME included, is in the platform plugin;
# platforminputcontexts only holds Qt's on-screen keyboard, which needs Qt Quick.)
QT_PLUGINS = {"platforms", "imageformats", "iconengines", "styles", "tls", "networkinformation", "generic"}
# Plugins inside those folders that pull in big modules SprigCast doesn't use (PDF pictures).
QT_PLUGIN_SKIP = {"qpdf.dll",
                  "qdirect2d.dll"}  # a second Windows platform plugin; Qt uses qwindows.dll unless told otherwise
# Qt Multimedia's own FFmpeg: SprigCast plays and records through PyAV's.
FFMPEG = ("avcodec", "avformat", "avutil", "avdevice", "avfilter", "swscale", "swresample")
# Loaded by name at run time, not through an import table, so never followed: keep them.
QT_ALWAYS = {"opengl32sw.dll", "d3dcompiler_47.dll"}
# Qt WebEngine's page text for each language; only US English is kept (others fall back to it).
WEBENGINE_LOCALE = "en-US.pak"


def size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink()


def imports(dll: Path) -> set[str]:
    try:
        pe = pefile.PE(str(dll), fast_load=True)
        pe.parse_data_directories(directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
    except pefile.PEFormatError:
        return set()
    names = set()
    for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
        for entry in getattr(pe, attr, []):
            names.add(entry.dll.decode(errors="replace").lower())
    pe.close()
    return names


def prune_pyside(pyside: Path) -> None:
    dlls = {f.name.lower(): f for f in pyside.glob("*.dll")}

    for f in pyside.rglob("*.pyi"):  # type hints, for editors only
        remove(f)

    # Plugins.
    plugins = pyside / "plugins"
    for folder in plugins.iterdir() if plugins.is_dir() else []:
        if folder.name not in QT_PLUGINS:
            remove(folder)
    for name in QT_PLUGIN_SKIP:
        for f in plugins.rglob(name):
            remove(f)

    # QML modules: only the page's.
    qml = pyside / "qml"
    for folder in qml.iterdir() if qml.is_dir() else []:
        if folder.name not in QML_KEEP:
            remove(folder)
        elif not QML_KEEP[folder.name]:
            for inner in folder.iterdir():
                if inner.is_dir():
                    remove(inner)

    # Every DLL reachable from what's kept.
    roots = [pyside / f"{m}.pyd" for m in QT_MODULES] + [pyside / "QtWebEngineProcess.exe"]
    roots += [f for f in qml.rglob("*.dll")] if qml.is_dir() else []
    roots += [f for f in plugins.rglob("*.dll")] if plugins.is_dir() else []
    roots += [f for f in pyside.glob("*.dll") if not f.name.lower().startswith(("qt6",) + FFMPEG)]
    needed: set[str] = set(QT_ALWAYS)
    todo = [r for r in roots if r.exists()]
    while todo:
        for name in imports(todo.pop()):
            if name in dlls and name not in needed:
                needed.add(name)
                todo.append(dlls[name])
    for name, f in dlls.items():
        if name.startswith("qt6") and name not in needed:
            remove(f)
    # Python modules: the ones SprigCast imports, plus each one whose Qt DLL stays (a kept module can
    # import it, as QtWebEngineWidgets does QtPrintSupport).
    for f in pyside.glob("Qt*.pyd"):
        module = f.stem.split(".")[0]
        if module not in QT_MODULES and f"qt6{module[2:].lower()}.dll" not in needed:
            remove(f)
    # Qt Multimedia's own FFmpeg (SprigCast uses PyAV's), unless something kept still needs it.
    for f in pyside.glob("*.dll"):
        if f.name.lower().startswith(FFMPEG) and f.name.lower() not in needed:
            remove(f)

    # Developer tools and files (Designer, QML tools, headers, type systems, translations of tools).
    for f in pyside.glob("*.exe"):
        if f.name != "QtWebEngineProcess.exe":
            remove(f)
    for name in ("metatypes", "typesystems", "include", "glue", "doc", "examples", "scripts"):
        remove(pyside / name)
    translations = pyside / "translations"
    for f in translations.glob("*.qm"):
        remove(f)
    for f in (translations / "qtwebengine_locales").glob("*.pak"):
        if f.name != WEBENGINE_LOCALE:
            remove(f)
    # Debug builds' copies of WebEngine resources (the release DLL reads the plain ones).
    for f in (pyside / "resources").glob("*.debug.*"):
        remove(f)
    # Chromium's developer tools (the inspector). SprigCast never opens them or turns on remote debugging.
    remove(pyside / "resources" / "qtwebengine_devtools_resources.pak")


def main() -> None:
    home = Path(sys.argv[1])
    site = home / "Lib" / "site-packages"
    before = size(home)

    if (site / "PySide6").is_dir():
        prune_pyside(site / "PySide6")
    for f in (site / "shiboken6").glob("*.pyi"):
        remove(f)
    for f in (site / "cv2").glob("opencv_videoio_ffmpeg*.dll"):
        remove(f)
    # Python's Tk, IDLE and pip: SprigCast never uses them (plugins don't install packages).
    for rel in ("tcl", "Lib/tkinter", "Lib/idlelib", "Lib/turtledemo", "Lib/ensurepip",
                "Lib/site-packages/pip", "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll"):
        remove(home / rel)
    for f in site.glob("pip-*.dist-info"):
        remove(f)
    # Other parts of Python only developers use: C import libraries for building extensions, Python's
    # own C-API test modules, venv, the 2to3 converter and help()'s topic texts.
    for rel in ("libs", "Lib/venv", "Lib/lib2to3", "Lib/pydoc_data"):
        remove(home / rel)
    for f in (home / "DLLs").glob("_test*.pyd"):
        remove(f)

    # Libraries' own test folders (numpy's are 8 MB) and type hints (.pyi, for editors only). SprigCast's
    # own package is left alone. This used to happen in build_windows.ps1, but its filter skipped every
    # path containing "sprigcast", and the build folder's path does, so nothing was ever removed.
    for package in site.iterdir():
        if not package.is_dir() or package.name.lower().startswith("sprigcast"):
            continue
        for folder in [d for d in package.rglob("*") if d.is_dir() and d.name in ("tests", "test")]:
            remove(folder)
        for f in package.rglob("*.pyi"):
            remove(f)

    after = size(home)
    print(f"Pruned the bundled Python: {before / 2**20:.0f} MB -> {after / 2**20:.0f} MB")


if __name__ == "__main__":
    main()
