"""The chat source: the shared feed drawn onto the canvas."""

import numpy as np

from sprigcast.chat import ChatMessage, readable_colour
from sprigcast.chat.feed import Feed, FEED
from sprigcast.sources import chat as chat_source


def test_feed_keeps_recent_lines_and_follows_mod_actions():
    feed = Feed(keep=3)
    for i in range(5):
        feed.push(ChatMessage("twitch", "a", f"m{i}", id=str(i), user_id="u1" if i % 2 else "u2"))
    version, lines = feed.snapshot()
    assert [line.message.text for line in lines] == ["m2", "m3", "m4"] and version == 5
    feed.remove(platform="twitch", message_id="3")
    assert [line.message.text for line in feed.snapshot()[1]] == ["m2", "m4"]
    feed.remove(platform="youtube", user_id="u2")  # another platform's ban leaves Twitch alone
    assert len(feed.snapshot()[1]) == 2
    feed.remove(platform="twitch", user_id="u2")
    assert feed.snapshot()[1] == [] and feed.version == 7


def test_readable_colour_without_qt():
    assert readable_colour("#0000FF", "x") != "#0000ff"  # too dark: lifted
    assert readable_colour("#ffcc00", "x") == "#ffcc00"
    assert readable_colour("", "fern") == readable_colour("", "FERN")
    assert readable_colour("nonsense", "x").startswith("#")


def test_chat_box_draws_newest_at_the_bottom_and_wraps(monkeypatch):
    monkeypatch.setattr(chat_source.EMOTES, "get", lambda url: None)  # no network in tests
    box = chat_source.ChatBox({"width": 300, "height": 200, "size": 20, "background_color": "#00000000"})
    lines = []
    feed = Feed()
    feed.push(ChatMessage("twitch", "fern", "short"))
    feed.push(ChatMessage("youtube", "moss", "a much longer message that will need to wrap onto more lines " * 2,
                          emotes=[(0, 0, "yt-1")], images={"yt-1": "https://yt3.ggpht.com/emoji-1"}))
    lines = feed.snapshot()[1]
    frame = box.draw(lines)
    assert frame.shape == (200, 300, 4) and frame.dtype == np.uint8
    alpha = frame[:, :, 3]
    assert alpha[-30:].any() and alpha[0, 0] == 0  # drawn from the bottom; corners stay see-through
    wrapped = box._wrap(lines[1].message, 240)
    assert len(wrapped) > 2 and wrapped[0][0][0] == "name" and wrapped[0][1][0] == "emote"
    assert all(sum(run[3] for run in ln) <= 240 + 1 for ln in wrapped)


def test_chat_source_redraws_only_when_chat_changes(monkeypatch):
    monkeypatch.setattr(chat_source.EMOTES, "get", lambda url: None)
    clock = [100.0]
    monkeypatch.setattr(chat_source.time, "monotonic", lambda: clock[0])
    FEED.clear()
    source = chat_source.ChatSource("chat1", {"width": 240, "height": 160})
    assert source.size == (240, 160)
    assert source.video_frame() is not None and source.status == "waiting for chat"  # an empty box first
    clock[0] += 1
    assert source.video_frame() is None  # nothing new: nothing drawn
    FEED.push(ChatMessage("youtube", "Canopy Kid", "hello"))
    clock[0] += 0.1
    assert source.video_frame() is None  # looks at most every 0.2 s
    clock[0] += 0.2
    assert source.video_frame() is not None and source.status == "ok"

    source.update({"width": 240, "height": 160, "hide_after": 5})
    clock[0] += 10  # older than "hide after": the box empties
    frame = source.video_frame()
    assert frame is not None and not frame[:, :, 3].any()
    FEED.clear()
