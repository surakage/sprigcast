import sys
from types import SimpleNamespace

from sprigcast import winver


def test_off_windows_it_is_zero(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert winver.windows_build() == 0


def test_the_real_build_wins_over_what_the_manifest_lets_windows_say(monkeypatch):
    # An exe without a Windows 10 manifest: GetVersionEx says Windows 8 (9200), kernel32 says 26100.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "getwindowsversion",
                        lambda: SimpleNamespace(build=9200, platform_version=(10, 0, 26100)), raising=False)
    assert winver.windows_build() == 26100


def test_app_sound_asks_the_real_build(monkeypatch):
    from sprigcast.sources import app_audio

    assert app_audio.windows_build is winver.windows_build
    monkeypatch.setattr(app_audio, "windows_build", lambda: 18363)  # Windows 10 1909: too old
    src = app_audio.AppAudioSource.__new__(app_audio.AppAudioSource)
    src._run()
    assert src.status == "unavailable: needs Windows 10 (2004) or newer"
