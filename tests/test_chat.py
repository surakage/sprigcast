import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.chat import ChatMessage  # noqa: E402
from sprigcast.chat import twitch  # noqa: E402

PRIVMSG = (
    "@badge-info=;badges=broadcaster/1,premium/1;color=#0000FF;display-name=Surakage;emotes=25:0-4,12-16/1902:6-10;"
    "id=b34ccfc7-4977-403a-8a94-33c6bac34fb8;mod=0;room-id=1337;subscriber=0;tmi-sent-ts=1507246572675;turbo=1;"
    "user-id=1337;user-type= :surakage!surakage@surakage.tmi.twitch.tv PRIVMSG #surakage :Kappa Keepo Kappa"
)


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("ChatTest")
    QSettings().clear()
    yield app
    from sprigcast.app import chat_panel

    for widget in app.allWidgets():  # a panel left reading would answer later tests' sign-ins
        if isinstance(widget, chat_panel.ChatPanel):
            widget.sleep()
    QSettings().clear()


def test_parse_a_twitch_chat_line():
    message = twitch.message_from_line(PRIVMSG)
    assert message.user == "Surakage" and message.text == "Kappa Keepo Kappa"
    assert message.color == "#0000FF" and message.badges == ("broadcaster", "premium")
    assert message.emotes == [(0, 4, "25"), (6, 10, "1902"), (12, 16, "25")]
    assert message.id.startswith("b34ccfc7") and message.user_id == "1337"


def test_me_actions_escaped_tags_and_sub_notices():
    action = twitch.message_from_line(
        "@display-name=fern;color=;emotes= :fern!fern@fern.tmi.twitch.tv PRIVMSG #x :\x01ACTION waves\x01")
    assert action.action and action.text == "waves" and action.user == "fern"
    sub = twitch.message_from_line(
        r"@display-name=moss;msg-id=sub;system-msg=moss\ssubscribed\sat\sTier\s1. :tmi.twitch.tv USERNOTICE #x")
    assert sub.notice and sub.text == "moss subscribed at Tier 1."
    assert twitch.message_from_line(":tmi.twitch.tv 001 justinfan1 :Welcome, GLHF!") is None
    assert twitch.message_from_line("PING :tmi.twitch.tv") is None


def test_channel_from_links_and_names():
    assert twitch.channel_from_text("https://www.twitch.tv/popout/Surakage/chat?popout=") == "surakage"
    assert twitch.channel_from_text("twitch.tv/surakage") == "surakage"
    assert twitch.channel_from_text("#Surakage") == "surakage"
    assert twitch.channel_from_text("http://127.0.0.1:8787/overlay/chat/dock") is None
    assert twitch.channel_from_text("https://dashboard.twitch.tv/popout/u/x/stream-manager/activity-feed") is None


class FakeSocket:
    def __init__(self):
        self.sent = []

    def sendTextMessage(self, line):  # noqa: N802
        self.sent.append(line)


def test_reader_answers_pings_and_reports_deletes(app):
    reader = twitch.TwitchChat()
    reader.socket = FakeSocket()
    got, deleted, cleared = [], [], []
    reader.message.connect(got.append)
    reader.deleted.connect(deleted.append)
    reader.cleared.connect(cleared.append)
    reader._on_text("PING :tmi.twitch.tv\r\n" + PRIVMSG + "\r\n")
    assert reader.socket.sent == ["PONG :tmi.twitch.tv"]
    assert got[0].text == "Kappa Keepo Kappa"
    reader.handle_line("@login=x;target-msg-id=abc :tmi.twitch.tv CLEARMSG #x :bad words")
    reader.handle_line("@target-user-id=42 :tmi.twitch.tv CLEARCHAT #x :someone")
    assert deleted == ["abc"] and cleared == ["42"]
    reader.socket = None


def test_message_html_escapes_and_lifts_dark_names():
    from sprigcast.app import chat_panel

    html = chat_panel.message_html(ChatMessage("twitch", "<b>x</b>", "hi <script> https://sprig.cast/a"))
    assert "<script>" not in html and "&lt;b&gt;" in html and "href='https://sprig.cast/a'" in html
    assert chat_panel.readable_colour("#0000FF", "x") != "#0000ff"  # too dark on the panel: lifted
    assert chat_panel.readable_colour("", "fern") == chat_panel.readable_colour("", "FERN")
    emote_html = chat_panel.message_html(twitch.message_from_line(PRIVMSG))
    assert emote_html.count("<img src='emote:25'") == 2 and "Keepo" not in emote_html.split("title='Keepo'")[0][-5:]


def test_panel_switches_between_link_twitch_and_account(app, monkeypatch):
    from sprigcast.app import chat_panel
    from sprigcast.chat.youtube import YouTubeChat
    from sprigcast.output import twitch_auth

    started = []
    for cls in (twitch.TwitchChat, YouTubeChat):
        monkeypatch.setattr(cls, "start", lambda self, ch: (started.append((self.platform, ch)),
                                                            setattr(self, "channel", ch)))
    monkeypatch.setattr(chat_panel.ChatPanel, "_show_link",
                        lambda self, url: self.pages.setCurrentWidget(self.web_holder))
    QSettings().setValue(twitch_auth.LOGIN_SETTING, "")
    panel = chat_panel.ChatPanel()
    panel.activate()
    assert panel.pages.currentWidget() is panel.empty

    panel.ask_link()  # a custom link fills this same panel
    panel.link_field.setText("http://127.0.0.1:8787/overlay/chat/dock")
    panel._use_link()
    assert panel.source() == ("link", "http://127.0.0.1:8787/overlay/chat/dock")
    assert panel.pages.currentWidget() is panel.web_holder

    twitch_auth._announce("Surakage")  # connecting Twitch switches chat to your channel
    assert panel.source() == ("chat", {"twitch": "surakage"}) and started[-1] == ("twitch", "surakage")
    assert panel.pages.currentWidget() is panel.view and not panel.view.mixed

    panel.use_link()  # the saved link is one click away
    assert panel.source()[0] == "link"

    panel.ask_names()  # just names: YouTube joins the same chat
    assert panel.name_fields["twitch"].placeholderText() == "surakage (connected)"
    panel.name_fields["youtube"].setText("youtube.com/@SlothBloom")
    panel._use_names()
    assert panel.source() == ("chat", {"twitch": "surakage", "youtube": "@SlothBloom"})
    assert set(panel.readers) == {"twitch", "youtube"} and panel.view.mixed
    assert "Twitch" in panel.where.text() and "YouTube" in panel.where.text()

    panel.ask_link()  # a pasted YouTube video link reads that stream's chat
    panel.link_field.setText("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    panel._use_link()
    assert panel.source()[1]["youtube"] == "v:dQw4w9WgXcQ"

    panel.ask_names()
    panel.name_fields["youtube"].setText("")
    panel._use_names()
    assert set(panel.readers) == {"twitch"} and not panel.view.mixed  # the others stop

    twitch_auth._announce("")  # disconnecting with no names left falls back to the link
    assert panel.source()[0] == "link"
    texts = [a.text() for a in panel.build_menu().actions() if a.text()]
    assert "Sign in…" in texts and "Channels…" in texts


def test_mixed_lines_carry_a_platform_mark_and_mods_clear_per_platform(app):
    from sprigcast.app import chat_panel

    view = chat_panel.ChatView()
    view.mixed = True
    view.add(ChatMessage("twitch", "a", "hi", user_id="1", id="t1"))
    view.add(ChatMessage("youtube", "b", "yo", user_id="1", id="y1"))
    assert "▍" in chat_panel.message_html(view.messages[0], True)
    view.remove(user_id="1", platform="youtube")
    assert [m.platform for m in view.messages] == ["twitch"]


def test_old_twitch_channel_setting_carries_over(app):
    from sprigcast.app import chat_panel

    QSettings().setValue(chat_panel.OLD_CHANNEL, "mossfriend")
    assert chat_panel.saved_names() == {"twitch": "mossfriend"}


def test_mentions_stand_out_and_scrolling_up_shows_a_jump_pill(app):
    from sprigcast.app import chat_panel

    assert chat_panel.mentions("hey @Surakage, nice!", frozenset({"surakage"}))
    assert chat_panel.mentions("surakage.", frozenset({"surakage"}))
    assert not chat_panel.mentions("surakage2 hi", frozenset({"surakage"}))
    html = chat_panel.message_html(ChatMessage("twitch", "a", "hi surakage"), names=frozenset({"surakage"}))
    assert chat_panel.theme.SELECT_TEXT in html

    view = chat_panel.ChatView()
    view.resize(300, 200)
    view.show()
    for i in range(60):
        view.add(ChatMessage("twitch", "a", f"line {i}"))
    assert view.document().isEmpty()  # drawn together, a moment later
    view.flush()
    app.processEvents()
    assert not view.jump.isVisible()  # following along: always at the newest line
    view.verticalScrollBar().setValue(0)  # scrolled up to read
    view.add(ChatMessage("twitch", "a", "new one"))
    view.flush()
    assert view.jump.isVisible() and view.verticalScrollBar().value() == 0
    view.jump.click()
    assert not view.jump.isVisible() and view.at_bottom()

    view.names = frozenset({"surakage"})  # a line that mentions you is washed green, the next one isn't
    view.add(ChatMessage("twitch", "a", "@surakage hi"))
    view.add(ChatMessage("twitch", "a", "plain"))
    view.flush()
    last = view.document().lastBlock()
    assert last.previous().blockFormat().background().color().name() == chat_panel.theme.SELECT_BG
    assert not last.blockFormat().hasProperty(last.blockFormat().Property.BackgroundBrush)


def test_emote_cache_starts_over_instead_of_growing(app, monkeypatch):
    """The page starts over once it has been handed EMOTE_CACHE pictures, and takes the ones still on
    screen from memory, so a start-over downloads nothing (it used to re-request every emote: a 2 s freeze)."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QImage

    from sprigcast.app import chat_panel

    monkeypatch.setattr(chat_panel, "EMOTE_CACHE", 3)
    fetched = []
    monkeypatch.setattr(chat_panel.ChatView, "_next_fetch", lambda self: fetched.extend(self._queue) or self._queue.clear())
    view = chat_panel.ChatView()
    view.resize(300, 300)
    view.show()
    picture = QImage(22, 22, QImage.Format.Format_ARGB32)
    picture.fill(0xFF00FF00)
    view._pictures["yt-1"] = picture
    view._given = 4  # already past the limit
    view.add(ChatMessage("youtube", "a", "hi ▢", emotes=[(3, 3, "yt-1")], images={"yt-1": "https://yt3.ggpht.com/emoji-1"}))
    view.flush()
    app.processEvents()
    assert view._given <= 1 and view._urls == {"yt-1": "https://yt3.ggpht.com/emoji-1"}
    assert fetched == [] and [m.text for m in view.messages] == ["hi ▢"]
    for i in range(10):  # new pictures wait their turn instead of all being requested at once
        view._fetch(QUrl(f"emote:e{i}"))
    assert len(fetched) == 10 and view._asked >= {f"e{i}" for i in range(10)}


def test_emote_downloads_are_queued_a_few_at_a_time(app, monkeypatch):
    from PySide6.QtCore import QUrl

    from sprigcast.app import chat_panel

    view = chat_panel.ChatView()
    asked = []

    class Net:
        def get(self, request):
            asked.append(request.url().toString())

            class Reply:
                class finished:  # noqa: N801
                    @staticmethod
                    def connect(fn):
                        pass
            return Reply()

    view._net = Net()
    for i in range(20):
        view._fetch(QUrl(f"emote:e{i}"))
    assert len(asked) == chat_panel.EMOTE_FETCHES and len(view._queue) == 20 - chat_panel.EMOTE_FETCHES


def test_panel_sleeps_when_taken_off_every_screen(app, monkeypatch):
    from sprigcast.app import chat_panel
    from sprigcast.output import twitch_auth

    stopped = []
    monkeypatch.setattr(twitch.TwitchChat, "start", lambda self, ch: setattr(self, "channel", ch))
    monkeypatch.setattr(twitch.TwitchChat, "stop", lambda self: stopped.append(self.channel))
    QSettings().setValue(twitch_auth.LOGIN_SETTING, "surakage")
    panel = chat_panel.ChatPanel()
    panel.activate()
    panel.view.add(ChatMessage("twitch", "a", "kept"))
    assert set(panel.readers) == {"twitch"} and panel.view.names == frozenset({"surakage"})
    panel.sleep()
    assert stopped == ["surakage"] and not panel.readers and not panel.active
    panel.activate()  # shown again: reads again, earlier lines still there
    assert set(panel.readers) == {"twitch"} and [m.text for m in panel.view.messages] == ["kept"]


def test_chat_back_on_twitch(app, monkeypatch):
    from sprigcast.app import chat_panel
    from sprigcast.output import twitch_auth

    monkeypatch.setattr(twitch.TwitchChat, "start", lambda self, ch: setattr(self, "channel", ch))
    sent = []
    monkeypatch.setattr(twitch_auth, "can_send_chat", lambda: True)
    monkeypatch.setattr(twitch_auth, "send_chat", lambda channel, text: sent.append((channel, text)))
    QSettings().setValue(twitch_auth.LOGIN_SETTING, "")
    connects = []
    panel = chat_panel.ChatPanel(lambda: connects.append(1))
    panel.activate()
    assert panel.say_row.isHidden()  # no chat yet: nothing to answer
    twitch_auth._announce("surakage")
    assert not panel.say_row.isHidden() and "surakage" in panel.say.toolTip()
    panel.say.setText("hi chat")
    panel.send()
    for _ in range(50):
        app.processEvents()
        if sent and panel.say.isEnabled():
            break
        import time
        time.sleep(0.01)
    app.processEvents()
    assert sent == [("surakage", "hi chat")] and panel.say.text() == ""

    monkeypatch.setattr(twitch_auth, "can_send_chat", lambda: False)  # connected before sending was asked for
    panel.say.setText("again")
    panel.send()
    assert connects == [1] and len(sent) == 1


def test_send_chat_needs_the_scope_and_finds_the_channel(monkeypatch):
    from sprigcast.output import twitch_auth

    tokens = {"access_token": "t", "refresh_token": "r", "expires_at": 9e12, "login": "surakage",
              "user_id": "42", "scope": ["channel:read:stream_key"]}
    monkeypatch.setattr(twitch_auth, "_load", lambda: tokens)
    with pytest.raises(twitch_auth.TwitchAuthError, match="Reconnect"):
        twitch_auth.send_chat("surakage", "hi")
    tokens["scope"].append(twitch_auth.CHAT_SCOPE)
    posted = []
    monkeypatch.setattr(twitch_auth, "_get", lambda path, token, cid: {"data": [{"id": "99"}]})
    monkeypatch.setattr(twitch_auth, "_post_json",
                        lambda path, body, token, cid: (posted.append(body), {"data": [{"is_sent": True}]})[1])
    twitch_auth.send_chat("surakage", "hi")
    twitch_auth.send_chat("#MossFriend", "yo")
    assert [p["broadcaster_id"] for p in posted] == ["42", "99"] and posted[0]["sender_id"] == "42"
    monkeypatch.setattr(twitch_auth, "_post_json", lambda *a: {"data": [{"is_sent": False,
                                                                         "drop_reason": {"message": "Slow mode"}}]})
    with pytest.raises(twitch_auth.TwitchAuthError, match="Slow mode"):
        twitch_auth.send_chat("surakage", "hi")


def test_a_dropped_chat_says_it_is_reconnecting(app, monkeypatch):
    from sprigcast.app import chat_panel
    from sprigcast.output import twitch_auth

    monkeypatch.setattr(twitch.TwitchChat, "start", lambda self, ch: setattr(self, "channel", ch))
    monkeypatch.setattr(twitch.TwitchChat, "stop", lambda self: None)
    QSettings().setValue(twitch_auth.LOGIN_SETTING, "surakage")
    panel = chat_panel.ChatPanel()
    panel.activate()
    panel.view.resize(300, 400)
    panel.hub._state("twitch", "connecting")
    assert panel.view.notice.isHidden()  # first connect: nothing has dropped
    panel.hub._state("twitch", "live")
    panel.hub._state("twitch", "offline: network blip")
    assert not panel.view.notice.isHidden() and "Twitch chat reconnecting" in panel.view.notice.text()
    assert "reconnecting" in panel.where.toolTip()
    panel.hub._state("twitch", "connecting")
    assert not panel.view.notice.isHidden()
    panel.hub._state("twitch", "live")
    assert panel.view.notice.isHidden()
    panel.hub._state("twitch", "waiting: stream ended")  # ended, not dropped
    assert panel.view.notice.isHidden()


def test_a_busy_chat_is_drawn_in_batches_and_clears_only_remove_their_lines(app):
    from sprigcast.app import chat_panel

    view = chat_panel.ChatView()
    view.resize(300, 400)
    for i in range(500):  # more than KEEP arrive between two draws
        view.add(ChatMessage("twitch", "a", f"line {i}", id=f"t{i}", user_id=str(i % 7)))
    assert view._flush.isActive() and len(view._pending) == chat_panel.KEEP
    view.flush()
    assert view.document().blockCount() == len(view.messages) == chat_panel.KEEP
    assert view.document().lastBlock().text().endswith("line 499")
    view.remove(platform="twitch", user_id="3")  # a timeout: that user's lines go, the rest stay put
    texts = [view.document().findBlockByNumber(i).text() for i in range(view.document().blockCount())]
    assert len(texts) == len(view.messages) and all(t.endswith(m.text) for t, m in zip(texts, view.messages))
    assert all(m.user_id != "3" for m in view.messages)
    last = view.messages[-1].id
    view.remove(platform="twitch", message_id=last)  # the newest line deleted
    assert view.document().blockCount() == len(view.messages)
    assert view.document().lastBlock().text().endswith(view.messages[-1].text)
    view.add(ChatMessage("twitch", "a", "after"))
    view.remove(platform="twitch", message_id="nope")  # a delete waits for nothing: the batch is drawn first
    assert view.document().lastBlock().text().endswith("after")


def test_signing_in_to_youtube_or_kick_starts_its_chat(app, monkeypatch):
    """A sign-in names the chat (service_signin._name_chat) and the open panel starts reading it at once."""
    from sprigcast.app import chat_panel
    from sprigcast.chat import kick
    from sprigcast.output import service_signin, stream_details

    started = []
    monkeypatch.setattr(kick.KickChat, "start", lambda self, name: (started.append(name), setattr(self, "channel", name)))
    monkeypatch.setattr(kick.KickChat, "stop", lambda self: None)
    QSettings().setValue(chat_panel.SOURCE, "link")
    QSettings().setValue(chat_panel.LINK, "http://127.0.0.1:8787/chat")
    panel = chat_panel.ChatPanel()
    monkeypatch.setattr(panel, "_show_link", lambda url: None)
    panel.activate()
    assert not panel.readers
    QSettings().setValue(stream_details.ACCOUNT_SETTING.format("kick"), "slothbloom")  # what a Kick sign-in does:
    service_signin._name_chat("kick", "slothbloom")  # it remembers the account, then names the chat
    assert set(started) == {"slothbloom"} and set(panel.readers) == {"kick"} and panel.source()[0] == "chat"


def test_an_older_sign_in_names_its_chat_once(app, monkeypatch):
    from sprigcast.app import chat_panel
    from sprigcast.output import service_signin, stream_details

    QSettings().setValue(stream_details.ACCOUNT_SETTING.format("kick"), "slothbloom")
    QSettings().setValue(stream_details.ACCOUNT_SETTING.format("youtube"), "Sloth Bloom")
    monkeypatch.setattr(stream_details, "access_token", lambda service: "tok")
    monkeypatch.setattr(service_signin, "_youtube_channel", lambda token: {"title": "Sloth Bloom", "customUrl": "@slothbloom"})
    assert service_signin.name_chat_from_sign_in("kick") and service_signin.name_chat_from_sign_in("youtube")
    assert chat_panel.saved_names() == {"kick": "slothbloom", "youtube": "@slothbloom"}
    chat_panel.save_names({})  # you cleared them: they aren't put back
    assert not service_signin.name_chat_from_sign_in("kick") and not service_signin.name_chat_from_sign_in("youtube")
    assert chat_panel.saved_names() == {}


def test_hidden_accounts_never_show_and_a_line_can_hide_its_sender(app, monkeypatch):
    from PySide6.QtCore import QPoint

    from sprigcast.app import chat_panel
    from sprigcast.chat import hidden
    from sprigcast.chat.feed import FEED
    from sprigcast.output import twitch_auth

    assert hidden.parse("Nightbot, @StreamElements\nnightbot  a b!") == ["nightbot", "streamelements", "a"]
    monkeypatch.setattr(twitch.TwitchChat, "start", lambda self, ch: setattr(self, "channel", ch))
    monkeypatch.setattr(twitch.TwitchChat, "stop", lambda self: None)
    QSettings().setValue(twitch_auth.LOGIN_SETTING, "surakage")
    FEED.clear()
    panel = chat_panel.ChatPanel()
    panel.resize(320, 500)
    panel.show()
    panel.activate()
    for user, text in (("Nightbot", "!commands"), ("fern", "hello"), ("oak", "hi fern")):
        panel.hub._message(ChatMessage("twitch", user, text))
    panel.view.flush()
    app.processEvents()
    assert [m.user for m in panel.view.messages] == ["Nightbot", "fern", "oak"]

    # Right-click the first line: Reply (Twitch, signed in), Hide, Copy
    first = panel.view.document().firstBlock()
    rect = panel.view.document().documentLayout().blockBoundingRect(first)
    message = panel.view.message_at(QPoint(20, int(rect.center().y())))
    assert message is not None and message.user == "Nightbot"
    menu = panel.view.line_menu(message)
    labels = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert labels == ["Reply to Nightbot", "Hide Nightbot", "Copy message"]
    next(a for a in menu.actions() if a.text() == "Reply to Nightbot").trigger()
    assert panel.say.text() == "@Nightbot "
    next(a for a in menu.actions() if a.text() == "Hide Nightbot").trigger()
    assert [m.user for m in panel.view.messages] == ["fern", "oak"]  # gone from the panel...
    assert [line.message.user for line in FEED.snapshot()[1]] == ["fern", "oak"]  # ...and from chat on your stream
    assert hidden.load() == ["nightbot"]
    panel.hub._message(ChatMessage("kick", "NIGHTBOT", "again"))  # any platform, any capitals
    panel.hub._message(ChatMessage("twitch", "", "Nightbot subscribed", notice=True))  # events still show
    panel.view.flush()
    assert [m.text for m in panel.view.messages] == ["hello", "hi fern", "Nightbot subscribed"]

    # The list in the ⋯ menu: edit it, add the usual bots, take one back out
    assert "Hidden accounts (1)…" in [a.text() for a in panel.build_menu().actions()]
    panel.ask_hidden()
    assert panel.hidden_field.toPlainText() == "nightbot"
    panel._add_common_bots()
    assert "streamelements" in panel.hidden_field.toPlainText().split("\n")
    panel.hidden_field.setPlainText("oak")
    panel._use_hidden()
    assert hidden.load() == ["oak"] and [m.user for m in panel.view.messages] == ["fern", ""]
    panel.hub._message(ChatMessage("twitch", "Nightbot", "back"))
    panel.view.flush()
    assert panel.view.messages[-1].user == "Nightbot"
    panel.sleep()
