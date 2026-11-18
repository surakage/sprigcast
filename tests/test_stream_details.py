"""Stream details: title, category, tags and the rest go straight to Twitch, YouTube and Kick."""

import json
import os
import time
import urllib.parse

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.output import service_signin, twitch_auth  # noqa: E402
from sprigcast.output import stream_details as sd  # noqa: E402
from sprigcast.output.keys import get_key, set_key  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def app():
    return QApplication.instance() or QApplication([])


class Web:
    """Records calls and answers them from a list of (method, url-start) -> reply or exception."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.answers = []
        monkeypatch.setattr(sd, "_call", self._call)

    def on(self, method, start, reply):
        self.answers.append((method, start, reply))

    def _call(self, method, url, token, body=None, *, headers=None, data=None, who=""):
        self.calls.append({"method": method, "url": url, "token": token, "body": body, "data": data,
                           "headers": headers or {}})
        for i, (m, start, reply) in enumerate(self.answers):
            if m == method and url.startswith(start):
                if isinstance(reply, list):  # answers in turn
                    reply = reply.pop(0) if len(reply) > 1 else reply[0]
                if isinstance(reply, BaseException):
                    raise reply
                return reply
        raise AssertionError(f"unexpected {method} {url}")


def _twitch_signed_in(scope=("channel:read:stream_key", "user:write:chat", sd.TWITCH_SCOPE), expires=None):
    set_key(twitch_auth.TOKEN_REF, json.dumps({"access_token": "tw", "refresh_token": "r", "user_id": "42",
                                               "login": "sloth", "scope": list(scope),
                                               "expires_at": expires or time.time() + 3600}))


def _signed_in(service, expires=None):
    set_key(sd.TOKEN_REF[service], json.dumps({"access_token": "acc", "refresh_token": "ref", "account": "Sloth",
                                               "expires_at": expires or time.time() + 3600}))


# Tags ---------------------------------------------------------------------------------------------------


def test_tags_from_what_was_typed():
    assert sd.clean_tags("#Cozy, English  cozy, Just Chatting") == ["Cozy", "English", "Just", "Chatting"]
    assert sd.clean_tags(["LGBTQIA+", "  "]) == ["LGBTQIA"]
    assert sd.tag_problem([f"t{i}" for i in range(11)]).startswith("Twitch takes at most 10")
    assert "25 letters" in sd.tag_problem(["x" * 26])
    assert sd.tag_problem(["Cozy"]) is None


# Twitch -------------------------------------------------------------------------------------------------


def test_twitch_reads_and_saves_title_category_and_tags(monkeypatch):
    _twitch_signed_in()
    web = Web(monkeypatch)
    web.on("GET", f"{twitch_auth.HELIX}/channels?broadcaster_id=42",
           {"data": [{"title": "Hi", "game_name": "Just Chatting", "game_id": "509658", "tags": ["Cozy"]}]})
    web.on("PATCH", f"{twitch_auth.HELIX}/channels?broadcaster_id=42", {})
    details = sd.load("twitch")
    assert (details.title, details.category, details.category_id, details.tags) == \
        ("Hi", "Just Chatting", "509658", ["Cozy"])
    details.title, details.tags = "  New title ", ["Cozy", "#English"]
    sd.save("twitch", details)
    patch = web.calls[-1]
    assert patch["body"] == {"title": "New title", "tags": ["Cozy", "English"], "game_id": "509658"}
    assert patch["headers"]["Client-Id"] == twitch_auth.client_id()
    details.category, details.category_id = "", ""
    sd.save("twitch", details)
    assert web.calls[-1]["body"]["game_id"] == "0"  # no category


def test_twitch_without_the_new_permission_asks_to_reconnect(monkeypatch):
    Web(monkeypatch)
    _twitch_signed_in(scope=("channel:read:stream_key", "user:write:chat"))
    with pytest.raises(sd.NeedsSignIn, match="Reconnect Twitch once"):
        sd.load("twitch")
    assert not sd.twitch_can_edit()
    set_key(twitch_auth.TOKEN_REF, "")
    with pytest.raises(sd.NeedsSignIn, match="Connect Twitch"):
        sd.load("twitch")


def test_twitch_refreshes_a_turned_away_token_once(monkeypatch):
    _twitch_signed_in()
    web = Web(monkeypatch)
    web.on("GET", f"{twitch_auth.HELIX}/search/categories", [PermissionError("old"),
                                                            {"data": [{"id": "1", "name": "Minecraft"}]}])
    monkeypatch.setattr(twitch_auth, "_refresh", lambda tokens, cid: {**tokens, "access_token": "fresh"})
    assert sd.search_categories("twitch", "mine") == [("1", "Minecraft")]
    assert [c["token"] for c in web.calls] == ["tw", "fresh"]
    assert "query=mine" in web.calls[-1]["url"]
    assert sd.search_categories("twitch", "m") == []  # too short to look up


def test_twitch_scope_is_asked_for_at_connect():
    assert sd.TWITCH_SCOPE in twitch_auth.SCOPES.split()


# YouTube ------------------------------------------------------------------------------------------------

LIVE = sd.YOUTUBE_API + "/liveBroadcasts?part=id,snippet,status&broadcastStatus=active"
WAITING = sd.YOUTUBE_API + "/liveBroadcasts?part=id,snippet,status&broadcastStatus=upcoming"


def _broadcast(bid, start, title="Stream"):
    return {"id": bid, "snippet": {"title": title, "description": "d", "scheduledStartTime": start},
            "status": {"privacyStatus": "unlisted"}}


def test_youtube_edits_the_stream_live_now_else_the_soonest_waiting(monkeypatch):
    _signed_in("youtube")
    web = Web(monkeypatch)
    web.on("GET", LIVE, [{"items": []}, {"items": [_broadcast("live1", "2026-11-18T01:00:00Z", "On now")]}])
    web.on("GET", WAITING, {"items": [_broadcast("later", "2026-11-20T01:00:00Z"),
                                      _broadcast("soon", "2026-11-19T01:00:00Z", "Soon")]})
    web.on("GET", sd.YOUTUBE_API + "/videos?part=snippet&id=soon", {"items": [{"snippet": {"categoryId": "24"}}]})
    web.on("GET", sd.YOUTUBE_API + "/videos", sd.DetailsError("no video"))
    first = sd.load("youtube")
    assert (first.broadcast_id, first.title, first.privacy, first.starts, first.video_category) == \
        ("soon", "Soon", "unlisted", "2026-11-19T01:00:00Z", "24")
    assert sd.load("youtube").broadcast_id == "live1"


def test_youtube_saves_title_description_and_privacy_keeping_the_start(monkeypatch):
    _signed_in("youtube")
    web = Web(monkeypatch)
    web.on("PUT", sd.YOUTUBE_API + "/liveBroadcasts?part=snippet,status", {})
    details = sd.Details(title="New", description="All about sloths", privacy="private", broadcast_id="b1",
                         starts="2026-11-19T01:00:00Z")
    sd.save("youtube", details)
    assert web.calls[-1]["body"] == {"id": "b1", "snippet": {"title": "New", "description": "All about sloths",
                                                             "scheduledStartTime": "2026-11-19T01:00:00Z"},
                                     "status": {"privacyStatus": "private"}}


def test_youtube_category_goes_on_the_video_with_its_tags(monkeypatch):
    _signed_in("youtube")
    web = Web(monkeypatch)
    web.on("PUT", sd.YOUTUBE_API + "/liveBroadcasts", {})
    web.on("GET", sd.YOUTUBE_API + "/videos?part=snippet&id=b1",
           [{"items": [{"snippet": {"categoryId": "24", "tags": ["sloth"], "title": "Old"}}]},
            {"items": [{"snippet": {"categoryId": "20", "tags": ["sloth"]}}]}, sd.DetailsError("YouTube said 500.")])
    web.on("PUT", sd.YOUTUBE_API + "/videos?part=snippet", {})
    details = sd.Details(title="New", description="d", broadcast_id="b1", starts="s", video_category="20",
                         tags=["sloth"], old_tags=["sloth"])
    assert sd.save("youtube", details).note == ""
    assert web.calls[-1]["body"] == {"id": "b1", "snippet": {"title": "New", "description": "d",
                                                             "categoryId": "20", "tags": ["sloth"]}}
    count = len(web.calls)
    sd.save("youtube", details)  # already Gaming with these tags: nothing more is sent
    assert web.calls[-1]["method"] == "GET" and len(web.calls) == count + 2
    assert sd.save("youtube", details).note == "Saved, but not the category or tags."
    assert "20" in sd.YOUTUBE_CATEGORIES and sd.YOUTUBE_CATEGORIES["20"] == "Gaming"


def test_youtube_tags_are_saved_on_the_video(monkeypatch):
    _signed_in("youtube")
    web = Web(monkeypatch)
    web.on("PUT", sd.YOUTUBE_API + "/liveBroadcasts", {})
    web.on("GET", sd.YOUTUBE_API + "/videos?part=snippet&id=b1",
           {"items": [{"snippet": {"categoryId": "20", "defaultLanguage": "en"}}]})
    web.on("PUT", sd.YOUTUBE_API + "/videos?part=snippet", {})
    saved = sd.save("youtube", sd.Details(title="T", broadcast_id="b1", starts="s", tags=["Sloth facts", "cozy"]))
    assert web.calls[-1]["body"]["snippet"] == {"defaultLanguage": "en", "title": "T", "description": "",
                                                "categoryId": "20", "tags": ["Sloth facts", "cozy"]}
    assert saved.old_tags == ["Sloth facts", "cozy"]
    assert sd.split_tags("Cozy, #Sloth  facts, cozy,") == ["Cozy", "Sloth facts"]
    assert sd.youtube_tag_problem(["x" * 30] * 20) and sd.youtube_tag_problem(["Cozy"]) is None


def test_youtube_with_nothing_waiting_makes_a_stream_on_the_sprigcast_key(monkeypatch):
    _signed_in("youtube")
    set_key("out-yt", "my-key")
    web = Web(monkeypatch)
    web.on("GET", sd.YOUTUBE_API + "/liveStreams", {"items": [
        {"id": "other", "cdn": {"ingestionInfo": {"streamName": "nope"}}, "contentDetails": {"isReusable": True}},
        {"id": "mine", "cdn": {"ingestionInfo": {"streamName": "my-key"}}, "contentDetails": {"isReusable": True}}]})
    web.on("POST", sd.YOUTUBE_API + "/liveBroadcasts?part=", {"id": "new1", "snippet": {"scheduledStartTime": "x"}})
    web.on("POST", sd.YOUTUBE_API + "/liveBroadcasts/bind", {})
    saved = sd.save("youtube", sd.Details(title="Hello", privacy="public"), stream_key_ref="out-yt")
    insert = next(c for c in web.calls if c["url"].startswith(sd.YOUTUBE_API + "/liveBroadcasts?part="))
    assert insert["body"]["contentDetails"] == {"enableAutoStart": True, "enableAutoStop": True}
    assert insert["body"]["snippet"]["scheduledStartTime"].endswith("Z")
    assert "streamId=mine" in web.calls[-1]["url"] and "id=new1" in web.calls[-1]["url"]
    assert saved.broadcast_id == "new1" and saved.starts == "x"


def test_youtube_thumbnail_goes_up_and_a_refusal_is_a_quiet_note(monkeypatch, tmp_path):
    from PySide6.QtGui import QColor, QImage

    picture = tmp_path / "thumb.png"
    image = QImage(1920, 1080, QImage.Format.Format_RGB32)
    image.fill(QColor("#4fd88f"))
    image.save(str(picture))
    _signed_in("youtube")
    web = Web(monkeypatch)
    web.on("PUT", sd.YOUTUBE_API + "/liveBroadcasts", {})
    web.on("POST", sd.YOUTUBE_UPLOAD + "/thumbnails/set?videoId=b1", [{}, sd._worded(
        "YouTube", 403, "The authenticated user doesn't have permissions to upload and set custom video thumbnails.")])
    details = sd.Details(title="T", broadcast_id="b1", starts="s")
    assert sd.save("youtube", details, str(picture)).note == ""
    upload = web.calls[-1]
    assert upload["headers"]["Content-Type"] == "image/jpeg" and upload["data"][:2] == b"\xff\xd8"
    shrunk = QImage.fromData(upload["data"])
    assert (shrunk.width(), shrunk.height()) == (1280, 720)
    saved = sd.save("youtube", details, str(picture))
    assert saved.note.startswith("Saved, but not the thumbnail. YouTube only takes custom thumbnails")


def test_youtube_needs_a_title_and_sign_in():
    with pytest.raises(sd.NeedsSignIn):
        sd.load("youtube")
    _signed_in("youtube")
    with pytest.raises(sd.DetailsError, match="needs a title"):
        sd.save("youtube", sd.Details(title="  "))


# Kick ---------------------------------------------------------------------------------------------------


def test_kick_reads_and_saves_title_and_category(monkeypatch):
    _signed_in("kick")
    web = Web(monkeypatch)
    web.on("GET", sd.KICK_API + "/channels", {"data": [{"stream_title": "Hi", "category": {"id": 15, "name": "Art"},
                                                        "slug": "sloth"}]})
    web.on("PATCH", sd.KICK_API + "/channels", {})
    details = sd.load("kick")
    assert (details.title, details.category, details.category_id) == ("Hi", "Art", "15")
    details.title = "New"
    sd.save("kick", details)
    assert web.calls[-1]["body"] == {"stream_title": "New", "category_id": 15}  # no tags then, none sent
    details.tags = ["Cozy", "#Sloth facts", "cozy"]
    sd.save("kick", details)
    assert web.calls[-1]["body"]["custom_tags"] == ["Cozy", "Sloth facts"]
    details.tags = []
    sd.save("kick", details)
    assert web.calls[-1]["body"]["custom_tags"] == []  # cleared on purpose


def test_kick_category_search_falls_back_to_the_newer_one(monkeypatch):
    _signed_in("kick")
    web = Web(monkeypatch)
    web.on("GET", sd.KICK_API + "/categories", sd.DetailsError("gone"))
    web.on("GET", sd.KICK_API_V2 + "/categories", {"data": [{"id": 7, "name": "Just Chatting"}]})
    assert sd.search_categories("kick", "just") == [("7", "Just Chatting")]


# Staying signed in ---------------------------------------------------------------------------------------


def test_an_expired_sign_in_is_renewed_through_the_helper(monkeypatch):
    _signed_in("kick", expires=time.time() - 5)
    sent = []

    def helper(url, fields, who):
        sent.append((url, dict(fields)))
        return {"access_token": "new", "refresh_token": "ref2", "expires_in": 7200}

    monkeypatch.setattr("sprigcast.output.oauth_loopback.post_form", helper)
    web = Web(monkeypatch)
    web.on("GET", sd.KICK_API + "/channels", {"data": [{"stream_title": "", "category": None}]})
    sd.load("kick")
    assert sent[0] == (service_signin.helper_url(), {"service": "kick", "grant_type": "refresh_token",
                                                     "refresh_token": "ref"})
    assert web.calls[0]["token"] == "new"
    kept = json.loads(get_key(sd.TOKEN_REF["kick"]))
    assert kept["refresh_token"] == "ref2" and kept["account"] == "Sloth"


def test_a_refused_renewal_signs_out(monkeypatch):
    _signed_in("youtube", expires=time.time() - 5)
    monkeypatch.setattr("sprigcast.output.oauth_loopback.post_form",
                        lambda url, fields, who: {"error": "invalid_grant", "status": 400})
    Web(monkeypatch)
    with pytest.raises(sd.NeedsSignIn, match="ran out"):
        sd.load("youtube")
    assert not get_key(sd.TOKEN_REF["youtube"])
    assert sd.account("youtube") == ""


def test_keeping_a_sign_in_asks_for_offline_access_and_edit_permission(monkeypatch):
    monkeypatch.setattr(service_signin.Waiter, "start", lambda self: None)
    yt = service_signin.begin("youtube", keep=True)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(yt.url).query)
    assert query["access_type"] == ["offline"] and "consent" in query["prompt"][0] and yt.keep
    plain = urllib.parse.parse_qs(urllib.parse.urlsplit(service_signin.begin("youtube").url).query)
    assert "access_type" not in plain
    kick = urllib.parse.parse_qs(urllib.parse.urlsplit(service_signin.begin("kick", keep=True).url).query)
    assert "channel:write" in kick["scope"][0].split()


def test_a_kept_sign_in_is_saved_and_named(monkeypatch):
    monkeypatch.setattr(service_signin, "post_form", lambda url, fields, who: (
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600} if fields.get("keep") == "1" else {}))
    monkeypatch.setattr(service_signin, "get_json", lambda url, token, who: {"items": [{"snippet": {"title": "Sloth"}}]})

    class W:
        url = "http://127.0.0.1:1/callback"

    flow = service_signin.Flow("youtube", "u", W(), "v", keep=True)
    fetched = service_signin._keep_rest(flow, "code")
    assert fetched.account == "Sloth" and fetched.key == ""
    assert json.loads(get_key(sd.TOKEN_REF["youtube"]))["refresh_token"] == "r"
    assert sd.account("youtube") == "Sloth"
    monkeypatch.setattr("sprigcast.output.oauth_loopback.post_form", lambda *a: {})
    sd.sign_out("youtube")
    assert sd.account("youtube") == "" and not get_key(sd.TOKEN_REF["youtube"])


# The pop-up ---------------------------------------------------------------------------------------------


def _wait(dialog, test, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        QApplication.processEvents()
        dialog.jobs._check()
        if test():
            return True
        time.sleep(0.01)
    return False


def test_the_card_shows_at_once_then_fills_and_saves(monkeypatch):
    from sprigcast.app import stream_details_dialog as sdd

    loaded = sd.Details(title="Hi", category="Art", category_id="15")
    calls = []

    def slow_load(service):
        time.sleep(0.2)
        return loaded

    monkeypatch.setattr(sd, "account", lambda service: "sloth" if service == "kick" else "")
    monkeypatch.setattr(sd, "load", slow_load)
    monkeypatch.setattr(sd, "save", lambda service, details, thumb, key: calls.append((service, details)) or details)
    started = time.time()
    dialog = sdd.StreamDetailsDialog(None, ["kick", "twitch"])
    assert time.time() - started < 0.15  # it doesn't wait on the web
    pane = dialog.panes["kick"]
    assert list(dialog.chips) == ["kick", "twitch", "youtube"]
    assert not dialog.save_button.isEnabled()
    assert _wait(dialog, lambda: pane.loaded)
    assert pane.title.text() == "Hi" and pane.category.text() == "Art"
    pane.category.setText("Unknown game")
    dialog._save()
    assert "Pick a category" in dialog.note.text() and not calls
    pane.category.setText("art")
    pane.title.setText("New")
    dialog._save()
    assert _wait(dialog, lambda: calls and dialog.note.text() == "Saved to Kick.")
    assert calls[0][1].title == "New" and calls[0][1].category_id == "15"
    dialog.show_service("twitch")  # nobody signed in: a button, no web call
    assert dialog.panes["twitch"].signed_out.isVisibleTo(dialog)
    assert dialog.panes["twitch"].sign_in.text() == "Connect Twitch"
    assert not dialog.save_button.isEnabled()
    dialog.done(0)
    dialog.deleteLater()


def test_a_failed_save_is_a_quiet_note(monkeypatch):
    from sprigcast.app import stream_details_dialog as sdd

    monkeypatch.setattr(sd, "account", lambda service: "sloth")
    monkeypatch.setattr(sd, "load", lambda service: sd.Details(title="Hi"))

    def fail(*_a):
        raise sd.DetailsError("Couldn't reach Twitch. Check your internet and try again.")

    monkeypatch.setattr(sd, "save", fail)
    dialog = sdd.StreamDetailsDialog(None, ["twitch"])
    pane = dialog.panes["twitch"]
    assert _wait(dialog, lambda: pane.loaded)
    dialog._save()
    assert _wait(dialog, lambda: "Couldn't reach Twitch" in dialog.note.text())
    assert dialog.save_button.isEnabled()
    dialog.done(0)
    dialog.deleteLater()
