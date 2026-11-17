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
