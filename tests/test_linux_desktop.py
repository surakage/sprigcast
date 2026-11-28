"""On a Linux desktop SprigCast has its name, icon and launcher entry (GNOME called it "python3", 2026-11-28)."""
import os

from sprigcast.app import linux_desktop
from sprigcast.core import systems


def _env(tmp_path, launcher=True):
    folder = tmp_path / "Sprig Cast $1"
    folder.mkdir(exist_ok=True)
    file = folder / "SprigCast"
    if launcher:
        file.write_text("#!/bin/sh\n")
    return {"HOME": str(tmp_path), linux_desktop.LAUNCHER_ENV: str(file)}


def test_the_entry_is_written_once_and_follows_the_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(systems, "is_linux", lambda: True)
    env = _env(tmp_path)
    path = linux_desktop.keep_entry("/x/logo.svg", env)
    assert path == tmp_path / ".local/share/applications/sprigcast.desktop"
    text = path.read_text()
    assert "Name=SprigCast\n" in text and "Icon=/x/logo.svg\n" in text and "Terminal=false\n" in text
    assert f'Exec="{tmp_path}/Sprig Cast \\$1/SprigCast"\n' in text
    assert f"StartupWMClass={linux_desktop.APP_ID}\n" in text
    assert linux_desktop.keep_entry("/x/logo.svg", env) is None  # already right: not written again
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    (moved / "SprigCast").write_text("#!/bin/sh\n")
    env[linux_desktop.LAUNCHER_ENV] = str(moved / "SprigCast")
    assert linux_desktop.keep_entry("/x/logo.svg", env) == path and "elsewhere" in path.read_text()
    assert not list(path.parent.glob("*.new"))


def test_nothing_is_written_from_source_or_on_other_systems(tmp_path, monkeypatch):
    monkeypatch.setattr(systems, "is_linux", lambda: True)
    assert linux_desktop.keep_entry("/x/logo.svg", {"HOME": str(tmp_path)}) is None  # from source: no launcher
    assert linux_desktop.keep_entry("/x/logo.svg", _env(tmp_path, launcher=False)) is None  # launcher gone
    monkeypatch.setattr(systems, "is_linux", lambda: False)
    assert linux_desktop.keep_entry("/x/logo.svg", _env(tmp_path)) is None
    assert not linux_desktop.name_app()
    assert not (tmp_path / ".local").exists()


def test_a_folder_that_cannot_be_written_is_only_noted(tmp_path, monkeypatch):
    monkeypatch.setattr(systems, "is_linux", lambda: True)
    env = _env(tmp_path)
    (tmp_path / ".local").write_text("a file where the folder should be")
    assert linux_desktop.keep_entry("/x/logo.svg", env) is None


def test_the_app_names_its_entry_and_the_launcher_says_where_it_is(monkeypatch):
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(systems, "is_linux", lambda: True)
    before = QGuiApplication.desktopFileName()
    try:
        assert linux_desktop.name_app() and QGuiApplication.desktopFileName() == linux_desktop.APP_ID
    finally:
        QGuiApplication.setDesktopFileName(before)
    script = open(os.path.join(os.path.dirname(__file__), "..", "packaging", "build_unix.sh")).read()
    assert f'{linux_desktop.LAUNCHER_ENV}="$here/SprigCast" exec' in script
