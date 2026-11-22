"""Sweep 13 (2026-11-19): widgets that ran Python for every event now run it only for the events they want."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_preview_watches_pinches_only_while_editable(app):
    from sprigcast.app.preview import Preview

    p = Preview("horizontal", "Horizontal", 16 / 9)
    assert p._pinch is not None  # Build's view
    p.editable = False  # Live's
    assert p._pinch is None
    p.editable = True
    p.twin = True
    assert p._pinch is None
    p.twin = False
    assert p._pinch is not None
    assert "event" not in Preview.__dict__


def test_place_rows_have_their_own_drag_handlers(app):
    from sprigcast.app import live_stage

    assert issubclass(live_stage._PlaceButton, live_stage._PlaceDrag)
    assert "eventFilter" not in live_stage.LiveSide.__dict__


def test_number_field_lights_its_edge_without_a_filter(app):
    from sprigcast.app.inspector_fields import NumberField, _NumberBox

    field = NumberField("Size", 0, 100)
    assert isinstance(field.box, _NumberBox) and field.box.frame is field
    field.box._light(True)
    assert field.property("focused") is True


def test_cards_draw_their_shadow_from_a_picture(app):
    from sprigcast.app.card import TextDialog

    d = TextDialog("New garden", "Name")
    assert d._card.graphicsEffect() is None
    d.show()
    app.processEvents()
    d.grab()  # paints: the shadow picture under the card
    d.close()


def test_garden_check_page_is_made_inside_its_pop_up(app):
    from sprigcast.app.garden_check import GardenSnapshot, garden_check_popup

    pop = garden_check_popup(lambda: GardenSnapshot(), [], None)
    assert pop.garden.parent() is pop.stack and pop.stack.currentWidget() is pop.garden
    pop.timer.stop()
    pop.deleteLater()


def test_card_shadow_is_one_small_picture_for_every_size(app):
    from sprigcast.app import theme

    theme._shadow_pictures.clear()
    picture, left, top, right, bottom = theme._shadow_picture(1.0, theme.PANEL)
    assert picture.width() == theme._SHADOW_CARD + left + right < 260
    for w, h in ((600, 420), (700, 900), (100, 60)):
        pieces = theme.shadow_pieces(w, h, left, top, right, bottom)
        assert len(pieces) == (9 if h > 2 * theme.SHADOW_CORNER else 6)  # a short card has no middle band
        xs = sorted({(t[0], t[2]) for t, _ in pieces})
        assert xs[0][0] == -left and xs[-1][0] + xs[-1][1] == w + right  # covers the card and its shadow, side to side
        assert all(a[0] + a[1] == b[0] for a, b in zip(xs, xs[1:]))  # with no gap or overlap
        corner = pieces[0]
        assert corner[0][2:] == corner[1][2:]  # corners are copied, not stretched
    assert len(theme._shadow_pictures) == 1


def test_a_cards_whole_shadow_is_put_together_once_per_size(app):
    from sprigcast.app import theme

    theme._shadows_put_together.clear()
    picture, left, top = theme.card_shadow(600, 420, 1.0, theme.PANEL)
    assert picture.width() > 600 and picture.height() > 420 and left < 0 and top < 0
    assert theme.card_shadow(600, 420, 1.0, theme.PANEL)[0] is picture  # kept
    for extra in range(theme.SHADOWS_KEPT + 3):  # a card that grows: only the last few sizes stay
        theme.card_shadow(600, 430 + extra, 1.0, theme.PANEL)
    assert len(theme._shadows_put_together) == theme.SHADOWS_KEPT


def test_the_shadow_picture_is_made_before_the_first_pop_up_opens(app):
    from PySide6.QtWidgets import QWidget

    from sprigcast.app import theme

    theme._shadow_pictures.clear()
    window = QWidget()
    theme.warm_card_shadow(window)
    assert (window.devicePixelRatioF(), theme.PANEL) in theme._shadow_pictures  # what paint_card_shadow asks for
    theme.warm_card_shadow(None)  # a warm-up never raises


def test_garden_check_parts_are_made_as_children_of_where_they_end_up(app):
    """Made on their own and put in after, each part got the page's look on the spot: one wait each while live."""
    from PySide6.QtWidgets import QWidget

    from sprigcast.app import garden_check
    from sprigcast.app.live_stage import Elided

    home = QWidget()
    tile, value = garden_check._tile("CPU", home)
    assert tile.parent() is home and value.parent() is tile
    assert garden_check._section("Stream health", home).parent() is home
    row = garden_check.GardenCheckPage._row("Twitch", "6000 kbps", "good", "steady", first=True, parent=home)
    assert row.parent() is home and all(part.parent() is row for part in row.parts)
    assert Elided("words", home).parent() is home
    page = garden_check.GardenCheckPage(lambda: garden_check.GardenSnapshot(checks=[("Mic heard", True, "")]), [])
    assert page.checks.itemAt(0).widget().parent() is page._checked  # in its "Things we checked" card
    assert page.overload_bar.parent().parent() is page._now and page.done.parent() is page  # in "Stream health"
    assert page.close_button.parent() is page


def test_also_in_is_one_pill_whatever_the_count():
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.inspector_fields import UsedIn

    QApplication.instance() or QApplication([])
    picked = []
    used = UsedIn(picked.append, word="bloom")
    used.set_blooms([])
    assert used.text() == "Only in this bloom so far." and not used.button.isVisibleTo(used)
    used.set_blooms([("b1", "Gameplay")])
    assert used.button.text().startswith("Also in Gameplay")
    used.set_blooms([(f"b{i}", f"Bloom {i}") for i in range(5)])
    assert used.button.text().startswith("Also in 5 blooms") and 'href="b4"' in used.text()
