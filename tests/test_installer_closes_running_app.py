"""A silent Setup closes a running SprigCast itself, since its suppressed "SprigCast is running" box cancels
Setup (packaging/sprigcast.iss InitializeSetup)."""

from pathlib import Path

from sprigcast.app.main import APP_MUTEX

ISS = (Path(__file__).resolve().parent.parent / "packaging" / "sprigcast.iss").read_text(encoding="utf-8")


def test_the_installer_waits_on_the_apps_own_mutex():
    assert f'#define AppMutexName "{APP_MUTEX}"' in ISS
    assert "AppMutex={#AppMutexName}" in ISS


def test_a_silent_setup_closes_sprigcast_before_the_mutex_check():
    body = ISS.split("function InitializeSetup: Boolean;", 1)[1].split("\nend;", 1)[0]
    assert "WizardSilent" in body  # a normal install still asks
    assert "'/im SprigCast.exe'" in body  # asked to close first
    assert "'/f /t /im SprigCast.exe'" in body  # then ended
    assert "Result := True" in body
