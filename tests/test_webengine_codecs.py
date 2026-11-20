"""packaging/webengine_codecs.py: the H.264/AAC page engine's zip fits PySide6's folder and is laid over it."""
from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "webengine_codecs", Path(__file__).resolve().parents[1] / "packaging" / "webengine_codecs.py")
wc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wc)


def _qt_install(root: Path) -> Path:
    for name in (*wc.DLLS, wc.HELPER):
        (root / "bin").mkdir(parents=True, exist_ok=True)
        (root / "bin" / name).write_bytes(b"new " + name.encode())
    (root / "bin" / "Qt6Core.dll").write_bytes(b"not ours")  # Qt itself never goes in
    for rel in ("resources/qtwebengine_resources.pak", "resources/qtwebengine_resources.debug.pak",
                "translations/qtwebengine_locales/en-US.pak", "qml/QtWebEngine/qtwebenginequickplugin.dll",
                "qml/QtWebEngine/qmldir", "qml/QtWebEngine/plugin.lib"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"new " + rel.encode())
    return root


def _pyside(root: Path) -> Path:
    for name in (*wc.DLLS, wc.HELPER, "msvcp140.dll"):
        root.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"old")
    return root


def test_pack_takes_the_engine_only_and_mirrors_pyside6(tmp_path):
    install = _qt_install(tmp_path / "qt")
    runtime = tmp_path / "crt"
    runtime.mkdir()
    (runtime / "msvcp140.dll").write_bytes(b"newer runtime")
    out = tmp_path / "e.zip"
    wc.pack(install, out, runtime)
    names = set(zipfile.ZipFile(out).namelist())
    assert {*wc.DLLS, wc.HELPER} <= names
    assert {"resources/qtwebengine_resources.pak", "translations/qtwebengine_locales/en-US.pak",
            "qml/QtWebEngine/qtwebenginequickplugin.dll", "qml/QtWebEngine/qmldir", "vcruntime/msvcp140.dll"} <= names
    assert "Qt6Core.dll" not in names and "resources/qtwebengine_resources.debug.pak" not in names
    assert "qml/QtWebEngine/plugin.lib" not in names


def test_pack_refuses_an_install_without_the_engine(tmp_path):
    install = _qt_install(tmp_path / "qt")
    (install / "bin" / wc.HELPER).unlink()
    with pytest.raises(FileNotFoundError):
        wc.pack(install, tmp_path / "e.zip")


def test_overlay_replaces_pyside6s_engine_and_only_a_newer_runtime(tmp_path):
    install = _qt_install(tmp_path / "qt")
    runtime = tmp_path / "crt"
    runtime.mkdir()
    (runtime / "msvcp140.dll").write_bytes(b"newer")
    (runtime / "vcruntime140.dll").write_bytes(b"older")
    out = tmp_path / "e.zip"
    wc.pack(install, out, runtime)
    pyside = _pyside(tmp_path / "PySide6")
    (pyside / "vcruntime140.dll").write_bytes(b"current")
    versions = {"msvcp140.dll": (14, 40), "msvcp140.dll.new": (14, 44),
                "vcruntime140.dll": (14, 44), "vcruntime140.dll.new": (14, 40)}
    changed = wc.overlay(out, pyside, wc.sha256(out), version=lambda p: versions.get(p.name))
    assert (pyside / "Qt6WebEngineCore.dll").read_bytes() == b"new Qt6WebEngineCore.dll"
    assert (pyside / "resources/qtwebengine_resources.pak").is_file()
    assert (pyside / "msvcp140.dll").read_bytes() == b"newer"  # newer: laid over
    assert (pyside / "vcruntime140.dll").read_bytes() == b"current"  # older: PySide6's stays
    assert "msvcp140.dll" in changed and "vcruntime140.dll" not in changed
    assert not list(pyside.glob("*.new"))


def test_overlay_refuses_a_wrong_checksum_or_a_folder_that_isnt_pyside6(tmp_path):
    out = tmp_path / "e.zip"
    wc.pack(_qt_install(tmp_path / "qt"), out)
    pyside = _pyside(tmp_path / "PySide6")
    with pytest.raises(ValueError):
        wc.overlay(out, pyside, "0" * 64)
    with pytest.raises(FileNotFoundError):
        wc.overlay(out, tmp_path / "elsewhere")
    (pyside / "Qt6WebEngineQuick.dll").unlink()  # a PySide6 without the module: the zip doesn't fit
    with pytest.raises(FileNotFoundError):
        wc.overlay(out, pyside)


def test_the_pin_is_absent_or_complete():
    pin = wc.pinned()
    assert pin is None or (pin["qt"] and pin["url"].startswith("https://github.com/") and len(pin["sha256"]) == 64)
