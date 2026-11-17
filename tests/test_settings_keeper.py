"""Settings and pairings survive an update (2026-11-17: control/, window/ and general/optimized went missing)."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtCore")
from PySide6.QtCore import QByteArray, QSettings  # noqa: E402

from sprigcast.app import settings_keeper  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_keeper, "stopped", False)
    q = QSettings(str(tmp_path / "registry.ini"), QSettings.Format.IniFormat)
    q.setValue("control/port", 4456)
    q.setValue("control/enabled", True)
    q.setValue("control/password", "dpapi:pw")
    q.setValue("control/local_key", "dpapi:local")
    q.setValue("control/devices", "dpapi:streambridge+soak")
    q.setValue("window/geometry", QByteArray(b"\x01\x02geometry"))
    q.setValue("window/mode", "live")
    q.setValue("general/optimized", True)
    q.setValue("general/theme", "slothbloom")
    q.sync()
    return q


def test_settings_and_pairings_survive_an_update_that_loses_them(tmp_path, live):
    config = tmp_path / "config"
    assert settings_keeper.keep(config, live) >= 9  # the old version closing (or 30 s after its start)
    for key in ("control/port", "control/enabled", "control/devices", "window/geometry", "window/mode",
                "general/optimized"):  # what the 2026-11-17 update lost
        live.remove(key)
    live.setValue("general/theme", "light")  # changed since the copy: stays as it is
    back = settings_keeper.restore(config, live)  # the new version's first start
    assert set(back) == {"control/port", "control/enabled", "control/devices", "window/geometry", "window/mode",
                         "general/optimized"}
    assert live.value("control/port", type=int) == 4456 and live.value("control/devices") == "dpapi:streambridge+soak"
    assert bytes(live.value("window/geometry")) == b"\x01\x02geometry" and live.value("window/mode") == "live"
    assert live.value("general/theme") == "light"
    assert settings_keeper.restore(config, live) == []  # nothing missing: nothing to do


def test_an_emptied_registry_comes_back_whole(tmp_path, live):
    config = tmp_path / "config"
    settings_keeper.keep(config, live)
    live.clear()
    settings_keeper.restore(config, live)
    assert live.value("control/password") == "dpapi:pw" and live.value("control/local_key") == "dpapi:local"


def test_what_sprigcast_removes_on_purpose_stays_removed(tmp_path, live):
    config = tmp_path / "config"
    live.setValue("video/pending", "{}")
    live.setValue("upload/proven_kbps", 6000)
    settings_keeper.keep(config, live)
    for key in ("video/pending", "upload/proven_kbps", "control/local_key"):  # a new password drops local_key
        live.remove(key)
    assert settings_keeper.restore(config, live) == []


def test_clear_all_my_data_leaves_no_copy_and_makes_none(tmp_path, live, monkeypatch):
    config = tmp_path / "config"
    settings_keeper.keep(config, live)
    settings_keeper.forget(config)
    assert not settings_keeper.path(config).exists()
    assert settings_keeper.keep(config, live) == 0 and not settings_keeper.path(config).exists()
    live.clear()
    assert settings_keeper.restore(config, live) == []


def test_uninstall_takes_keys_and_pairings_out_of_the_copy(tmp_path, live):
    from sprigcast.tools import forget_keys

    config = tmp_path / "config"
    settings_keeper.keep(config, live)
    copy = QSettings(str(settings_keeper.path(config)), QSettings.Format.IniFormat)
    assert forget_keys.forget_settings(copy) == 3
    copy = QSettings(str(settings_keeper.path(config)), QSettings.Format.IniFormat)
    assert not copy.contains("control/password") and not copy.contains("control/devices")
    assert copy.value("control/port", type=int) == 4456  # choices stay for a reinstall
    src = (ROOT / "src" / "sprigcast" / "tools" / "forget_keys.py").read_text(encoding="utf-8")
    assert "forget_kept_copy()" in src.split("def main", 1)[1]


def test_installing_an_update_never_touches_the_settings():
    """Only the uninstaller's "delete my data" (which also deletes the copy's folder) removes the settings key."""
    text = (ROOT / "packaging" / "sprigcast.iss").read_text(encoding="utf-8")
    assert text.count("RegDeleteKeyIncludingSubkeys(HKCU, 'Software\\Slothbloom\\SprigCast')") == 1
    deleting = text.split("procedure DeleteSprigCastData;", 1)[1].split("\nend;", 1)[0]
    assert "Software\\Slothbloom\\SprigCast" in deleting and "{localappdata}\\Slothbloom\\SprigCast" in deleting
    assert "DeleteSprigCastData()" in text.split("usPostUninstall", 1)[1].split("\nend;", 1)[0]
    assert text.count("DeleteSprigCastData") == 2  # defined, and called only there
