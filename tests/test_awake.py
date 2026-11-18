"""SprigCast keeps the monitors and the PC awake while streaming or recording (core/awake.py)."""

from __future__ import annotations

from sprigcast.core import awake


def test_windows_is_told_only_on_a_change(monkeypatch):
    told = []
    monkeypatch.setattr(awake, "_set", lambda flags: told.append(flags) or True)
    monkeypatch.setattr(awake, "_held", False)
    awake.hold(False)
    assert told == []
    awake.hold(True)
    awake.hold(True)
    assert told == [awake.ES_CONTINUOUS | awake.ES_DISPLAY_REQUIRED | awake.ES_SYSTEM_REQUIRED] and awake.held()
    awake.hold(False)
    assert told[1:] == [awake.ES_CONTINUOUS] and not awake.held()


def test_off_windows_it_does_nothing(monkeypatch):
    monkeypatch.setattr(awake.sys, "platform", "linux")
    assert awake._set(awake.ES_CONTINUOUS) is False
