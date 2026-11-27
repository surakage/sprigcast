import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QCoreApplication, QPoint, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.chat import ChatMessage, moderation  # noqa: E402


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("ChatModTools")
    QSettings().clear()
    yield app
    QSettings().clear()


def _view(app, *messages, powers=("timeout", "ban", "delete")):
    from sprigcast.app import chat_panel

    view = chat_panel.ChatView()
    view.powers = lambda message: powers
    view.resize(420, 300)
    view.show()
    for message in messages:
        view.add(message)
    view.flush()
    app.processEvents()
    return view


def _line_point(view, row: int) -> QPoint:
    block = view.document().findBlockByNumber(row)
    rect = view.document().documentLayout().blockBoundingRect(block)
    return QPoint(40, round(rect.center().y() - view.verticalScrollBar().value()))


def test_hover_bar_shows_what_the_line_allows_and_ban_asks_first(app):
    view = _view(app, ChatMessage("twitch", "moss", "hello", id="m1", user_id="7"),
                 ChatMessage("twitch", "me", "hi", id="m2", user_id="1", badges=("broadcaster",)))
    asked = []
    view.moderate_asked.connect(lambda m, action, seconds: asked.append((m.id, action, seconds)))

    view._hover_line(_line_point(view, 0))
    assert view.quick.isVisible()
    assert all(b.isVisibleTo(view.quick) for b in view.quick.buttons.values())

    view.quick.buttons["timeout"].click()
    assert asked == [("m1", "timeout", 600)]

    view._hover_line(_line_point(view, 0))
    view.quick.buttons["ban"].click()  # first press only asks
    assert asked == [("m1", "timeout", 600)] and view.quick.buttons["ban"].text() == "Sure?"
    view.quick.buttons["ban"].click()
    assert asked[-1] == ("m1", "ban", 0)

    view._hover_line(_line_point(view, 1))  # the channel's owner: nothing to offer
    assert not view.quick.isVisible()
    view._hover_line(None)
    assert not view.quick.isVisible()


def test_hover_bar_hides_delete_without_a_message_id_and_stays_off_without_powers(app):
    view = _view(app, ChatMessage("kick", "moss", "hello", user_id="7"), powers=("timeout", "ban"))
    view._hover_line(_line_point(view, 0))
    assert view.quick.isVisible() and not view.quick.buttons["delete"].isVisibleTo(view.quick)

    quiet = _view(app, ChatMessage("kick", "moss", "hello", user_id="7"), powers=())
    quiet._hover_line(_line_point(quiet, 0))
    assert not quiet.quick.isVisible()


def test_clicking_a_name_asks_for_the_card_and_keeps_the_chat(app):
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    from sprigcast.app import chat_panel

    view = _view(app, ChatMessage("twitch", "moss", "hello there", id="m1", user_id="7"))
    asked = []
    view.user_asked.connect(lambda m: asked.append(m.user))
    point = None
    for x in range(0, 120, 2):  # find the name's link on the line
        if view.anchorAt(QPoint(x, _line_point(view, 0).y())).startswith(chat_panel.USER_LINK):
            point = QPoint(x, _line_point(view, 0).y())
            break
    assert point is not None
    for kind in (QMouseEvent.Type.MouseButtonPress, QMouseEvent.Type.MouseButtonRelease):
        event = QMouseEvent(kind, QPointF(point), QPointF(view.viewport().mapToGlobal(point)),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(view.viewport(), event)
    assert asked == ["moss"]
    assert "hello there" in view.toPlainText()  # the link wasn't followed


def test_user_card_lists_their_lines_and_bans_only_when_sure(app, monkeypatch):
    from sprigcast.app import chat_user_card

    monkeypatch.setattr(moderation, "account_made", lambda platform, user_id: "2020-01-01T00:00:00Z")
    message = ChatMessage("twitch", "moss", "third", id="m3", user_id="7", badges=("subscriber",))
    lines = [ChatMessage("twitch", "moss", t, user_id="7") for t in ("first", "second")] + [message]
    card = chat_user_card.UserCard(message, lines, ("timeout", "ban"), True)
    done = []
    card.moderated.connect(lambda m, action, seconds: done.append((action, seconds)))
    card.show()
    assert set(card.buttons) == {"reply", "hide", "timeout", "ban"}
    card.buttons["ban"].click()
    assert done == [] and card.buttons["ban"].text().startswith("Sure")
    card.buttons["ban"].click()
    assert done == [("ban", 0)]

    no_powers = chat_user_card.UserCard(ChatMessage("kick", "leaf", "hi", user_id="9"), [], (), False)
    assert set(no_powers.buttons) == {"hide"} and not no_powers.age.isVisibleTo(no_powers)


def test_age_words():
    day = 86400
    now = 1_800_000_000.0
    import datetime

    def iso(seconds_ago):
        return datetime.datetime.fromtimestamp(now - seconds_ago, datetime.timezone.utc).isoformat()

    assert moderation.age_words(iso(3600), now) == "Today"
    assert moderation.age_words(iso(1 * day), now) == "1 day ago"
    assert moderation.age_words(iso(45 * day), now) == "1 month ago"
    assert moderation.age_words(iso(800 * day), now) == "2 years ago"
    assert moderation.age_words("not a time", now) == ""


def test_youtube_delete_finds_the_line_in_the_api_list():
    items = [
        {"id": "LCC.a", "snippet": {"displayMessage": "hi"}, "authorDetails": {"channelId": "UC1"}},
        {"id": "LCC.b", "snippet": {"displayMessage": "spam spam"}, "authorDetails": {"channelId": "UC2"}},
        {"id": "LCC.c", "snippet": {"displayMessage": "spam spam"}, "authorDetails": {"channelId": "UC2"}},
    ]
    assert moderation.youtube_message_id(items, "LCC.a", "UC1", "x") == "LCC.a"  # the same id
    assert moderation.youtube_message_id(items, "Chw-page-id", "UC2", "spam spam") == "LCC.c"  # the newest match
    assert moderation.youtube_message_id(items, "Chw-page-id", "UC3", "spam spam") == ""


def test_youtube_timeout_ban_and_delete_calls(monkeypatch):
    from sprigcast.output import stream_details as sd

    calls = []

    def fake_yt(token, method, path, body=None):
        calls.append((method, path.split("?")[0], body))
        if path.startswith("videos"):
            return {"items": [{"liveStreamingDetails": {"activeLiveChatId": "CHAT1"}}]}
        if method == "GET" and path.startswith("liveChat/messages"):
            return {"items": [{"id": "LCC.z", "snippet": {"displayMessage": "bad words"},
                               "authorDetails": {"channelId": "UCx"}}]}
        return {}

    monkeypatch.setattr(sd, "_with_token", lambda service, call: call("token"))
    monkeypatch.setattr(sd, "_yt", fake_yt)
    moderation._yt_chats.clear()

    moderation.act("youtube", "VIDEO1", "timeout", user_id="UCx", seconds=600)
    assert calls[0][:2] == ("GET", "videos")
    assert calls[1] == ("POST", "liveChat/bans", {"snippet": {
        "liveChatId": "CHAT1", "type": "temporary", "bannedUserDetails": {"channelId": "UCx"},
        "banDurationSeconds": 600}})
    moderation.act("youtube", "VIDEO1", "ban", user_id="UCx")
    assert calls[-1][2]["snippet"]["type"] == "permanent" and "banDurationSeconds" not in calls[-1][2]["snippet"]
    moderation.act("youtube", "VIDEO1", "delete", user_id="UCx", message_id="Chw-page", text="bad words")
    assert calls[-1][:2] == ("DELETE", "liveChat/messages")
    assert sum(1 for c in calls if c[1] == "videos") == 1  # the chat's id is looked up once

    with pytest.raises(moderation.ModerationError):
        moderation.act("youtube", "", "ban", user_id="UCx")  # no stream found yet


# The mod bar (Twitch) ---------------------------------------------------------------------------------------


def test_twitch_chat_modes_shield_and_clear_call_helix(monkeypatch):
    from sprigcast.output import twitch_auth

    tokens = {"access_token": "t", "refresh_token": "r", "expires_at": 9e12, "login": "surakage",
              "user_id": "42", "scope": list(twitch_auth.MOD_SCOPES)}
    monkeypatch.setattr(twitch_auth, "_load", lambda: tokens)
    with pytest.raises(twitch_auth.NeedsReconnect):  # an older sign-in: once more to allow the mod bar
        twitch_auth.set_chat_mode("surakage", "slow_mode", True)
    assert {twitch_auth.SETTINGS_SCOPE, twitch_auth.SHIELD_SCOPE} <= set(twitch_auth.SCOPES.split())
    tokens["scope"] += [twitch_auth.SETTINGS_SCOPE, twitch_auth.SHIELD_SCOPE]
    calls = []

    def send(method, path, body, token, cid):
        calls.append((method, path, body))
        if path.startswith("/chat/settings") and method == "GET":
            return {"data": [{"slow_mode": True, "follower_mode": False, "subscriber_mode": False, "emote_mode": True}]}
        if path.startswith("/moderation/shield_mode") and method == "GET":
            return {"data": [{"is_active": True}]}
        return {}

    monkeypatch.setattr(twitch_auth, "_send", send)
    where = "broadcaster_id=42&moderator_id=42"
    assert twitch_auth.chat_modes("surakage") == {"slow_mode": True, "follower_mode": False,
                                                  "subscriber_mode": False, "emote_mode": True, "shield": True}
    calls.clear()
    twitch_auth.set_chat_mode("surakage", "slow_mode", True)
    twitch_auth.set_chat_mode("surakage", "emote_mode", False)
    twitch_auth.set_chat_mode("surakage", "shield", True)
    twitch_auth.clear_chat("surakage")
    assert calls == [("PATCH", f"/chat/settings?{where}", {"slow_mode": True, "slow_mode_wait_time": 30}),
                     ("PATCH", f"/chat/settings?{where}", {"emote_mode": False}),
                     ("PUT", f"/moderation/shield_mode?{where}", {"is_active": True}),
                     ("DELETE", f"/moderation/chat?{where}", None)]


def test_mod_bar_shows_for_your_twitch_chat_and_switches_modes(app, monkeypatch):
    import time

    from sprigcast.app import chat_panel
    from sprigcast.chat import twitch
    from sprigcast.output import twitch_auth

    monkeypatch.setattr(twitch.TwitchChat, "start", lambda self, ch: setattr(self, "channel", ch))
    monkeypatch.setattr(twitch_auth, "chat_modes", lambda channel: {"slow_mode": True, "shield": False})
    switched = []
    monkeypatch.setattr(twitch_auth, "set_chat_mode", lambda channel, mode, on: switched.append((channel, mode, on)))
    cleared = []
    monkeypatch.setattr(twitch_auth, "clear_chat", lambda channel: cleared.append(channel))
    QSettings().setValue(twitch_auth.LOGIN_SETTING, "surakage")
    panel = chat_panel.ChatPanel()
    panel.show()
    panel.activate()
    try:
        assert panel.mod_toggle.isVisible() and not panel.mod_row.isVisible()
        panel.mod_toggle.setChecked(True)

        def settle(done):
            for _ in range(100):
                app.processEvents()
                if done():
                    return
                time.sleep(0.01)

        settle(lambda: panel.mode_buttons["slow_mode"].isEnabled())
        assert panel.mod_row.isVisible() and panel.mode_buttons["slow_mode"].isChecked()
        assert not panel.mode_buttons["shield"].isChecked()
        panel.mode_buttons["shield"].click()
        settle(lambda: switched and panel.mode_buttons["shield"].isEnabled())
        assert switched == [("surakage", "shield", True)] and panel.mode_buttons["shield"].isChecked()
        panel.clear_button.click()  # asks first
        assert panel.clear_button.text() == "Sure?" and not cleared
        panel.clear_button.click()
        settle(lambda: cleared)
        assert cleared == ["surakage"]
        panel.mod_toggle.setChecked(False)
        assert not panel.mod_row.isVisible()
    finally:
        panel.sleep()


def test_word_filter_leaves_out_words_and_links_but_not_mods():
    from sprigcast.chat import word_filter

    words = word_filter.pattern(word_filter.parse("spam*\nBad Word, cheap"))
    blocks = lambda text, **kw: word_filter.blocks(ChatMessage("twitch", "a", text, **kw), words, True)  # noqa: E731
    assert blocks("total SPAMMER here") and blocks("a bad word") and blocks("so cheap")
    assert not blocks("cheaper") and not blocks("hello friends")
    assert blocks("see https://x.example/free") and blocks("visit freestuff.com")
    assert not blocks("https://x.example", badges=("moderator",))
    assert not word_filter.blocks(ChatMessage("twitch", "a", "spam"), None, False)


def test_hub_applies_the_word_filter_and_panel_logs_what_you_did(app, monkeypatch):
    from sprigcast.app import chat_panel
    from sprigcast.chat import hub as hub_module

    shown = []
    panel = chat_panel.ChatPanel()
    panel.hub.message.connect(lambda m: shown.append(m.text))
    panel.hub.set_word_filter(["spam*"], False, save=True)
    panel.hub._message(ChatMessage("twitch", "a", "spammy stuff"))
    panel.hub._message(ChatMessage("twitch", "b", "hello"))
    assert shown == ["hello"]
    assert hub_module.word_filter.load() == (["spam*"], False)
    panel.mode_set.emit("clear", True, "")
    app.processEvents()
    assert panel.mod_log and panel.mod_log[-1].endswith("Twitch: chat cleared")
    panel.show_mod_log()
    assert "chat cleared" in panel.log_view.toPlainText()
    panel.sleep()


def test_first_time_chatters_get_a_tag():
    from sprigcast.app import chat_panel
    from sprigcast.chat import twitch

    line = ("@badges=;display-name=Newbie;first-msg=1;id=x1;user-id=5 :newbie!newbie@newbie.tmi.twitch.tv "
            "PRIVMSG #surakage :hi all")
    message = twitch.message_from_line(line)
    assert "first" in message.badges
    assert ">&nbsp;First&nbsp;<" in chat_panel.message_html(message)
