"""On-stream chat looks: Garden card, Clean outline, Minimal and Big text (user 2026-11-27)."""

import time

from sprigcast.chat import ChatMessage
from sprigcast.chat.feed import Line
from sprigcast.sources import chat


def _lines():
    return [Line(ChatMessage(platform="twitch", user="Moss", text="hello there garden", color="#9b7bff"),
                 time.monotonic())]


def test_each_look_draws_its_own_way():
    boxes = {key: chat.ChatBox({"chat_look": key}) for key in chat.LOOKS}
    assert boxes["card"].bubble[3] and boxes["big"].bubble[3]
    assert not boxes["outline"].bubble[3] and not boxes["minimal"].bubble[3]
    assert boxes["big"].size == round(boxes["card"].size * 1.5)
    assert boxes["outline"].stroke[0] >= 2 and boxes["card"].stroke[0] == 0
    assert boxes["outline"].marks and not boxes["minimal"].marks
    pictures = {key: box.draw(_lines()) for key, box in boxes.items()}
    assert all(p.shape == (640, 480, 4) and p[..., 3].any() for p in pictures.values())
    assert chat.ChatBox({"chat_look": "nonsense"}).look_key == "card"


def test_the_look_is_a_drop_down_on_the_style_tab():
    from sprigcast.core import style

    assert [label for _key, label in chat.ChatSource.choices["chat_look"]] == \
        ["Big text", "Clean outline", "Garden card", "Minimal"]
    assert style.is_style_key("chat_look") and chat.DEFAULTS["chat_look"] == "card"
