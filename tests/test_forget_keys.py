import sys

import pytest

from sprigcast.output import keys
from sprigcast.tools import forget_keys


def test_matches_the_service_keys_are_saved_under():
    assert forget_keys.SERVICE == keys.KEYRING_SERVICE


@pytest.mark.parametrize("target, ours", [
    ("SprigCast", True),
    # what a PC kept after its data was wiped (2026-11-17)
    ("sprigcast/twitch@SprigCast", True),
    ("twitch/oauth@SprigCast", True),
    ("sprigcast/out-1@SprigCast", True),
    ("NotSprigCast", False),
    ("SprigCast@other", False),
    ("git:https://github.com", False),
])
def test_only_sprigcast_entries_are_ours(target, ours):
    assert forget_keys.is_ours(target) is ours


@pytest.mark.skipif(sys.platform == "win32", reason="off Windows only")
def test_does_nothing_off_windows():
    assert forget_keys.forget() == 0


def test_secret_settings_go_and_the_rest_stay(tmp_path):
    from PySide6.QtCore import QSettings

    q = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    q.setValue("control/password", "dpapi:AAAA")
    q.setValue("control/devices", "plain-from-an-old-build")
    q.setValue("control/devices_unreadable", "junk")
    q.setValue("myplugin/api_key", "dpapi:BBBB")
    q.setValue("control/port", 4455)
    q.setValue("general/theme", "slothbloom")
    assert forget_keys.forget_settings(q) == 4
    assert sorted(q.allKeys()) == ["control/port", "general/theme"]
