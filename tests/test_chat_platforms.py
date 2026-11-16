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
    sent = {"event": "App\\Events\\ChatMessageSentEvent",
            "data": json.dumps({"id": "x2", "content": "yo", "sender": {"username": "b"}})}
    reader.handle_text(json.dumps(sent))
    assert [m.text for m in got] == ["hi", "yo"]
    # A retired chat key (Pusher 4001) moves the next try on to the next key
    assert reader._key_index == 0
    reader.handle_text(json.dumps({"event": "pusher:error", "data": {"code": 4001, "message": "gone"}}))
    assert reader._key_index == 1
    reader._ping.stop()
    reader.socket = None


def test_tiktok_handles_and_tikfinity_messages():
    from sprigcast.chat import tiktok

    assert tiktok.handle_from_text("https://www.tiktok.com/@Sloth.Bloom/live") == "sloth.bloom"
    assert tiktok.handle_from_text("@moss_fern") == "moss_fern"
    assert tiktok.handle_from_text("no spaces allowed") is None
    chat = tiktok.message_from_tikfinity({"event": "chat", "data": {
        "uniqueId": "surakage", "nickname": "Sura", "comment": "hi chat", "msgId": "7", "userId": "1",
        "isModerator": True, "tikfinityUsername": "surakage",
        "emotes": [{"placeInComment": 2, "emoteId": "e1", "emoteImageUrl": "https://p16.tiktokcdn.com/e1.png"}]}})
    assert chat.user == "Sura" and chat.badges == ("broadcaster", "moderator")
    assert chat.text == "hi▢ chat" and chat.emotes == [(2, 2, "tt-e1")] and chat.images["tt-e1"].startswith("https://")
    streak = {"event": "gift", "data": {"nickname": "Fern", "giftName": "Rose", "giftType": 1, "repeatCount": 3,
                                        "repeatEnd": False}}
    assert tiktok.message_from_tikfinity(streak) is None  # mid-streak: wait for the last count
    streak["data"]["repeatEnd"] = True
    gift = tiktok.message_from_tikfinity(streak)
    assert gift.notice and gift.text == "Fern sent Rose x3"
    assert tiktok.message_from_tikfinity({"event": "like", "data": {"likeCount": 5}}) is None
    assert tiktok.message_from_tikfinity({"event": "follow", "data": {"nickname": "Moss"}}).text == "Moss followed"


def test_tiktok_falls_back_to_tiktok_itself_when_tikfinity_is_not_running(monkeypatch):
    from PySide6.QtCore import QCoreApplication

    from sprigcast.chat import tiktok

    QCoreApplication.instance() or QCoreApplication([])
    reader = tiktok.TikTokChat()
    direct, tried = [], []
    monkeypatch.setattr(reader, "_open_tikfinity", lambda: tried.append("tikfinity"))
    monkeypatch.setattr(reader, "_open_direct", lambda: direct.append(reader.channel))
    reader.start("sloth.bloom")
    assert tried == ["tikfinity"]
    reader._tikfinity_down()  # never connected: TikFinity isn't running
    assert direct == ["sloth.bloom"]
    reader._tikfinity_up()
    reader._tikfinity_down()  # was running and closed: look for it again first
    assert direct == ["sloth.bloom"] and reader._retry.isActive()
    old = reader._run
    reader._direct_ended(old - 1, "stream ended", True)  # an old run ending changes nothing
    reader.stop()


def test_tiktok_not_live_checks_slow_down_and_going_live_checks_now(monkeypatch):
    from PySide6.QtCore import QCoreApplication

    from sprigcast.chat import tiktok

    QCoreApplication.instance() or QCoreApplication([])
    assert [tiktok.not_live_wait(n) for n in (1, 10, 11, 30, 31, 500)] == [60, 60, 120, 120, 300, 300]
    reader = tiktok.TikTokChat()
    states = []
    reader.status.connect(states.append)
    monkeypatch.setattr(reader, "_open_tikfinity", lambda: None)
    reader.start("sloth.bloom")
    for _ in range(12):
        reader._direct_ended(reader._run, "not live yet", True)
    assert reader._retry.interval() == 120_000
    assert states.count("connecting") == 1  # quiet rechecks don't flip the state (or fill the log)
    reader.check_now()
    assert reader._checks == 0 and reader._retry.interval() == 0
    reader.stop()
    reader.check_now()  # stopped: nothing to do
    assert not reader._retry.isActive()


def test_tiktok_direct_never_imports_the_heavy_library_on_the_ui_thread(monkeypatch):
    import sys

    from PySide6.QtCore import QCoreApplication

    from sprigcast.chat import tiktok

    QCoreApplication.instance() or QCoreApplication([])
    for name in [n for n in sys.modules if n == "TikTokLive" or n.startswith(("TikTokLive.", "TikTokLiveProto"))]:
        monkeypatch.delitem(sys.modules, name)
    scheduled = []

    class Loop:
        loop = object()

    monkeypatch.setattr(tiktok, "_shared_loop", lambda: Loop())
    monkeypatch.setattr(tiktok.asyncio, "run_coroutine_threadsafe", lambda coro, loop: (scheduled.append(coro), coro.close()))
    reader = tiktok.TikTokChat()
    reader._wanted, reader.channel = True, "sloth.bloom"
    reader._open_direct()
    assert scheduled and reader.via == "direct"
    assert not any(n == "TikTokLive" or n.startswith("TikTokLiveProto") for n in sys.modules)
    reader.stop()
