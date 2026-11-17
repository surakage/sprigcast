"""An update clears SprigCast's old code first, so files taken out of SprigCast don't linger (packaging/sprigcast.iss)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "sprigcast"
BASE = "{app}\\python\\Lib\\site-packages\\sprigcast\\"


def _install_delete() -> list[str]:
    text = (ROOT / "packaging" / "sprigcast.iss").read_text(encoding="utf-8")
    section = text.split("[InstallDelete]", 1)[1].split("\n[", 1)[0]
    return [line for line in section.splitlines() if line.startswith("Type:")]


def test_every_code_folder_is_cleared_but_the_camera():
    lines = _install_delete()
    names = {line.split(BASE, 1)[1].rstrip('"') for line in lines}
    folders = {p.name for p in PACKAGE.iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert folders <= names, f"add to [InstallDelete]: {sorted(folders - names)}"
    assert "*.py" in names
    assert "camera" not in names  # its DLL can be in use (#272)
    assert all(line.startswith(("Type: files;", "Type: filesandordirs;")) and BASE in line for line in lines)


def test_an_update_deletes_what_the_last_version_had_and_this_one_doesnt(tmp_path):
    """As OBS's updater does with removed_files: libraries dropped from SprigCast (onnxruntime, pedalboard) went
    nowhere on update before, since [InstallDelete] only clears SprigCast's own code."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("manifest", ROOT / "packaging" / "manifest.py")
    manifest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manifest)
    home = tmp_path / "python"
    (home / "Lib" / "site-packages" / "onnxruntime").mkdir(parents=True)
    (home / "Lib" / "site-packages" / "onnxruntime" / "capi.pyd").write_bytes(b"x")
    (home / "Lib" / "__pycache__").mkdir()
    (home / "Lib" / "__pycache__" / "os.cpython-312.pyc").write_bytes(b"x")  # made at run time: not listed
    (home / "python.exe").write_bytes(b"x")
    manifest.main(home)
    listed = (home / manifest.NAME).read_text(encoding="utf-8").split()
    assert listed == ["Lib\\site-packages\\onnxruntime\\capi.pyd", "python.exe"]
    iss = (ROOT / "packaging" / "sprigcast.iss").read_text(encoding="utf-8")
    assert manifest.NAME in iss and "RemoveDroppedFiles();" in iss and "ssInstall" in iss
    build = (ROOT / "packaging" / "build_windows.ps1").read_text(encoding="utf-8")
    assert build.index("manifest.py") > build.index("prune.py")  # listed after pruning: exactly what ships
