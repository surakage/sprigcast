"""7TV, BetterTTV and FrankerFaceZ emotes, and animated emotes moving (chat/extra_emotes.py, sources/chat.py,
app/chat_panel.py). No network: the lists and pictures here are made up in the shapes those sites answer in."""

import io

import numpy as np
import pytest

from sprigcast.chat import ChatMessage, extra_emotes
from sprigcast.chat.feed import Feed
from sprigcast.sources import chat as chat_source

SEVENTV = {"id": "global", "name": "7TV", "emotes": [
    {"id": "01FCY771D800007PQ2DF3GDTN6", "name": "RainTime", "flags": 1, "data": {
        "id": "01FCY771D800007PQ2DF3GDTN6", "name": "RainTime", "animated": True,
        "host": {"url": "//cdn.7tv.app/emote/01FCY771D800007PQ2DF3GDTN6", "files": [
            {"name": "1x.avif", "width": 32, "height": 32, "format": "AVIF"},
            {"name": "1x.webp", "width": 32, "height": 32, "format": "WEBP"},
            {"name": "2x.webp", "width": 128, "height": 64, "format": "WEBP"}]}}},
    {"id": "broken", "name": "NoHost", "data": {}},
]}
BTTV = [{"id": "5e76d338d6581c3724c0f0b2", "code": "catJAM", "imageType": "gif", "animated": True},
        {"id": "5e76d399d6581c3724c0f0b8", "code": "w!", "imageType": "png", "animated": False, "modifier": True}]
BTTV_CHANNEL = {"id": "x", "bots": [], "channelEmotes": [{"id": "aaaaaaaaaaaaaaaaaaaaaaaa", "code": "sprigHi"}],
                "sharedEmotes": [{"id": "bbbbbbbbbbbbbbbbbbbbbbbb", "code": "catJAM"}]}
FFZ = {"default_sets": [3], "sets": {
    "3": {"emoticons": [{"id": 28136, "name": "LilZ", "width": 32, "height": 32,
                         "urls": {"1": "https://cdn.frankerfacez.com/emote/28136/1",
                                  "2": "https://cdn.frankerfacez.com/emote/28136/2"}}]},
    "1539687": {"emoticons": [{"id": 9, "name": "ffzRainbow", "modifier": True,
                               "urls": {"1": "https://cdn.frankerfacez.com/emote/9/1"}}]}}}
FFZ_ROOM = {"room": {"set": 166907}, "sets": {"166907": {"emoticons": [
    {"id": 381875, "name": "WideHard", "width": 96, "height": 32,
     "urls": {"1": "//cdn.frankerfacez.com/emote/381875/1"},
     "animated": {"1": "https://cdn.frankerfacez.com/emote/381875/animated/1"}}]}}}


def test_each_sites_list_is_read():
    seven = extra_emotes.parse_7tv(SEVENTV)
    assert seven == {"RainTime": extra_emotes.Emote(
        "7tv-01FCY771D800007PQ2DF3GDTN6", "https://cdn.7tv.app/emote/01FCY771D800007PQ2DF3GDTN6/2x.webp", 2.0)}
    assert extra_emotes.parse_7tv({"emote_set": SEVENTV}) == seven  # a channel's answer holds its set
    assert extra_emotes.parse_7tv({"emote_set": None}) == {} and extra_emotes.parse_7tv([]) == {}
    better = extra_emotes.parse_bttv(BTTV)
    assert list(better) == ["catJAM"]  # "w!" changes the emote before it: left as text
    assert better["catJAM"].url == "https://cdn.betterttv.net/emote/5e76d338d6581c3724c0f0b2/2x"
    assert set(extra_emotes.parse_bttv(BTTV_CHANNEL)) == {"sprigHi", "catJAM"}
    ffz = extra_emotes.parse_ffz(FFZ)
    assert list(ffz) == ["LilZ"] and ffz["LilZ"].key == "ffz-28136"  # only the sets for everyone, no effects
    room = extra_emotes.parse_ffz(FFZ_ROOM)["WideHard"]
    assert room.url.endswith("/381875/animated/1") and room.ratio == 3.0  # its moving picture, and it's wide
    assert extra_emotes.parse_ffz({"sets": []}) == {}


def test_only_twitch_asks_all_three_and_a_channels_own_win():
    twitch = [what for what, _p, _u in extra_emotes.lists_for("twitch", "123")]
    assert twitch == ["FrankerFaceZ", "BetterTTV", "7TV", "FrankerFaceZ channel", "BetterTTV channel", "7TV channel"]
    assert [u for _w, _p, u in extra_emotes.lists_for("kick", "55")] == [
        extra_emotes.SEVENTV_GLOBAL, "https://7tv.io/v3/users/kick/55"]
    assert len(extra_emotes.lists_for("youtube", "")) == 1  # no channel id yet: the ones for everyone
    assert len(extra_emotes.lists_for("twitch", "../x")) == 3  # nothing odd goes into an address


def test_words_become_emotes_only_as_whole_words_outside_twitchs_own():
    emotes = {"catJAM": extra_emotes.Emote("bttv-1", "https://cdn.betterttv.net/emote/1/2x"),
              "Kappa": extra_emotes.Emote("7tv-9", "https://cdn.7tv.app/emote/9/2x.webp", 2.0)}
    message = ChatMessage("twitch", "fern", "Kappa catJAM catjam  catJAMs catJAM", emotes=[(0, 4, "25")])
    assert extra_emotes.mark(message, emotes) == 2
    assert message.emotes == [(0, 4, "25"), (6, 11, "bttv-1"), (29, 34, "bttv-1")]  # Twitch's own Kappa stays
    assert message.images == {"bttv-1": "https://cdn.betterttv.net/emote/1/2x"} and message.ratios == {}
    wide = ChatMessage("kick", "moss", "Kappa")
    extra_emotes.mark(wide, emotes)
    assert wide.ratios == {"7tv-9": 2.0}
    notice = ChatMessage("twitch", "", "catJAM subscribed", notice=True)
    assert extra_emotes.mark(notice, emotes) == 0


def test_a_chats_lists_are_looked_up_once_and_again_later_when_one_failed():
    asked, clock, down = [], [0.0], [True]

    def fetch(url):
        asked.append(url)
        if "betterttv" in url and down[0]:
            raise OSError("unreachable")
        if "users/twitch" in url and "7tv" in url:
            return None  # this channel has no 7TV set
        return SEVENTV if "7tv" in url else (FFZ if url.endswith("global") else FFZ_ROOM if "franker" in url else BTTV)

    extra = extra_emotes.ExtraEmotes(fetch, clock=lambda: clock[0])
    extra.want("twitch", "123", wait=True)
    assert len(asked) == 6 and set(extra.emotes("twitch")) == {"RainTime", "LilZ", "WideHard"}
    extra.want("twitch", "123", wait=True)
    assert len(asked) == 6  # not again straight away
    down[0] = False
    clock[0] += extra_emotes.AGAIN_S + 1
    extra.want("twitch", "123", wait=True)
    assert len(asked) == 12 and "catJAM" in extra.emotes("twitch")
    message = ChatMessage("twitch", "fern", "hi catJAM")
    assert extra.apply(message) == 1
    extra.enabled = False
    assert extra.apply(ChatMessage("twitch", "fern", "hi catJAM")) == 0
    assert extra.emotes("kick") == {}


def test_the_hub_marks_extra_emotes_once_the_reader_knows_its_channel(monkeypatch):
    pytest.importorskip("PySide6")
    from sprigcast.chat import hub as chat_hub

    wanted = []
    extra = extra_emotes.ExtraEmotes(lambda url: BTTV if "betterttv" in url and url.endswith("global") else None)
    monkeypatch.setattr(extra, "want", lambda platform, room="", wait=False, real=extra.want: (
        wanted.append((platform, room)), real(platform, room, wait=True)))
    monkeypatch.setattr(chat_hub, "EXTRA", extra)
    monkeypatch.setattr(chat_hub, "FEED", Feed())
    hub = chat_hub.ChatHub(lambda: {})

    class Reader:
        room = ""

    hub.readers["twitch"] = reader = Reader()
    got = []
    hub.message.connect(got.append)
    hub._state("twitch", "live")
    reader.room = "123"
    hub._message(ChatMessage("twitch", "fern", "catJAM"))
    hub._message(ChatMessage("twitch", "fern", "catJAM again"))
    assert wanted == [("twitch", ""), ("twitch", "123")]  # asked once per channel, not once per message
    assert [m.emotes for m in got] == [[(0, 5, "bttv-5e76d338d6581c3724c0f0b2")]] * 2


# Animated emotes -------------------------------------------------------------------------------------------

def _gif(count=3, size=(28, 28), ms=100) -> bytes:
    from PIL import Image

    frames = [Image.new("RGBA", size, (80 * i, 255 - 80 * i, 0, 255)) for i in range(count)]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=ms, loop=0)
    return out.getvalue()


def test_an_animated_emotes_pictures_are_kept_small_and_few():
    from PIL import Image

    moving = chat_source.moving_from(Image.open(io.BytesIO(_gif(3))))
    assert len(moving.frames) == 3 and moving.total == pytest.approx(0.3)
    assert moving.at(0.05) is moving.frames[0] and moving.at(0.15) is moving.frames[1]
    assert moving.at(0.35) is moving.frames[0]  # round again
    long = chat_source.moving_from(Image.open(io.BytesIO(_gif(100, size=(200, 100), ms=20))))
    assert len(long.frames) <= chat_source.MOVING_FRAMES and long.total == pytest.approx(2.0)  # as long, fewer
    assert long.height == chat_source.MOVING_SIDE and long.width == 2 * chat_source.MOVING_SIDE
    assert chat_source.moving_from(Image.open(io.BytesIO(_gif(1)))) is None


def _moving_box(monkeypatch, **settings):
    from PIL import Image

    moving = chat_source.moving_from(Image.open(io.BytesIO(_gif(3))))
    monkeypatch.setattr(chat_source.EMOTES, "get", lambda url: moving)
    box = chat_source.ChatBox({"width": 300, "height": 120, "size": 20, **settings})
    feed = Feed()
    feed.push(ChatMessage("twitch", "fern", "hi catJAM", emotes=[(3, 8, "bttv-1")],
                          images={"bttv-1": "https://cdn.betterttv.net/emote/1/2x"}))
    return box, feed.snapshot()[1]


def test_on_stream_only_the_moving_emotes_are_pasted_again(monkeypatch):
    box, lines = _moving_box(monkeypatch)
    first = box.draw(lines, now=0.05)
    assert box.moving and first.shape == (120, 300, 4)
    assert np.array_equal(box.frame(0.05), first)
    later = box.frame(0.15)
    changed = np.argwhere((later != first).any(axis=2))
    assert len(changed) and np.ptp(changed[:, 0]) < 30 and np.ptp(changed[:, 1]) < 30  # only where the emote is
    assert later is not first and np.array_equal(box.frame(0.35), first)

    still, lines = _moving_box(monkeypatch, animate_emotes=False)
    drawn = still.draw(lines, now=0.15)
    assert not still.moving and np.array_equal(drawn, first)  # its first picture, drawn once


def test_the_chat_source_moves_its_emotes_between_chat_changes(monkeypatch):
    from PIL import Image

    moving = chat_source.moving_from(Image.open(io.BytesIO(_gif(3))))
    monkeypatch.setattr(chat_source.EMOTES, "get", lambda url: moving)
    monkeypatch.setattr(chat_source.painted, "DRAW_ASIDE", False)
    feed = Feed()
    monkeypatch.setattr(chat_source, "FEED", feed)
    clock = [100.0]
    monkeypatch.setattr(chat_source.time, "monotonic", lambda: clock[0])
    source = chat_source.ChatSource("chat-1", {"width": 300, "height": 120})
    feed.push(ChatMessage("twitch", "fern", "catJAM", emotes=[(0, 5, "bttv-1")],
                          images={"bttv-1": "https://cdn.betterttv.net/emote/1/2x"}))
    first = source.video_frame()
    assert first is not None
    clock[0] += 0.01
    assert source.video_frame() is None  # not every frame
    clock[0] += 0.1
    second = source.video_frame()
    assert second is not None and not np.array_equal(first, second)
    plain = chat_source.ChatSource("chat-2", {"width": 300, "height": 120, "animate_emotes": False})
    assert plain.video_frame() is not None
    clock[0] += 0.5
    assert plain.video_frame() is None  # still emotes: nothing drawn until chat changes


@pytest.fixture
def app():
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("EmoteTest")
    QSettings().clear()
    yield app
    QSettings().clear()


class _Reply:
    def __init__(self, data):
        self._data = data

    def readAll(self):  # (Qt naming)
        from PySide6.QtCore import QByteArray

        return QByteArray(self._data)

    def deleteLater(self):
        pass


def _panel_with_a_moving_emote(app, wide=1.0):
    from PySide6.QtCore import QUrl

    from sprigcast.app import chat_panel

    view = chat_panel.ChatView()
    view.resize(320, 200)
    view.show()
    message = ChatMessage("twitch", "fern", "hi catJAM", emotes=[(3, 8, "bttv-1")],
                          images={"bttv-1": "https://cdn.betterttv.net/emote/1/2x"})
    if wide != 1.0:
        message.ratios["bttv-1"] = wide
    view.add(message)
    view.flush()
    view._loading = 1
    view._fetched(_Reply(_gif(3)), QUrl("emote:bttv-1"))
    view._pictures_arrived()
    return chat_panel, view


def _shot(view):
    from PySide6.QtGui import QImage

    image = view.viewport().grab().toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    return bytes(image.constBits())


def test_in_the_panel_an_animated_emote_moves_without_laying_the_page_out_again(app):
    _panel, view = _panel_with_a_moving_emote(app)
    try:
        assert list(view._moving) == ["bttv-1"] and len(view._moving["bttv-1"][0]) == 3
        assert view._move.isActive()
        laid_out = []
        view.document().documentLayout().documentSizeChanged.connect(laid_out.append)
        view._move_on(0.05)
        first = _shot(view)
        view._move_on(0.15)
        assert view._moving_at == {"bttv-1": 1}
        assert _shot(view) != first  # the page shows its next picture
        view._move_on(0.35)
        assert _shot(view) == first and not laid_out
        # Off in the Look menu: the timer stops and nothing more is handed to the page
        from dataclasses import replace

        view.set_look(replace(view.look, moving=False))
        assert not view._move.isActive()
        view._move_on(0.15)
        assert view._moving_at == {}
    finally:
        view.close()


def test_the_panels_timer_only_runs_while_a_moving_emote_is_on_the_page_and_shown(app):
    _panel, view = _panel_with_a_moving_emote(app)
    try:
        view.hide()
        assert not view._move.isActive()
        view.show()
        assert view._move.isActive()
        view.clear_chat()
        view._move_on()
        assert not view._move.isActive()  # none on the page any more
        view.add(ChatMessage("twitch", "fern", "catJAM", emotes=[(0, 5, "bttv-1")]))
        assert view._move.isActive()  # one came back
    finally:
        view.close()


def test_a_wide_emote_keeps_its_shape_in_the_panel(app):
    from sprigcast.app import chat_panel

    message = ChatMessage("twitch", "fern", "WideHard", emotes=[(0, 7, "ffz-1")], ratios={"ffz-1": 3.0})
    assert f"width={3 * chat_panel.EMOTE_SIZE} height={chat_panel.EMOTE_SIZE}" in chat_panel._body(message)
    assert f"width={chat_panel.EMOTE_SIZE} height={chat_panel.EMOTE_SIZE}" in chat_panel._body(
        ChatMessage("twitch", "fern", "Kappa", emotes=[(0, 4, "25")]))


def test_the_look_menu_switches_extra_emotes_off(app, monkeypatch):
    from dataclasses import replace

    from sprigcast.app import chat_panel
    from sprigcast.chat import hub as chat_hub

    extra = extra_emotes.ExtraEmotes(lambda url: None)
    monkeypatch.setattr(chat_panel, "EXTRA", extra)
    monkeypatch.setattr(chat_hub, "EXTRA", extra)
    panel = chat_panel.ChatPanel()
    panel.use_look(replace(panel.view.look, extra=False))
    assert not extra.enabled and not chat_panel.Look.saved().extra
    chat_hub.ChatHub(lambda: {})
    assert not extra.enabled  # remembered the next time SprigCast opens
