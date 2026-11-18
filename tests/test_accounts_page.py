"""Accounts: the one place to sign in and out of Twitch, YouTube and Kick."""

import pytest

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.app import accounts_page  # noqa: E402
from sprigcast.output import service_signin, stream_details, twitch_auth  # noqa: E402


def test_one_row_per_platform_with_sign_in_or_sign_out(monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(service_signin, "available", lambda s: s in ("youtube", "kick"))
    names = {"twitch": "sloth", "youtube": "", "kick": "slothbloom"}
    monkeypatch.setattr(accounts_page, "signed_in_as", lambda s: names[s])
    page = accounts_page.AccountsPage()
    assert list(page.rows) == ["twitch", "youtube", "kick"]
    texts = {s: (state.text(), button.text()) for s, (state, button) in page.rows.items()}
    assert texts == {"twitch": ("Signed in as sloth", "Sign out"), "youtube": ("Not signed in", "Sign in"),
                     "kick": ("Signed in as slothbloom", "Sign out")}


def test_signing_out_of_kick_forgets_it_here_and_tells_kick_on_a_worker(monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(service_signin, "available", lambda s: s == "kick")
    stream_details._remember("kick", "slothbloom")
    told = []
    monkeypatch.setattr(stream_details, "sign_out", lambda s: told.append(s))
    page = accounts_page.AccountsPage()
    assert page.rows["kick"][1].text() == "Sign out"
    page._clicked("kick")
    assert page.rows["kick"][0].text() == "Not signed in" and page.rows["kick"][1].text() == "Sign in"
    import time

    for _ in range(50):
        if told:
            break
        time.sleep(0.02)
    assert told == ["kick"]


def test_signing_out_of_twitch_disconnects(monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(service_signin, "available", lambda s: False)
    gone = []
    monkeypatch.setattr(twitch_auth, "disconnect", lambda: gone.append(True))
    monkeypatch.setattr(accounts_page, "signed_in_as", lambda s: "sloth" if not gone else "")
    page = accounts_page.AccountsPage()
    page._clicked("twitch")
    assert gone and page.rows["twitch"][1].text() == "Sign in"
