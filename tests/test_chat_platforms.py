"""YouTube and Kick chat readers: the parts that turn each platform's public chat into ChatMessages."""

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtCore")

from sprigcast.chat import kick, name_for, youtube  # noqa: E402


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


def test_kick_names_emotes_and_events():
    assert kick.slug_from_text("https://kick.com/SlothBloom") == "slothbloom"
    assert kick.slug_from_text("@moss_fern") == "moss_fern"
    assert kick.slug_from_text("bad name!") is None
    assert kick.chatroom_from_channel({"chatroom": {"id": 668}}) == 668
    data = {"id": "abc", "content": "gg [emote:37226:KEKW]!", "sender": {
        "id": 9, "username": "Moss", "identity": {"color": "#E9113C", "badges": [{"type": "moderator"}]}}}
    message = kick.message_from_event("App\\Events\\ChatMessageEvent", data)
    assert message.text == "gg KEKW!" and message.emotes == [(3, 6, "kick-37226")]
    assert message.images["kick-37226"].endswith("/37226/fullsize") and message.badges == ("moderator",)
    sub = kick.message_from_event("App\\Events\\GiftedSubscriptionsEvent",
                                  {"gifter_username": "Fern", "gifted_usernames": ["a", "b"]})
    assert sub.notice and sub.text == "Fern gifted 2 subs"


def test_kick_reader_subscribes_pongs_and_dedupes():
    from PySide6.QtCore import QCoreApplication

    QCoreApplication.instance() or QCoreApplication([])

    class Socket:
        sent = []

        def sendTextMessage(self, text):  # noqa: N802
            self.sent.append(json.loads(text))

    reader = kick.KickChat()
    reader.chatroom, reader.socket = 668, Socket()
    got, states = [], []
    reader.message.connect(got.append)
    reader.status.connect(states.append)
    reader.handle_text(json.dumps({"event": "pusher:connection_established", "data": "{}"}))
    assert [m["data"]["channel"] for m in reader.socket.sent] == ["chatrooms.668.v2", "chatroom_668"]
    reader.handle_text(json.dumps({"event": "pusher_internal:subscription_succeeded", "data": "{}"}))
    line = {"event": "App\\Events\\ChatMessageEvent",
            "data": json.dumps({"id": "x1", "content": "hi", "sender": {"username": "a"}})}
    reader.handle_text(json.dumps(line))
    reader.handle_text(json.dumps(line))
    reader.handle_text(json.dumps({"event": "pusher:ping", "data": {}}))
    assert len(got) == 1 and states == ["live"] and reader.socket.sent[-1]["event"] == "pusher:pong"
    reader._ping.stop()
    reader.socket = None
