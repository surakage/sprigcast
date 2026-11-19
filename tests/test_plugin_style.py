"""The shared look for plugin cards (core/style.py): the Style tab and the custom look box."""

import sys

import numpy as np
import pytest

from sprigcast.core import style


def test_colours_of_every_kind_and_nothing_else():
    assert style.colour("#fff") == "#ffffffff"
    assert style.colour("#10203040") == "#10203040"
    assert style.colour("PINK") == "#ff7eb6ff"
    assert style.colour("rgba(255, 0, 0, 0.5)") == "#ff000080"
    assert style.colour("rgb(100%, 0%, 0%)") == "#ff0000ff"
    for bad in ("", "nope", "#12345", "rgb(300, 0, 0)", "rgba(1,2,3,7)", None, 12, ["#fff"]):
        assert style.colour(bad) is None


def test_a_custom_look_reads_like_css_and_skips_what_it_doesnt_know():
    rules, skipped = style.parse_look("""
        /* my clock */
        .clock {
          color: #fff; background: rgba(0,0,0,0.5);
          font-family: "Comic Sans MS", sans-serif;
          font-size: 120%; border-radius: 0; border: 3px solid white;
          hide: date, dot; wobble: 3; just words
        }""")
    assert rules == {"text": "#ffffffff", "card": "#00000080", "font": "Comic Sans MS", "size": 1.2,
                     "corners": 0.0, "border": (3.0, "#ffffffff"), "hide": frozenset({"date", "dot"})}
    assert skipped == ("wobble: not a look setting", "just words: needs a colon")


@pytest.mark.parametrize("text", [
    "size: 900%", "text: url(http://x)", "font: x; DROP TABLE", "corners: banana", "hide: ../../", "border: 3 fish",
    "{" * 5000, ":" * 5000, "a" * 100_000, "\x00\x01:\x02", "text: " + "#" * 3000,
    "import os; os.system('x')", "__import__('os')",
])
def test_nothing_a_custom_look_says_can_break_a_plugin(text):
    look = style.resolve({"custom_look": text}, {"text_color": "#123456"})
    assert isinstance(look, style.Style)
    assert 0.5 <= look.scale <= 2.0
    assert style.colour(look.text) == look.text
    style.look_problems(text)  # and the note under the box


def test_settings_and_the_custom_look_on_top():
    base = {"text_color": "#f2f7f3", "card_color": "#151c19e0", "accent_color": "#4fd88f"}
    plain = style.resolve({}, base)
    assert (plain.text, plain.card, plain.accent, plain.scale, plain.corners) == \
        ("#f2f7f3ff", "#151c19e0", "#4fd88fff", 1.0, "round")
    mine = style.resolve({"text_color": "white", "font": "Georgia", "text_scale": 150, "corners": "leaf"}, base)
    assert (mine.text, mine.font, mine.scale, mine.corners) == ("#ffffffff", "Georgia", 1.5, "leaf")
    assert mine.families()[0] == "Georgia" and plain.families()[0] == "Nunito"
    bad = style.resolve({"text_color": "nope", "text_scale": "big", "corners": "wavy", "font": "x.ttf"}, base)
    assert (bad.text, bad.scale, bad.corners, bad.font) == ("#f2f7f3ff", 1.0, "round", "")
    over = style.resolve({"text_color": "white", "custom_look": "text: pink\nsize: 0.8\ncorners: 12"}, base)
    assert (over.text, over.scale, over.radius) == ("#ff7eb6ff", 0.8, 12.0)
    assert over.key != plain.key


def test_parts_shown_and_hidden():
    look = style.resolve({"custom_look": "hide: date\nshow: dot"})
    assert not look.shows("date", {"show_date": True})
    assert look.shows("dot", {"show_dot": False})
    assert not style.Style().shows("label", {"show_label": False})
    assert style.Style().shows("label", {})
    assert not style.Style().shows("label", {}, default=False)


def test_looking_twice_is_free():
    settings = {"custom_look": "text: pink", "text_color": "#fff"}
    assert style.resolve(settings) is style.resolve(dict(settings))


def test_style_keys():
    assert style.style_keys(["text", "text_color", "show_date", "custom_look", "minutes"]) == \
        ["text_color", "show_date", "custom_look"]


# Drawing -------------------------------------------------------------------------------------------------

pytest.importorskip("PySide6.QtWidgets")

from test_store_effects import added, app  # noqa: E402,F401


def test_card_shapes(app):  # noqa: F811
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter, QColor

    for corners in ("round", "leaf", "square"):
        look = style.resolve({"corners": corners, "custom_look": "border: 2 white\nshadow: on"})
        image = QImage(200, 100, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        p = QPainter(image)
        path = style.draw_card(p, QRectF(10, 10, 180, 60), 24, look)
        p.end()
        assert not path.isEmpty()
        corner = QColor.fromRgba(image.pixel(12, 12)).alpha()
        assert (corner > 200) == (corners == "square"), corners  # round and leaf cut the top-left corner
        assert QColor.fromRgba(image.pixel(100, 40)).alpha() > 200


def test_now_playing_takes_the_look(added):  # noqa: F811
    added("now_playing")
    mod = sys.modules["sprigcast_plugin_now_playing"]
    from PySide6.QtGui import QColor

    track = mod.Track("Song", "Band")
    for settings in ({}, {"corners": "leaf", "font": "Georgia"}, {"custom_look": "card: #ff000080\nhide: dot"},
                     {"custom_look": "this is not a look at all"}):
        image = mod.paint(track, settings)
        assert (image.width(), image.height()) == (mod.W, mod.H)
    red = mod.paint(track, {"custom_look": "card: #ff0000"})
    assert QColor(red.pixel(300, 30)).red() > 200


def test_chat_bubbles_take_the_look():
    from sprigcast.chat import ChatMessage
    from sprigcast.chat.feed import Line
    from sprigcast.sources.chat import ChatBox

    message = ChatMessage(platform="twitch", user="sloth", text="hello there friends", color="#ff00ff")
    lines = [Line(message, 0.0)]
    for settings in ({}, {"corners": "leaf"}, {"corners": "square"},
                     {"custom_look": "card: #ff000080\nborder: 2 white\ncorners: leaf\nhide: marks"},
                     {"custom_look": "font: Comic Sans MS; size: 150%"}, {"custom_look": "!!!"}):
        frame = ChatBox(settings).draw(lines)
        assert frame.shape == (640, 480, 4)
        assert frame[..., 3].any()
    from PIL import Image, ImageDraw

    box = ChatBox({"corners": "leaf"})
    image = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    box._bubble(image, ImageDraw.Draw(image, "RGBA"), (10, 10, 190, 70), 60, (255, 0, 0, 128))
    alphas = set(np.unique(np.asarray(image)[..., 3]))
    assert 128 in alphas and max(alphas) == 128  # see-through stays even: the two corner sizes aren't layered
    assert np.asarray(image)[11, 11, 3] == 0 and np.asarray(image)[11, 188, 3] > 0  # big corner cut, small one not


def test_the_style_tab(app):  # noqa: F811
    from PySide6.QtWidgets import QTabWidget

    from sprigcast.app.settings_dialog import SettingsDialog

    defaults = {"minutes": 5, "text_color": "#ffffff", "card_color": "#000000e0", "font": "", "text_scale": 100,
                "corners": "round", "show_date": True, "custom_look": ""}
    dialog = SettingsDialog("Clock", defaults, {**defaults, "font": "Papyrus", "text_scale": 140,
                                                "custom_look": "wobble: 2"})
    tabs = dialog.findChild(QTabWidget)
    assert tabs is not None and [tabs.tabText(i) for i in range(tabs.count())] == ["Settings", "Style"]
    assert "wobble" in dialog.look_note.text()
    values = dialog.values()
    assert values["font"] == "Papyrus" and values["text_scale"] == 140 and values["corners"] == "round"
    dialog.fields["font"].setCurrentIndex(0)
    assert dialog.values()["font"] == ""
    dialog.fields["custom_look"].setPlainText("text: pink")
    assert not dialog.look_note.text()
    dialog._reset_style(style.style_keys(defaults))
    assert dialog.values() == defaults
    one = SettingsDialog("Box", {"width": 4, "color": "#fff"}, {})
    assert one.findChild(QTabWidget) is None


def test_clock_cards_take_the_look(app):  # noqa: F811
    from PySide6.QtGui import QColor

    from sprigcast.core import cards

    words = cards.Words("8:42 PM", "Wed, Nov 18", badge="moon")
    plain = cards.card_width([words])
    assert cards.card_width([words], cards.look_of({})) == plain
    assert cards.card_width([words], cards.look_of({"text_scale": 150})) > plain
    assert cards.card_width([words], cards.look_of({"custom_look": "hide: badge"})) < plain
    for settings in ({}, {"corners": "round"}, {"corners": "square", "font": "Georgia", "text_scale": 200},
                     {"custom_look": "card: #fff8e7; border: 3 pink; shadow: on; hide: badge, vine, label"},
                     {"custom_look": "corners: 0; size: 50%"}, {"custom_look": "}}}{{{ nonsense"}):
        look = cards.look_of(settings)
        width = cards.card_width([words], look)
        image = cards.paint(width, cards.Words("4:59", "Starting in", small_on_top=True, vine=0.5), look=look)
        assert (image.width(), image.height()) == (width, cards.H)
    red = cards.paint(400, words, look=cards.look_of({"card_color": "#ff0000"}))
    assert QColor.fromRgba(red.pixel(200, 200)).red() > 200
    square = cards.paint(400, words, look=cards.look_of({"corners": "square"}))
    assert QColor.fromRgba(square.pixel(6, 6)).alpha() > 0  # leaf-cut by default; square fills the corner
    assert QColor.fromRgba(cards.paint(400, words).pixel(6, 6)).alpha() == 0


def test_the_clock_plugins_have_the_style_tab(added):  # noqa: F811
    from sprigcast.core.source import create_source

    for plugin, kind in (("local_time", "local_time"), ("countdown", "countdown"),
                         ("stream_timer", "stream_timer")):
        added(plugin)
        source = create_source(kind, "s", {})
        assert {"font", "text_scale", "corners", "custom_look", "accent_color"} <= set(source.defaults)
        source.update({**source.settings, "text_scale": 160})
        source.update({**source.settings, "custom_look": "size: 160%"})  # the same look: the same box


def test_enter_never_resets_the_style(app, caplog):  # noqa: F811
    """PC 2026-11-18: a custom look typed and OK'd came back empty. Enter on the focused Reset style button (an
    auto-default button in a dialog) put the whole style back."""
    import logging

    from PySide6.QtWidgets import QPushButton

    from sprigcast.app.settings_dialog import SettingsDialog

    defaults = {"show_date": True, "text_color": "#ffffff", "card_color": "#000000e0", "custom_look": ""}
    dialog = SettingsDialog("Local time", defaults, dict(defaults))
    assert not any(b.autoDefault() for b in dialog.findChildren(QPushButton)
                   if b.text() in ("Reset style", "Pick…", "Browse…"))
    assert dialog.fields["custom_look"].tabChangesFocus()
    dialog.fields["custom_look"].setPlainText("text: #ffd36b\nhide: date")
    with caplog.at_level(logging.INFO, logger="sprigcast.app.settings_dialog"):
        dialog.accept()
    assert dialog.values()["custom_look"] == "text: #ffd36b\nhide: date"
    assert "changed custom_look" in caplog.text
    look = style.resolve(dialog.values(), defaults)
    assert look.text == "#ffd36bff" and not look.shows("date", dialog.values())
