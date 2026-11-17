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


def test_kick_chat_is_gone_until_it_is_built_properly():
    """Kick chat read kick.com's private feed behind Cloudflare: removed (2026-11-17), see docs/REVISIT.md."""
    from sprigcast.chat import PLATFORMS, name_for

    assert "kick" not in PLATFORMS and name_for("kick", "kick.com/moss") is None
