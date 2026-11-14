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
    from sprigcast.chat.kick import KickChat
    from sprigcast.chat.youtube import YouTubeChat
    from sprigcast.output import twitch_auth

    started = []
    for cls in (twitch.TwitchChat, YouTubeChat, KickChat):
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

    panel.ask_names()  # just names: YouTube and Kick join the same chat
    assert panel.name_fields["twitch"].placeholderText() == "surakage (connected)"
    panel.name_fields["youtube"].setText("youtube.com/@SlothBloom")
    panel.name_fields["kick"].setText("kick.com/slothbloom")
    panel._use_names()
    assert panel.source() == ("chat", {"twitch": "surakage", "youtube": "@SlothBloom", "kick": "slothbloom"})
    assert set(panel.readers) == {"twitch", "youtube", "kick"} and panel.view.mixed
    assert "Twitch" in panel.where.text() and "Kick" in panel.where.text()

    panel.ask_link()  # a pasted YouTube video link reads that stream's chat
    panel.link_field.setText("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    panel._use_link()
    assert panel.source()[1]["youtube"] == "v:dQw4w9WgXcQ"

    panel.ask_names()
    panel.name_fields["youtube"].setText("")
    panel.name_fields["kick"].setText("")
    panel._use_names()
    assert set(panel.readers) == {"twitch"} and not panel.view.mixed  # the others stop

    twitch_auth._announce("")  # disconnecting with no names left falls back to the link
    assert panel.source()[0] == "link"
    texts = [a.text() for a in panel.build_menu().actions() if a.text()]
    assert "Connect Twitch…" in texts and "Channels…" in texts


def test_mixed_lines_carry_a_platform_mark_and_mods_clear_per_platform(app):
    from sprigcast.app import chat_panel

    view = chat_panel.ChatView()
    view.mixed = True
    view.add(ChatMessage("twitch", "a", "hi", user_id="1", id="t1"))
    view.add(ChatMessage("kick", "b", "yo", user_id="1", id="k1"))
    assert "▍" in chat_panel.message_html(view.messages[0], True)
    view.remove(user_id="1", platform="kick")
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
    app.processEvents()
    assert not view.jump.isVisible()  # following along: always at the newest line
    view.verticalScrollBar().setValue(0)  # scrolled up to read
    view.add(ChatMessage("twitch", "a", "new one"))
    assert view.jump.isVisible() and view.verticalScrollBar().value() == 0
    view.jump.click()
    assert not view.jump.isVisible() and view.at_bottom()

    view.names = frozenset({"surakage"})  # a line that mentions you is washed green, the next one isn't
    view.add(ChatMessage("twitch", "a", "@surakage hi"))
    view.add(ChatMessage("twitch", "a", "plain"))
    last = view.document().lastBlock()
    assert last.previous().blockFormat().background().color().name() == chat_panel.theme.SELECT_BG
    assert not last.blockFormat().hasProperty(last.blockFormat().Property.BackgroundBrush)


def test_emote_cache_starts_over_instead_of_growing(app, monkeypatch):
    from sprigcast.app import chat_panel

    monkeypatch.setattr(chat_panel, "EMOTE_CACHE", 3)
    view = chat_panel.ChatView()
    view._asked.update({"a", "b", "c", "d"})
    view.add(ChatMessage("kick", "a", "hi", images={"kick-1": "https://files.kick.com/emotes/1/fullsize"}))
    assert not view._asked and view._urls == {"kick-1": "https://files.kick.com/emotes/1/fullsize"}
    assert [m.text for m in view.messages] == ["hi"]


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
