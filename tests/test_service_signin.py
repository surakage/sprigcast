"""YouTube and Kick browser sign-in: the loopback catch, and what's read from each service."""

import threading
import urllib.parse
import urllib.request

import pytest

from sprigcast.output import oauth_loopback, service_signin
from sprigcast.output.oauth_loopback import SignInError, Waiter, free_port


def _browser(code="abc", state_override=None, error=None):
    """A stand-in browser: reads the sign-in URL, then comes back to its redirect_uri."""
    seen = {}

    def open_url(url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        seen.update(query)
        back = {"state": state_override or query["state"]}
        back.update({"error": error} if error else {"code": code})
        target = f"{query['redirect_uri']}?{urllib.parse.urlencode(back)}".replace("localhost", "127.0.0.1")
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()

    return open_url, seen


def test_the_loopback_catches_the_code_once_and_checks_the_state():
    w = Waiter(free_port())
    w.start()
    open_url, _ = _browser()
    open_url(f"https://example/auth?{urllib.parse.urlencode({'redirect_uri': w.url, 'state': w.state})}")
    assert w.wait(5) == "abc"
    w = Waiter(free_port())
    w.start()
    open_url, _ = _browser(state_override="forged")
    open_url(f"https://example/auth?{urllib.parse.urlencode({'redirect_uri': w.url, 'state': w.state})}")
    with pytest.raises(SignInError, match="didn't match"):
        w.wait(5)
    w = Waiter(free_port())
    w.start()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(oauth_loopback.Cancelled):
        w.wait(5, cancel)


@pytest.fixture
def ids(monkeypatch):
    monkeypatch.setattr(service_signin, "DEFAULTS", {"youtube/client_id": "yt-id", "youtube/client_secret": "",
                                                     "kick/client_id": "k-id", "kick/client_secret": "k-secret"})
    monkeypatch.setattr(service_signin, "setting", lambda name: service_signin.DEFAULTS.get(name, ""))


def test_youtube_reads_the_channel_and_a_reusable_key(ids, monkeypatch):
    posts = []
    monkeypatch.setattr(service_signin, "post_form", lambda url, fields, who: posts.append((url, fields)) or
                        {"access_token": "tok"})

    def get_json(url, token, who, headers=None):
        assert token == "tok"
        if "/channels" in url:
            return {"items": [{"snippet": {"title": "Sloth Bloom"}}]}
        return {"items": [{"contentDetails": {"isReusable": False}, "cdn": {"ingestionInfo": {"streamName": "old"}}},
                          {"contentDetails": {"isReusable": True}, "cdn": {"ingestionType": "rtmp",
                                                                           "ingestionInfo": {"streamName": "k-1"}}}]}

    monkeypatch.setattr(service_signin, "get_json", get_json)
    open_url, seen = _browser()
    got = service_signin.youtube(open_url=open_url)
    assert (got.account, got.key, got.server) == ("Sloth Bloom", "k-1", "")
    assert seen["code_challenge_method"] == "S256" and seen["redirect_uri"].startswith("http://127.0.0.1:")
    token_call = posts[0][1]
    assert token_call["code"] == "abc" and token_call["code_verifier"] and "client_secret" not in token_call
    assert posts[-1][0].endswith("/revoke")  # only the key is kept


def test_kick_reads_server_and_key_on_its_fixed_redirect(ids, monkeypatch):
    posts = []
    monkeypatch.setattr(service_signin, "post_form", lambda url, fields, who: posts.append((url, fields)) or
                        {"access_token": "tok"})
    monkeypatch.setattr(service_signin, "get_json", lambda url, token, who, headers=None: {"data": [
        {"slug": "slothbloom", "stream": {"url": "rtmps://ingest.kick/app", "key": "sk_1"}}]})
    open_url, seen = _browser()
    got = service_signin.kick(open_url=open_url)
    assert (got.account, got.key, got.server) == ("slothbloom", "sk_1", "rtmps://ingest.kick/app")
    assert seen["redirect_uri"] == service_signin.KICK_REDIRECT and "streamkey:read" in seen["scope"]
    assert posts[0][1]["client_secret"] == "k-secret"


def test_sign_in_is_only_offered_once_registered(monkeypatch):
    monkeypatch.setattr(service_signin, "setting", lambda name: "")
    assert not service_signin.available("youtube") and not service_signin.available("kick")
    assert not service_signin.available("x")
    with pytest.raises(SignInError, match="Paste the key"):
        service_signin.youtube(open_url=lambda u: None)


def test_get_started_signs_in_when_a_place_is_picked():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    from sprigcast.app import getting_started as gs

    QApplication.instance() or QApplication([])
    asked = []

    def sign_in(service, parent):
        asked.append(service)
        return service_signin.Fetched(account="Sloth Bloom", key="k")

    d = gs.GettingStartedDialog(can_sign_in=lambda s: s == "youtube", sign_in=sign_in)
    d.live.click()
    d.places["youtube"].click()
    d.places["kick"].click()
    d.places["youtube"].click()
    d.places["youtube"].click()  # off and on again: no second sign-in
    assert asked == ["youtube"]
    assert d.places["youtube"].note == "Sloth Bloom" and d.places["kick"].note == "Key next"
