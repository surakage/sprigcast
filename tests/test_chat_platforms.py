"""YouTube chat reader: the parts that turn its public chat into ChatMessages."""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtCore")

from sprigcast.chat import name_for, youtube  # noqa: E402


def test_youtube_handles_and_links():
    assert youtube.handle_from_text("@SlothBloom") == "@SlothBloom"
    assert youtube.handle_from_text("SlothBloom") == "@SlothBloom"
    assert youtube.handle_from_text("https://www.youtube.com/@SlothBloom/streams") == "@SlothBloom"
    assert youtube.handle_from_text("youtube.com/channel/UCabcdefghijklmnopqrstuv") == "channel/UCabcdefghijklmnopqrstuv"
    assert youtube.handle_from_text("https://youtu.be/dQw4w9WgXcQ") == "v:dQw4w9WgXcQ"
    assert youtube.handle_from_text("https://www.youtube.com/live/dQw4w9WgXcQ?si=x") == "v:dQw4w9WgXcQ"
    assert youtube.handle_from_text("not a name at all!") is None
    assert name_for("youtube", "@x_y") == "@x_y"


def test_youtube_live_page_and_chat_page():
    live = '<html><link rel="canonical" href="https://www.youtube.com/watch?v=dQw4w9WgXcQ"></html>'
    assert youtube.video_from_live_page(live) == "dQw4w9WgXcQ"
    offline = '<html><link rel="canonical" href="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"></html>'
    assert youtube.video_from_live_page(offline) is None
    # Late-2026 /live pages: canonical says "undefined", the stream is the page's current video
    current = ('<link rel="canonical" href="undefined"><script>var ytInitialData = {"currentVideoEndpoint":'
               '{"clickTrackingParams":"x","watchEndpoint":{"videoId":"4t4AQ15yj1M"}}};</script>')
    assert youtube.video_from_live_page(current) == "4t4AQ15yj1M"
    data = {"contents": {"liveChatRenderer": {
        "continuations": [{"invalidationContinuationData": {"continuation": "TOP", "timeoutMs": 10000}}],
        "header": {"liveChatHeaderRenderer": {"viewSelector": {"sortFilterSubMenuRenderer": {"subMenuItems": [
            {"title": "Top chat", "continuation": {"reloadContinuationData": {"continuation": "TOP"}}},
            {"title": "Live chat", "continuation": {"reloadContinuationData": {"continuation": "ALL"}}},
        ]}}}},
        "actions": [{"addChatItemAction": {"item": {}}}],
    }}}
    page = ('<script>ytcfg.set({"INNERTUBE_API_KEY":"AIzaKEY","INNERTUBE_CLIENT_VERSION":"2.20251110.01.00"});'
            f'</script><script>window["ytInitialData"] = {json.dumps(data)};</script>')
    key, version, continuation, actions = youtube.chat_setup(page)
    assert (key, version, continuation, len(actions)) == ("AIzaKEY", "2.20251110.01.00", "ALL", 1)


def test_youtube_messages_super_chats_and_deletes():
    item = {"liveChatTextMessageRenderer": {
        "id": "m1", "authorName": {"simpleText": "@Fern"}, "authorExternalChannelId": "UCfern",
        "message": {"runs": [{"text": "hi "}, {"emoji": {"emojiId": "😀"}}, {"text": " "},
                             {"emoji": {"emojiId": "UCx/abc", "isCustomEmoji": True, "shortcuts": [":leaf:"],
                                        "image": {"thumbnails": [{"url": "https://yt3.ggpht.com/leaf"}]}}}]},
        "authorBadges": [{"liveChatAuthorBadgeRenderer": {"icon": {"iconType": "MODERATOR"}}}],
    }}
    message = youtube.message_from_item(item)
    assert message.user == "Fern" and message.text == "hi 😀 :leaf:" and message.badges == ("moderator",)
    assert message.emotes == [(5, 10, "yt-UCxabc")] and message.images["yt-UCxabc"] == "https://yt3.ggpht.com/leaf"
    paid = youtube.message_from_item({"liveChatPaidMessageRenderer": {
        "id": "p1", "authorName": {"simpleText": "Moss"}, "purchaseAmountText": {"simpleText": "$5.00"},
        "message": {"runs": [{"text": "love it"}]}}})
    assert paid.notice and paid.text == "Moss sent $5.00: love it"
    assert youtube.message_from_item({"liveChatViewerEngagementMessageRenderer": {}}) is None

    reply = {"continuationContents": {"liveChatContinuation": {
        "continuations": [{"timedContinuationData": {"continuation": "NEXT", "timeoutMs": 60000}}],
        "actions": [{"addChatItemAction": {"item": item}},
                    {"markChatItemAsDeletedAction": {"targetItemId": "m1"}}]}}}
    actions, continuation, wait = youtube.parse_chat_response(reply)
    assert continuation == "NEXT" and wait == youtube.POLL_MAX and len(actions) == 2
    assert youtube.parse_chat_response({})[1] is None  # stream over

    from PySide6.QtCore import QCoreApplication

    QCoreApplication.instance() or QCoreApplication([])
    reader = youtube.YouTubeChat()
    got, deleted = [], []
    reader.message.connect(got.append)
    reader.deleted.connect(deleted.append)
    reader._handle(actions + actions[:1])  # the same message twice shows once
    assert [m.id for m in got] == ["m1"] and deleted == ["m1"]


def test_tiktok_chat_is_gone_until_it_is_built_properly():
    """TikTok chat was unofficial and suspected in an idle crash (2026-11-16): removed until built anew."""
    from sprigcast.chat import PLATFORMS, name_for

    assert "tiktok" not in PLATFORMS and name_for("tiktok", "@moss") is None


def test_kick_names_events_and_emotes():
    from sprigcast.chat import PLATFORMS, kick, name_for

    assert "kick" in PLATFORMS
    assert [name_for("kick", t) for t in ("kick.com/Moss", "https://kick.com/popout/moss/chat", "@moss", "a b")] == \
        ["moss", "moss", "moss", None]
    line = kick.message_from_event("chat.message.sent", {
        "message_id": "m1", "content": "hi [emote:37226:KEKW]",
        "sender": {"user_id": 9, "username": "fern", "identity": {"username_color": "#1475E1",
                                                                    "badges": [{"text": "Moderator", "type": "moderator"}]}}})
    assert (line.user, line.text, line.color, line.badges, line.id) == ("fern", "hi KEKW", "#1475E1", ("moderator",), "m1")
    assert line.emotes == [(3, 6, "kick-37226")] and line.images["kick-37226"].endswith("/37226/fullsize")
    gifts = kick.message_from_event("channel.subscription.gifts", {"gifter": {"username": "oak"}, "giftees": [{}, {}]})
    assert gifts.notice and gifts.text == "oak gifted 2 subs"
    assert kick.message_from_event("livestream.status.updated", {}) is None


def test_kick_reader_is_quiet_about_failures_and_slow_to_retry(monkeypatch):
    import json

    from PySide6.QtCore import QCoreApplication

    from sprigcast.chat import kick

    QCoreApplication.instance() or QCoreApplication([])
    monkeypatch.setattr(kick.KickChat, "_open", lambda self: None)  # no network in tests
    reader = kick.KickChat()
    got, cleared, states = [], [], []
    reader.message.connect(got.append)
    reader.cleared.connect(cleared.append)
    reader.status.connect(states.append)
    reader.start("moss")
    reader.handle_text(json.dumps({"type": "ready", "user_id": 77, "slug": "moss"}))
    line = {"type": "event", "event": "chat.message.sent", "data": {"message_id": "a", "content": "yo",
                                                                     "sender": {"username": "fern"}}}
    reader.handle_text(json.dumps(line))
    reader.handle_text(json.dumps(line))  # the same line twice: shown once
    reader.handle_text(json.dumps({"type": "event", "event": "moderation.banned",
                                   "data": {"banned_user": {"user_id": 12}}}))
    reader.handle_text("pong")
    assert [m.text for m in got] == ["yo"] and cleared == ["12"] and states == ["live"]
    reader.handle_text(json.dumps({"type": "error", "why": "no Kick channel by that name"}))
    assert states[-1] == "offline: no Kick channel by that name"
    assert reader._retry.interval() == kick.NOT_FOUND_RETRY * 1000  # a typo isn't retried every few seconds
    reader._retry.stop()
    reader._failed("Kick chat isn't reachable right now")
    first = reader._retry.interval()
    reader._retry.stop()
    reader._failed("Kick chat isn't reachable right now")
    assert reader._retry.interval() > first  # backs off
    reader.stop()
    assert not reader._retry.isActive() and not reader._ping.isActive()


def test_kick_relay_address_can_be_pointed_elsewhere(monkeypatch):
    from sprigcast.chat import kick

    monkeypatch.delenv("SPRIGCAST_KICK_CHAT_URL", raising=False)
    assert kick.relay_url("moss").startswith(kick.RELAY) and kick.relay_url("moss").endswith("?channel=moss")
    monkeypatch.setenv("SPRIGCAST_KICK_CHAT_URL", "ws://127.0.0.1:8787/v1/chat")
    assert kick.relay_url("moss") == "ws://127.0.0.1:8787/v1/chat?channel=moss"


def test_kick_reader_over_a_real_socket(monkeypatch):
    """The reader against a stand-in relay on this PC: connect, 'ready', a chat line, then the relay goes away."""
    import json

    from PySide6.QtCore import QCoreApplication, QElapsedTimer
    from PySide6.QtNetwork import QHostAddress
    from PySide6.QtWebSockets import QWebSocketServer

    from sprigcast.chat import kick

    app = QCoreApplication.instance() or QCoreApplication([])
    server = QWebSocketServer("relay", QWebSocketServer.SslMode.NonSecureMode)
    assert server.listen(QHostAddress.SpecialAddress.LocalHost, 0)
    asked, clients = [], []

    def joined():
        socket = server.nextPendingConnection()
        clients.append(socket)
        asked.append(socket.requestUrl().query())
        socket.sendTextMessage(json.dumps({"type": "ready", "user_id": 1, "slug": "moss"}))
        socket.sendTextMessage(json.dumps({"type": "event", "event": "chat.message.sent",
                                           "data": {"message_id": "z", "content": "hello", "sender": {"username": "oak"}}}))

    server.newConnection.connect(joined)
    monkeypatch.setenv("SPRIGCAST_KICK_CHAT_URL", f"ws://127.0.0.1:{server.serverPort()}/v1/chat")
    reader = kick.KickChat()
    got, states = [], []
    reader.message.connect(got.append)
    reader.status.connect(states.append)
    reader.start("moss")

    def wait(done, ms=5000):
        clock = QElapsedTimer()
        clock.start()
        while not done() and clock.elapsed() < ms:
            app.processEvents()

    wait(lambda: got)
    assert asked == ["channel=moss"] and [m.text for m in got] == ["hello"] and "live" in states
    clients[0].close()
    wait(lambda: states[-1].startswith("offline"))
    assert states[-1].startswith("offline: disconnected") and reader._retry.isActive()
    assert reader._retry.interval() == kick.QUICK_RETRY * 1000  # a link that was working: back quickly
    reader.stop()
    server.close()
