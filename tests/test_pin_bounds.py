import math

from sprigcast.core import edit
from sprigcast.core.scene import SceneItem

SIZE = (200, 100)


def _xy(quad):
    return [(round(x, 3), round(y, 3)) for x, y, *_ in quad]


def test_default_pin_is_centre_and_rotation_turns_around_it():
    item = SceneItem(source="s", x=100, y=100)
    assert item.pin(SIZE) == (200, 150)
    item.rotation = 90
    xs = [p[0] for p in item.quad(SIZE)]
    ys = [p[1] for p in item.quad(SIZE)]
    assert (min(xs) + max(xs)) / 2 == 200 and math.isclose((min(ys) + max(ys)) / 2, 150)
    assert item.pin(SIZE) == (200, 150)


def test_top_left_pin_turns_around_the_corner():
    item = SceneItem(source="s", x=100, y=100, align="top_left", rotation=90)
    quad = _xy(item.quad(SIZE))
    assert quad[0] == (100, 100)  # the pinned corner stays
    assert quad[1] == (100, 300)  # the top edge now points down


def test_changing_the_pin_keeps_the_picture_in_place():
    item = SceneItem(source="s", x=50, y=40, rotation=30)
    before = _xy(item.quad(SIZE))
    item.set_align(SIZE, "bottom_right")
    assert [(round(a, 2), round(b, 2)) for a, b in _xy(item.quad(SIZE))] == \
        [(round(a, 2), round(b, 2)) for a, b in before]
    item.set_pin(SIZE, 500, 500)
    assert tuple(round(v, 6) for v in item.pin(SIZE)) == (500, 500)


def test_bounds_fit_fill_stretch():
    item = SceneItem(source="s", bounds="fit", bounds_size=(400, 400), align="top_left")
    assert item.drawn_size(SIZE) == (400, 400)
    quad = _xy(item.quad(SIZE))
    assert quad[0] == (0, 100) and quad[3] == (400, 300)  # 400x200, centred in the box
    item.bounds_align = "top"
    assert _xy(item.quad(SIZE))[0] == (0, 0)
    item.bounds = "fill"
    item.bounds_align = "center"
    quad = item.quad(SIZE)
    assert _xy(quad)[0] == (0, 0) and _xy(quad)[3] == (400, 400)  # cut to the box
    assert math.isclose(quad[0][2], 0.25) and math.isclose(quad[3][2], 0.75)  # the middle half of the width
    item.bounds = "stretch"
    quad = item.quad(SIZE)
    assert _xy(quad)[3] == (400, 400) and quad[3][2] == 1.0


def test_dragging_a_bounded_item_resizes_its_box():
    start = SceneItem(source="s", bounds="fit", bounds_size=(400, 400), align="top_left")
    item = start.model_copy()
    edit.scale_from_corner(item, start, SIZE, 3, (800, 600), keep_aspect=False)
    assert item.bounds_size == (800, 600) and (item.x, item.y) == (0, 0)
    assert edit.contains(item, SIZE, (10, 10))  # the empty part of the box still picks it


def test_old_items_still_load():
    item = SceneItem.model_validate({"source": "s", "x": 1, "y": 2})
    assert item.align == "center" and item.bounds == "off" and not item.bounded


def test_obs_alignment_bits_round_trip():
    from sprigcast.control.api import BOUNDS_FROM_OBS, BOUNDS_TO_OBS, _from_obs_align, _obs_align
    from sprigcast.core.scene import ALIGNS

    assert _obs_align("top_left") == 5 and _obs_align("center") == 0
    assert all(_from_obs_align(_obs_align(a)) == a for a in ALIGNS)
    assert all(BOUNDS_FROM_OBS[BOUNDS_TO_OBS[b]] == b for b in BOUNDS_TO_OBS)
