"""Clear all my data: what goes, what never goes, and the clean-up that waits for SprigCast to close."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from sprigcast.app import clear_data  # noqa: E402


def test_only_folders_named_sprigcast_are_ever_deleted():
    assert clear_data.ours(Path(r"C:/Users/me/AppData/Local/Slothbloom/SprigCast"))
    assert clear_data.ours(Path(r"C:/ProgramData/SprigCast Camera"))
    assert clear_data.ours(Path(r"C:/Users/me/Videos/SprigCast"))
    assert not clear_data.ours(Path(r"D:/Videos"))
    assert not clear_data.ours(Path(r"C:/Users/SprigCastFan/Videos"))


def test_the_folders_found_are_ours_and_exist(tmp_path, monkeypatch):
    from PySide6.QtCore import QStandardPaths

    QStandardPaths.setTestModeEnabled(True)
    try:
        mine = tmp_path / "SprigCast"
        mine.mkdir()
        other = tmp_path / "Videos"
        other.mkdir()
        monkeypatch.setenv("APPDATA", str(tmp_path))
        found = clear_data.data_folders(recordings=other)
        assert mine in found and other not in found
        assert all(clear_data.ours(f) for f in found)
        assert clear_data.data_folders(recordings=mine).count(mine) == 1
    finally:
        QStandardPaths.setTestModeEnabled(False)


def test_the_windows_clean_up_waits_then_removes_folders_and_settings():
    script = clear_data.removal_script([Path(r"C:\Users\me\Documents\SprigCast")], 4321)
    lines = script.split("\r\n")
    assert 'find " 4321 "' in lines[3] and "goto wait" in lines[3]  # waits for this SprigCast to close
    assert 'rmdir /s /q "C:\\Users\\me\\Documents\\SprigCast"' in script
    assert r'reg delete "HKCU\Software\Slothbloom\SprigCast" /f' in script
    assert script.index("goto wait") < script.index("rmdir") < script.index('del "%~f0"')
    assert "timeout" not in script  # needs a console, which this has none of


def test_clear_all_forgets_settings_and_secrets_then_removes(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    from sprigcast.tools import forget_keys

    calls = []
    monkeypatch.setattr(forget_keys, "forget", lambda: calls.append("creds") or 2)
    monkeypatch.setattr(forget_keys, "forget_settings", lambda: calls.append("secrets") or 1)
    monkeypatch.setattr(forget_keys, "forget_files", lambda: calls.append("files") or 0)
    folder = tmp_path / "SprigCast"
    (folder / "Logs").mkdir(parents=True)
    monkeypatch.setattr(clear_data, "data_folders", lambda recordings=None: [folder])
    removed = []
    monkeypatch.setattr(clear_data, "remove_after_exit", lambda folders: removed.extend(folders))
    QSettings().setValue("clear-data-test/value", 1)
    assert clear_data.clear_all() == [folder]
    assert calls == ["creds", "secrets", "files"] and removed == [folder]
    assert QSettings().value("clear-data-test/value") is None


def test_off_windows_folders_go_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(clear_data.sys, "platform", "linux")
    folder = tmp_path / "SprigCast"
    (folder / "a").mkdir(parents=True)
    clear_data.remove_after_exit([folder])
    assert not folder.exists()
