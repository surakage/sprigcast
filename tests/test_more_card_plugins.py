"""Five small card plugins (user, 2026-11-27): Next stream, Chat word counter, Playing now, Pinned message and
Name tag. Each draws SprigCast's clock card, only when its words change."""

import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

from sprigcast import plugin_hooks, plugin_store
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def plugins(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    ids = ("next_stream", "word_count", "playing_now", "pinned", "name_tag")
    for pid in ids:
        assert plugin_store.install(tmp_path / "plugins", next(p for p in plugin_store.catalog() if p.id == pid)) is None
    yield {pid: sys.modules[f"sprigcast_plugin_{pid}"] for pid in ids}
    plugin_hooks.output_changed("stream", None)
    for pid in ids:
        plugin_store.remove(tmp_path / "plugins", pid)
        assert pid not in known_kinds()


def test_next_stream(plugins):
    ns = plugins["next_stream"]
    wed = datetime(2026, 11, 25, 18, 0)  # a Wednesday
    assert ns.words({"when": "Friday 7 PM", "label": "Next stream"}, wed) == ("Friday 7 PM", "Next stream  ·  in 2 days")
    assert ns.words({"when": "fri 19:30", "label": ""}, wed) == ("Friday 7:30 PM", "in 2 days")
    assert ns.words({"when": "Thu 7 PM", "label": ""}, wed)[1] == "tomorrow"
    assert ns.parse("Wednesday 5 PM", wed).day == 2  # over: next week's
    assert ns.parse("Wednesday 5:45 PM", wed).day == 25  # a little late still counts as today
    assert ns.words({"when": "someday"}, wed) is None
    source = create_source("next_stream", "n", {"when": "nope"})
    assert not source.video_frame()[..., 3].any() and "isn't a day" in source.status
    source.update({"when": "Friday 7 PM"})
    assert source.video_frame()[..., 3].any() and source.video_frame() is None


def _line(text, at, notice=False):
    return SimpleNamespace(at=at, message=SimpleNamespace(text=text, notice=notice))


def test_word_counter(plugins):
    wc = plugins["word_count"]
    s = {"word": "GG, ggs", "once_per_message": True}
    assert wc.hits("GG gg GGs", s) == 1 and wc.hits("eggs", s) == 0
    assert wc.hits("GG gg ggs", {**s, "once_per_message": False}) == 3
    c = wc.COUNTER = wc.Counter()
    c.take(s, [_line("gg", 1.0)], 0.5)  # chat from before: not counted
    assert c.count == 0
    c.take(s, [_line("gg", 1.0), _line("GG!", 2.0), _line("hi", 3.0), _line("gg", 4.0, notice=True)], 0.5)
    assert c.count == 1
    c.take(s, [_line("gg", 1.0), _line("GG!", 2.0), _line("hi", 3.0), _line("gg wp", 5.0)], 0.5)
    assert c.count == 2
    c.take(s, [], 9.0)  # a new stream
    assert c.count == 0
    from sprigcast.control.native import _PLUGIN_ACTIONS

    api = SimpleNamespace(col=SimpleNamespace(sources=[SimpleNamespace(kind="word_count", settings={"word": "F"})]))
    assert _PLUGIN_ACTIONS["wordcount.add"].run(api, {"amount": 3}) == {"count": 3, "word": "F"}
    assert _PLUGIN_ACTIONS["wordcount.reset"].run(api, {})["count"] == 0
    assert wc.big_text({"word": "GG"}, 1234) == "GG  ×  1,234"
    source = create_source("word_count", "w", {})
    assert source.video_frame()[..., 3].any() and source.video_frame() is None
    c.add(1)
    assert source.video_frame() is not None


def test_playing_now(plugins, monkeypatch):
    pn = plugins["playing_now"]
    from sprigcast.output import twitch_auth

    monkeypatch.setattr(twitch_auth, "helix_get", lambda path_for, scope=None: {"data": [{"game_name": "Minecraft"}]}
                        if path_for("7") == "/channels?broadcaster_id=7" else {})
    pn.CATEGORY = pn.Category()
    source = create_source("playing_now", "p", {})
    assert not source.video_frame()[..., 3].any()  # nothing yet: hidden
    pn.CATEGORY.busy, pn.CATEGORY.where = True, "twitch"
    pn.CATEGORY._read("twitch")
    assert pn.CATEGORY.name == "Minecraft" and pn.shown_text({}) == "Minecraft"
    assert source.video_frame()[..., 3].any()
    assert pn.shown_text({"text": "Just chatting"}) == "Just chatting"


def test_pinned_message(plugins):
    pm = plugins["pinned"]
    pm.unpin()
    source = create_source("pinned", "m", {})
    assert not source.video_frame()[..., 3].any()
    entries = []
    plugin_hooks.fill_chat_menu(SimpleNamespace(text="that jump was clean", user="Mikedog151", notice=False),
                                lambda label, fn: entries.append((label, fn)))
    assert [e[0] for e in entries] == ["Pin on stream"]
    entries[0][1]()
    assert pm.PIN["text"] == "that jump was clean" and pm.PIN["user"] == "Mikedog151"
    assert source.video_frame()[..., 3].any()
    assert pm.showing({"unpin_after": 1}, pm.PIN["at"] + 61) is None
    from sprigcast.control.native import _PLUGIN_ACTIONS

    assert _PLUGIN_ACTIONS["pin.clear"].run(None, {}) == {"text": None, "user": None}


def test_name_tag_slides_in_stays_and_leaves(plugins, monkeypatch):
    nt = plugins["name_tag"]
    s = {"show_for": 8}
    assert nt.step_at(0.0, s) == 1 and nt.step_at(0.2, s) < nt.STEPS and nt.step_at(1, s) == nt.STEPS
    assert nt.step_at(8.5, s) < nt.STEPS and nt.step_at(9.5, s) == 0
    assert nt.step_at(1000, {"show_for": 0}) == nt.STEPS
    now = [100.0]
    monkeypatch.setattr(nt.time, "monotonic", lambda: now[0])
    source = create_source("name_tag", "t", {"name": "Suraruisuh", "line": "Cozy games"})
    now[0] = 101.0
    full = source.video_frame()
    assert full is not None and full[:, :8, 3].any()
    now[0] = 120.0
    assert not source.video_frame()[..., 3].any()
    source.wake()
    now[0] = 120.1
    half = source.video_frame()
    assert half is not None and not half[:, -8:, 3].any()  # still sliding in from the left
