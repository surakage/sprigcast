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
