"""TikTok gifts, likes, follows and shares from TikFinity become SprigCast API events (group "tiktok")."""

from sprigcast.chat.tiktok import event_from_tikfinity
from sprigcast.control.native import NativeApi, NativeSession, event_group


def _frame(event, **data):
    return {"event": event, "data": {"nickname": "Leafy", "uniqueId": "leafy", "userId": "9", **data}}


def test_gift_streak_waits_for_its_end():
    assert event_from_tikfinity(_frame("gift", giftType=1, repeatEnd=False, repeatCount=3)) is None
    name, data = event_from_tikfinity(_frame("gift", giftType=1, repeatEnd=True, repeatCount=5,
                                             giftName="Rose", giftId=5655, diamondCount=1))
    assert name == "tiktok.gift"
    assert data == {"user": "Leafy", "handle": "leafy", "userId": "9", "gift": "Rose", "giftId": 5655,
                    "count": 5, "diamonds": 5}


def test_like_follow_share_subscribe_chat():
    assert event_from_tikfinity(_frame("like", likeCount=15, totalLikeCount=300))[1]["total"] == 300
    for kind in ("follow", "share", "subscribe"):
        assert event_from_tikfinity(_frame(kind)) == (f"tiktok.{kind}", {"user": "Leafy", "handle": "leafy", "userId": "9"})
    assert event_from_tikfinity(_frame("chat", comment="hi"))[1]["text"] == "hi"
    assert event_from_tikfinity(_frame("roomUser", viewerCount=3)) is None
    assert event_from_tikfinity({"event": "gift", "data": "nope"}) is None


def test_apps_get_them_in_the_tiktok_group():
    assert event_group("tiktok.gift") == "tiktok"
    session = NativeSession(api=NativeApi.__new__(NativeApi))
    session.welcomed = True
    assert session.event("tiktok.follow", {"user": "Leafy"}) is not None
    session.events = {"bloom"}
    assert session.event("tiktok.follow", {"user": "Leafy"}) is None
