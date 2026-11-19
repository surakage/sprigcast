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
