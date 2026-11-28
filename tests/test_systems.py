"""The Linux and macOS versions' foundation (core/systems.py): where its files go, how it saves secrets, and what it says
about the parts that aren't on Linux yet."""

import sys
from pathlib import Path

import pytest

from sprigcast.control import secret_store
from sprigcast.core import systems

on_linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")


def test_state_folder_follows_the_desktop(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert systems.state_dir() == tmp_path / "SprigCast"
    monkeypatch.setenv("XDG_STATE_HOME", "relative/path")  # the desktop's rule: a relative one is ignored
    assert systems.state_dir() == Path.home() / ".local" / "state" / "SprigCast"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert systems.state_dir() == Path.home() / ".local" / "state" / "SprigCast"


@on_linux
def test_logs_go_to_the_state_folder_not_documents(monkeypatch, tmp_path):
    from sprigcast.app import logbook

    monkeypatch.delenv("SPRIGCAST_LOG_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert logbook.log_dir() == tmp_path / "SprigCast" / "Logs"
    monkeypatch.setenv("SPRIGCAST_LOG_DIR", str(tmp_path / "mine"))  # a folder asked for still wins
    assert logbook.log_dir() == tmp_path / "mine"


def test_words_and_folders_per_system(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert systems.not_yet("Virtual camera") == "unavailable: Virtual camera isn't on Linux yet"
    line = systems.start_line()
    assert line.startswith("Linux: not built yet here: ") and all(part in line for part in systems.NOT_YET)
    assert systems.log_dir() == systems.state_dir() / "Logs"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert systems.not_yet("Virtual camera") == "unavailable: Virtual camera isn't on macOS yet"
    assert systems.start_line().startswith("macOS: not built yet here: ")
    assert systems.log_dir() == Path.home() / "Library" / "Logs" / "SprigCast"
    monkeypatch.setattr(sys, "platform", "win32")
    assert systems.log_dir() is None  # Windows: Documents, as ever


@on_linux
def test_sound_and_capture_sources_say_not_on_linux_yet():
    from sprigcast.sources import audio, capture

    with pytest.raises(RuntimeError, match="Microphone and desktop sound isn't on Linux yet"):
        audio._pyaudio()
    with pytest.raises(RuntimeError, match="Screen, window and game capture isn't on Linux yet"):
        capture._load_backend()


@on_linux
def test_a_pc_without_a_password_store_says_so_in_plain_words(monkeypatch):
    import keyring

    from sprigcast.output import keys

    def no_store(*_args):
        raise keyring.errors.NoKeyringError("No recommended backend was available.")

    monkeypatch.setattr(keyring, "set_password", no_store)
    monkeypatch.setattr(keyring, "delete_password", no_store)
    monkeypatch.setattr(keyring, "get_password", no_store)
    with pytest.raises(RuntimeError, match="GNOME Keyring or KWallet"):
        keys.set_key("sprigcast/out-1", "live_abc")
    keys.set_key("sprigcast/out-1", "")  # nothing to remove: no error
    # The Control API's password is kept as it was, and still reads back.
    assert secret_store.protect("leafy") == "leafy" and secret_store.unprotect("leafy") == "leafy"
    assert secret_store.unprotect("keyring:abc") == ""


def test_a_keyring_value_cannot_be_read_off_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert secret_store.is_protected("keyring:abc") and secret_store.unprotect("keyring:abc") == ""
