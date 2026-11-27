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
    monkeypatch.setattr(service_signin, "DEFAULTS", {"youtube/client_id": "yt-id", "kick/client_id": "k-id"})
    monkeypatch.setattr(service_signin, "setting", lambda name: service_signin.DEFAULTS.get(name, ""))


def test_youtube_reads_the_channel_and_a_reusable_key(ids, monkeypatch):
    posts = []
    monkeypatch.setattr(service_signin, "post_form", lambda url, fields, who, **kw: posts.append((url, fields)) or
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
    url, token_call = posts[0]
    assert url == service_signin.HELPER_URL  # the helper adds the secret: none in the app
    assert token_call["service"] == "youtube" and token_call["code"] == "abc" and token_call["code_verifier"]
    assert "client_secret" not in token_call and token_call["redirect_uri"] == seen["redirect_uri"]
    assert token_call["keep"] == "1" and seen["access_type"] == "offline"  # one sign-in: Stream details too
    assert not any(url.endswith("/revoke") for url, _ in posts)  # Google would end the whole grant


def test_kick_reads_server_and_key_on_its_fixed_redirect(ids, monkeypatch):
    posts = []
    monkeypatch.setattr(service_signin, "post_form", lambda url, fields, who, **kw: posts.append((url, fields)) or
                        {"access_token": "tok"})
    monkeypatch.setattr(service_signin, "get_json", lambda url, token, who, headers=None: {"data": [
        {"slug": "slothbloom", "stream": {"url": "rtmps://ingest.kick/app", "key": "sk_1"}}]})
    open_url, seen = _browser()
    got = service_signin.kick(open_url=open_url)
    assert (got.account, got.key, got.server) == ("slothbloom", "sk_1", "rtmps://ingest.kick/app")
    assert seen["redirect_uri"] == service_signin.KICK_REDIRECT and "streamkey:read" in seen["scope"]
    assert posts[0][1]["service"] == "kick" and "client_secret" not in posts[0][1]


def test_sign_in_is_only_offered_once_registered(monkeypatch):
    monkeypatch.setattr(service_signin, "setting", lambda name: "")
    assert not service_signin.available("youtube") and not service_signin.available("kick")
    assert not service_signin.available("x")
    with pytest.raises(SignInError, match="Paste the key"):
        service_signin.youtube(open_url=lambda u: None)


def test_get_started_signs_in_on_connect_only():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    from sprigcast.app import getting_started as gs

    QApplication.instance() or QApplication([])
    asked = []

    def sign_in(service, parent):
        asked.append(service)
        return service_signin.Fetched(account="Sloth Bloom", key="k")

    d = gs.GettingStartedDialog(can_sign_in=lambda s: s == "youtube", sign_in=sign_in)
    d.content["gaming"].click()
    d.shapes["horizontal"].click()
    d.live.click()
    d.places["youtube"].click()
    d.places["kick"].click()
    assert asked == []  # picking never opens a sign-in
    d.go.click()
    d.connect_rows["youtube"].connect_button.click()
    assert asked == ["youtube"] and list(d.connect_rows) == ["youtube"]  # Kick without its sign-in: not listed
    d.skip.click()
    d.places["youtube"].click()
    d.places["youtube"].click()  # off and on again: still signed in, no second sign-in
    assert asked == ["youtube"]
    assert d.places["youtube"].note == "Sloth Bloom" and d.places["kick"].note == "Key next"


def test_no_secret_ships_in_the_app():
    import inspect

    text = inspect.getsource(service_signin)
    assert "GOCSPX" not in text and "client_secret" not in text


def test_a_vertical_youtube_place_gets_its_own_key_for_dual_stream(monkeypatch):
    """YouTube's Dual stream takes the vertical feed on a second key: never the horizontal one."""
    made = []
    streams = [{"contentDetails": {"isReusable": True}, "snippet": {"title": "SprigCast"},
                "cdn": {"ingestionInfo": {"streamName": "wide"}}}]
    monkeypatch.setattr(service_signin, "get_json", lambda url, token, who, headers=None: {"items": list(streams)})

    class Reply:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def urlopen(request, timeout=10):
        import json

        body = json.loads(request.data)
        made.append(body["snippet"]["title"])
        return Reply(json.dumps({"cdn": {"ingestionInfo": {"streamName": "tall"}}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert service_signin._youtube_key("tok") == "wide"
    assert service_signin._youtube_key("tok", vertical=True) == "tall" and made == ["SprigCast Vertical"]
    streams.append({"contentDetails": {"isReusable": True}, "snippet": {"title": "SprigCast Vertical"},
                    "cdn": {"ingestionInfo": {"streamName": "tall"}}})
    streams.reverse()  # the vertical one listed first: horizontal still gets its own
    assert service_signin._youtube_key("tok") == "wide"
    assert service_signin._youtube_key("tok", vertical=True) == "tall" and len(made) == 1


def test_the_helper_is_asked_again_when_it_doesnt_answer(monkeypatch):
    import io
    import urllib.request

    from sprigcast.output import oauth_loopback

    seen = []

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        seen.append(timeout)
        if len(seen) == 1:
            raise TimeoutError("The read operation timed out")
        return Reply(b'{"access_token": "a"}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(oauth_loopback.time, "sleep", lambda s: None)
    reply = oauth_loopback.post_form("https://helper.test", {"code": "c"}, "the helper",
                                     timeout=oauth_loopback.HELPER_TIMEOUT, tries=2)
    assert reply == {"access_token": "a"}
    assert seen == [oauth_loopback.HELPER_TIMEOUT] * 2


def test_the_helper_isnt_asked_again_after_a_refusal(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    from sprigcast.output import oauth_loopback

    seen = []

    def urlopen(request, timeout):
        seen.append(1)
        raise urllib.error.HTTPError(request.full_url, 400, "Bad", {}, io.BytesIO(b'{"error": "invalid_grant"}'))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    reply = oauth_loopback.post_form("https://helper.test", {"code": "c"}, "the helper", tries=2)
    assert reply["error"] == "invalid_grant" and len(seen) == 1


def test_one_destination_sign_in_also_signs_in_stream_details_and_names_chat(ids, monkeypatch):
    import json

    from PySide6.QtCore import QSettings

    from sprigcast.app.chat_panel import NAMES
    from sprigcast.output import stream_details

    assert service_signin.CHAT_NAMES == NAMES
    QSettings().setValue(NAMES, json.dumps({"twitch": "sloth"}))
    monkeypatch.setattr(service_signin, "post_form", lambda url, fields, who, **kw: {
        "access_token": "tok", "refresh_token": "ref", "expires_in": 3600})
    monkeypatch.setattr(service_signin, "get_json", lambda url, token, who, headers=None: {"data": [
        {"slug": "slothbloom", "stream": {"url": "rtmps://ingest.kick/app", "key": "sk_1"}}]})
    open_url, seen = _browser()
    service_signin.kick(open_url=open_url)
    assert "channel:write" in seen["scope"] and "streamkey:read" in seen["scope"]
    assert stream_details.account("kick") == "slothbloom"
    assert json.loads(stream_details.get_key(stream_details.TOKEN_REF["kick"]))["refresh_token"] == "ref"
    assert json.loads(QSettings().value(NAMES)) == {"twitch": "sloth", "kick": "slothbloom"}
    QSettings().setValue(NAMES, json.dumps({"kick": "mine"}))
    service_signin.kick(open_url=_browser()[0])
    assert json.loads(QSettings().value(NAMES)) == {"kick": "mine"}  # a channel you typed stays
    stream_details._remember("kick", "")
    stream_details.set_key(stream_details.TOKEN_REF["kick"], "")
    QSettings().remove(NAMES)


def test_youtube_sends_to_the_key_a_stream_is_waiting_on_not_the_first_listed(monkeypatch):
    """The channel's first reusable key was taken, and with an old key listed first SprigCast sent to a dead one:
    YouTube closed the connection five times with only "I/O error" (live 2026-11-21)."""
    def stream(ident, key, title="Default stream key"):
        return {"id": ident, "contentDetails": {"isReusable": True}, "snippet": {"title": title},
                "cdn": {"ingestionType": "rtmp", "ingestionInfo": {"streamName": key}}}

    old, current, other = stream("s-old", "dead-key"), stream("s-now", "live-key"), stream("s-3", "third")
    pick = service_signin.pick_youtube_key
    assert pick([old, current], None, ("s-now",))[0] == "live-key"
    assert pick([old, current], "dead-key", ("s-now",))[0] == "live-key"  # nothing waits on the saved one
    assert pick([old, current, other], "third", ("s-now", "s-3"))[0] == "third"  # a stream waits on the saved one
    assert pick([old, current], "live-key", ())[0] == "live-key"  # YouTube didn't say: the saved one stays
    assert pick([old, current], "gone", ())[0] == "dead-key" and pick([], "x", ("s-now",)) == ("", "")

    asked = []

    def yt(token, method, path, body=None):
        asked.append(path.split("broadcastStatus=")[1].split("&")[0])
        if "active" in path:
            return {"items": []}
        return {"items": [{"snippet": {"scheduledStartTime": "2026-12-01"}, "contentDetails": {"boundStreamId": "s-3"}},
                          {"snippet": {"scheduledStartTime": "2026-11-21"}, "contentDetails": {"boundStreamId": "s-now"}},
                          {"snippet": {}, "contentDetails": {}}]}

    from sprigcast.output import stream_details

    monkeypatch.setattr(stream_details, "_yt", yt)
    streams = [old, current, other, stream("s-v", "tall", "SprigCast Vertical")]
    monkeypatch.setattr(service_signin, "get_json", lambda url, token, who, headers=None: {"items": list(streams)})
    assert service_signin._youtube_key("tok", saved="dead-key") == "live-key" and asked == ["active", "upcoming"]
    assert service_signin._youtube_key("tok", saved="third") == "third"
    asked.clear()
    assert service_signin._youtube_key("tok", vertical=True, saved="dead-key") == "tall" and asked == []

    def down(token, method, path, body=None):
        raise stream_details.DetailsError("YouTube isn't answering.")

    monkeypatch.setattr(stream_details, "_yt", down)
    assert service_signin._youtube_key("tok", saved="live-key") == "live-key"


def test_going_live_checks_a_signed_in_youtube_key_and_keeps_the_saved_one_when_youtube_is_silent(monkeypatch):
    import types

    mw = pytest.importorskip("sprigcast.app.main_window")
    from sprigcast.output import keys, stream_details

    said, saved = [], {}
    window = types.SimpleNamespace(_say=said.append)
    window.statusBar = lambda: types.SimpleNamespace(showMessage=lambda text, ms: said.append(text))
    output = types.SimpleNamespace(canvas="horizontal", key_ref="ref-1", account="Sloth Bloom", service="youtube")
    monkeypatch.setattr(keys, "set_key", lambda ref, key: saved.update({ref: key}))
    monkeypatch.setattr(stream_details, "account", lambda service: "Sloth Bloom")
    asked = []

    def from_account(service, vertical=False, saved=None):
        asked.append((service, vertical, saved))
        return service_signin.Fetched(account="Sloth Bloom", key="live-key")

    monkeypatch.setattr(service_signin, "key_from_account", from_account)
    fresh = mw.MainWindow._fresh_youtube_key
    assert fresh(window, output, "dead-key") == "live-key" and saved == {"ref-1": "live-key"}
    assert asked == [("youtube", False, "dead-key")] and "live-key" not in " ".join(said)
    assert fresh(window, output, "live-key") == "live-key" and len(saved) == 1

    def down(*a, **k):
        raise service_signin.SignInError("Couldn't reach YouTube.")

    monkeypatch.setattr(service_signin, "key_from_account", down)
    assert fresh(window, output, "dead-key") == "dead-key"
    monkeypatch.setattr(stream_details, "account", lambda service: "")
    monkeypatch.setattr(service_signin, "key_from_account", from_account)
    assert fresh(window, output, "dead-key") == "dead-key" and len(asked) == 2  # not signed in: nothing is asked


def test_tiktok_names_the_account_with_a_hex_challenge_and_never_touches_the_key(monkeypatch):
    import hashlib
    import json

    from sprigcast.output import stream_details

    monkeypatch.setattr(service_signin, "setting", lambda name: {"tiktok/client_id": "tt-key"}.get(name, ""))
    posts = []
    monkeypatch.setattr(service_signin, "post_form", lambda url, fields, who, **kw: posts.append((url, fields)) or
                        {"access_token": "tok", "refresh_token": "ref", "expires_in": 86400})
    asked = []

    def get_json(url, token, who, headers=None):
        asked.append(url)
        return {"data": {"user": {"open_id": "o", "display_name": "Sloth Bloom", "username": "slothbloom"}},
                "error": {"code": "ok", "message": ""}}

    monkeypatch.setattr(service_signin, "get_json", get_json)
    open_url, seen = _browser()
    got = service_signin.tiktok(open_url=open_url)
    assert (got.account, got.key) == ("Sloth Bloom (@slothbloom)", "")
    assert seen["client_key"] == "tt-key" and seen["scope"] == "user.info.basic,user.info.profile"
    assert seen["redirect_uri"].startswith("http://127.0.0.1:") and seen["redirect_uri"].endswith("/callback")
    verifier = posts[0][1]["code_verifier"]
    assert seen["code_challenge"] == hashlib.sha256(verifier.encode()).hexdigest()  # TikTok wants hex
    assert posts[0][1]["service"] == "tiktok" and posts[0][1]["keep"] == "1" and "client_secret" not in posts[0][1]
    assert asked == [f"{service_signin.TIKTOK_API}/user/info/?fields=open_id,display_name,username"]
    assert stream_details.account("tiktok") == "Sloth Bloom (@slothbloom)"
    assert json.loads(stream_details.get_key(stream_details.TOKEN_REF["tiktok"]))["refresh_token"] == "ref"
    stream_details._remember("tiktok", "")
    stream_details.set_key(stream_details.TOKEN_REF["tiktok"], "")


def test_tiktok_account_text_and_refusals():
    assert service_signin.tiktok_account({"display_name": "A", "username": "@b"}) == "A (@b)"
    assert service_signin.tiktok_account({"username": "b"}) == "@b"
    assert service_signin.tiktok_account({}) == "TikTok"
    assert "tiktok" not in service_signin.GIVES_KEY


def test_tiktok_sign_out_revokes_through_the_helper(monkeypatch):
    import json

    from sprigcast.output import oauth_loopback as loop
    from sprigcast.output import stream_details

    stream_details.set_key(stream_details.TOKEN_REF["tiktok"], json.dumps({"refresh_token": "ref"}))
    stream_details._remember("tiktok", "A (@b)")
    posts = []
    monkeypatch.setattr(loop, "post_form", lambda url, fields, who, **kw: posts.append((url, fields)) or {})
    stream_details.sign_out("tiktok")
    assert posts == [(service_signin.helper_url(), {"service": "tiktok", "grant_type": "revoke", "token": "ref"})]
    assert stream_details.account("tiktok") == "" and not stream_details.get_key(stream_details.TOKEN_REF["tiktok"])
