"""The system's password store is never waited on (core/password_store.py): the first start on an Ubuntu desktop
sat 3 min 23 s behind "Choose password for new keyring", and the box came back twice after Cancel (test build 16,
2026-11-28)."""
import sys
import threading
import time

import pytest

from sprigcast.core import password_store

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Windows' store never asks; it is called directly")


def test_an_answer_comes_straight_back_and_errors_pass_through():
    assert password_store.ask("reading", lambda: "live_abc") == "live_abc"
    with pytest.raises(KeyError):
        password_store.ask("reading", lambda: {}["missing"])
    assert password_store.state() == ""  # an ordinary error isn't a refusal


def test_an_open_box_holds_nothing_up_and_nothing_else_is_asked_meanwhile(monkeypatch):
    monkeypatch.setattr(password_store, "WAIT_S", 0.05)
    answered = threading.Event()
    asked = []

    def waits_on_the_box():
        asked.append(1)
        answered.wait(5)
        return "live_abc"

    began = time.monotonic()
    with pytest.raises(password_store.Unavailable):
        password_store.ask("reading twitch", waits_on_the_box)
    with pytest.raises(password_store.Unavailable):
        password_store.ask("reading kick", waits_on_the_box)  # not asked: one box at a time
    assert time.monotonic() - began < 1 and asked == [1]
    answered.set()
    for _ in range(100):
        if not password_store.state():
            break
        time.sleep(0.01)
    assert password_store.ask("reading twitch", lambda: "live_abc") == "live_abc"  # answered: works again


def test_a_dismissed_box_is_not_shown_again_this_run():
    class PromptDismissedException(Exception):
        pass

    asked = []

    def dismissed():
        asked.append(1)
        raise PromptDismissedException("Prompt dismissed")

    with pytest.raises(PromptDismissedException):
        password_store.ask("reading twitch", dismissed)
    for _ in range(3):
        with pytest.raises(password_store.Unavailable):
            password_store.ask("reading kick", dismissed)
    assert asked == [1] and "not asked again" in password_store.state()


def test_a_key_behind_an_open_box_reads_as_not_there_yet_without_marking_it_missing(monkeypatch):
    import keyring

    from sprigcast.output import keys

    monkeypatch.setattr(password_store, "WAIT_S", 0.05)
    gate = threading.Event()
    monkeypatch.setattr(keyring, "get_password", lambda *_: gate.wait(5) and "live_abc")
    assert keys.get_key("sprigcast/out-9") is None
    assert keys.probably_has_key("sprigcast/out-9")
    gate.set()


def test_the_settings_folder_is_this_users_alone(tmp_path, monkeypatch):
    from sprigcast.core import systems

    folder = tmp_path / "Slothbloom"
    folder.mkdir(mode=0o755)
    assert systems.private_settings(folder) and folder.stat().st_mode & 0o777 == 0o700
    monkeypatch.setattr(sys, "platform", "win32")
    assert not systems.private_settings(folder)
