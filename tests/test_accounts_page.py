"""Accounts: signing out of Twitch, YouTube and Kick, and opening Settings → Stream to sign in."""

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from sprigcast.app import accounts_page  # noqa: E402
from sprigcast.core.scene import Output  # noqa: E402
from sprigcast.output import stream_details, twitch_auth  # noqa: E402


def test_signing_out_of_kick_forgets_it_here_and_tells_kick_on_a_worker(monkeypatch):
    QApplication.instance() or QApplication([])
    stream_details._remember("kick", "slothbloom")
    told = []
    monkeypatch.setattr(stream_details, "sign_out", lambda s: told.append(s))
    assert accounts_page.sign_out("kick") == ""
    assert stream_details.account("kick") == ""
    import time

    for _ in range(50):
        if told:
            break
        time.sleep(0.02)
    assert told == ["kick"]


def test_signing_out_of_twitch_disconnects(monkeypatch):
    gone = []
    monkeypatch.setattr(twitch_auth, "disconnect", lambda: gone.append(True))
    assert accounts_page.sign_out("twitch") == "" and gone

    def broken():
        raise OSError("no network")

    monkeypatch.setattr(twitch_auth, "disconnect", broken)
    assert "no network" in accounts_page.sign_out("twitch")


def test_sign_in_opens_settings_stream_on_that_platforms_place():
    QApplication.instance() or QApplication([])
    opened = []

    class Main(QWidget):
        class collection:  # noqa: N801
            outputs = [Output(id="rec", type="recording", canvas="horizontal"),
                       Output(id="tw", type="stream", service="twitch", canvas="horizontal"),
                       Output(id="yt", type="stream", service="youtube", canvas="horizontal")]

        def _open_settings(self, tab, output_id=None):
            opened.append((tab, output_id))

    main = Main()
    child = QWidget(main)
    accounts_page.open_accounts(child, "youtube")
    accounts_page.open_accounts(child, "kick")  # no Kick place yet: the place list
    assert opened == [("Stream/Places", "yt"), ("Stream/Places", None)]
