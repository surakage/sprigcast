import sys

import pytest

from sprigcast.output import keys
from sprigcast.tools import forget_keys


def test_matches_the_service_keys_are_saved_under():
    assert forget_keys.SERVICE == keys.KEYRING_SERVICE


@pytest.mark.parametrize("target, ours", [
    ("SprigCast", True),
    ("twitch-main@SprigCast", True),
    ("NotSprigCast", False),
    ("SprigCast@other", False),
    ("git:https://github.com", False),
])
def test_only_sprigcast_entries_are_ours(target, ours):
    assert forget_keys.is_ours(target) is ours


@pytest.mark.skipif(sys.platform == "win32", reason="off Windows only")
def test_does_nothing_off_windows():
    assert forget_keys.forget() == 0
