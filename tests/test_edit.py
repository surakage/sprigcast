import pytest

from sprigcast.core import edit
from sprigcast.core.scene import SceneItem

SIZE = (200, 100)


def corners(item):
    return [(round(x, 6), round(y, 6)) for x, y, _u, _v in item.quad(SIZE)]


def test_hit_test_picks_top_most_and_skips_hidden_and_locked():
    items = [
        SceneItem(source="bg", x=0, y=0),
        SceneItem(source="bg", x=50, y=20),
        SceneItem(source="bg", x=60, y=30, visible=False),
    ]
    sizes = {"bg": SIZE}
    assert edit.hit_test(items, sizes, (100, 50)) == 1
    assert edit.hit_test(items, sizes, (10, 10)) == 0
    items[1].locked = True
    assert edit.hit_test(items, sizes, (100, 50)) == 0
    assert edit.hit_test(items, sizes, (500, 500)) is None


def test_hit_test_respects_rotation():
    item = SceneItem(source="s", x=0, y=0, rotation=90)  # 200x100 turned upright around (100, 50)
    assert edit.contains(item, SIZE, (100, 0))  # inside once rotated
    assert not edit.contains(item, SIZE, (10, 50))  # was inside before rotating


def test_handle_at_finds_nearest_corner():
    item = SceneItem(source="s", x=10, y=10)
    assert edit.handle_at(item, SIZE, (211, 111), radius=5) == 3
    assert edit.handle_at(item, SIZE, (10, 12), radius=5) == 0
    assert edit.handle_at(item, SIZE, (100, 60), radius=5) is None


def test_move_and_snap_to_canvas_edges_and_centre():
    start = SceneItem(source="s", x=100, y=100)
    item = start.model_copy()
    edit.move(item, start, SIZE, (5, 7))
    assert (item.x, item.y) == (105, 107)
    edit.move(item, start, SIZE, (-96, 0), canvas=(1920, 1080), snap=8)
    assert item.x == 0  # left edge snapped to the canvas edge
    edit.move(item, start, SIZE, (758, 387), canvas=(1920, 1080), snap=8)
    assert (item.x + 100, item.y + 50) == (960, 540)  # centre snapped to the canvas centre


def test_scale_from_corner_keeps_opposite_corner_fixed():
    start = SceneItem(source="s", x=10, y=20)
    item = start.model_copy()
    edit.scale_from_corner(item, start, SIZE, corner=3, point=(410, 220))  # drag bottom-right outwards
    assert item.scale == pytest.approx(2.0)
    assert corners(item)[0] == (10, 20)
    edit.scale_from_corner(item, start, SIZE, corner=0, point=(110, 70))  # drag top-left inwards
    assert item.scale == pytest.approx(0.5)
    assert corners(item)[3] == (210, 120)


def test_scale_with_rotation_keeps_anchor():
    start = SceneItem(source="s", x=0, y=0, rotation=30)
    anchor = corners(start)[0]
    item = start.model_copy()
    far = corners(start)[3]
    edit.scale_from_corner(item, start, SIZE, corner=3, point=(far[0] * 1.5, far[1] * 1.5))
    assert corners(item)[0] == pytest.approx(anchor)
    assert item.scale > 1


def test_crop_from_corner_trims_edges_and_keeps_opposite_corner():
    start = SceneItem(source="s", x=0, y=0, scale=2.0)  # 400 x 200 on canvas
    item = start.model_copy()
    edit.crop_from_corner(item, start, SIZE, corner=3, point=(300, 160))  # drag bottom-right inwards
    assert item.crop == (0, 0, 50, 20)
    assert corners(item)[0] == (0, 0)
    item2 = start.model_copy()
    edit.crop_from_corner(item2, start, SIZE, corner=0, point=(40, 20))  # drag top-left inwards
    assert item2.crop == (20, 10, 0, 0)
    assert corners(item2)[3] == (400, 200)


def test_crop_never_removes_the_whole_source():
    start = SceneItem(source="s")
    item = start.model_copy()
    edit.crop_from_corner(item, start, SIZE, corner=3, point=(0, 0))
    assert item.cropped_size(SIZE) == (edit.MIN_CROPPED, edit.MIN_CROPPED)


def test_nudge():
    item = SceneItem(source="s", x=1, y=1)
    edit.nudge(item, -10, 0)
    assert (item.x, item.y) == (-9, 1)


def test_snap_options_and_other_sources():
    size = (100, 50)
    start = SceneItem(source="s", x=0, y=0)
    other = (500.0, 300.0, 200.0, 100.0)  # x, y, w, h
    item = start.model_copy()
    # 6 px left of butting against the other item's left edge (x 400 → right edge at 500)
    edit.move(item, start, size, (394, 600), canvas=(1920, 1080), snap=8, others=[other])
    assert item.x == 400
    item = start.model_copy()
    edit.move(item, start, size, (503, 397), canvas=(1920, 1080), snap=8, others=[other])
    assert (item.x, item.y) == (500, 400)  # left edges line up, top butts against the other's bottom
    item = start.model_copy()
    edit.move(item, start, size, (5, 5), canvas=(1920, 1080), snap=8, edges=False, center=False)
    assert (item.x, item.y) == (5, 5)  # canvas edge snapping switched off
    item = start.model_copy()
    edit.move(item, start, size, (5, 5), canvas=(1920, 1080), snap=8)
    assert (item.x, item.y) == (0, 0)


def test_sides_stretch_or_keep_shape_and_crop():
    item = SceneItem(source="s", x=10, y=10)
    start = item.model_copy()
    # The right side's middle handle (5) of a 200×100 item at (10, 10) sits at (210, 60).
    assert edit.handle_at(item, SIZE, (210, 60), radius=5) == 5
    edit.scale_from_corner(item, start, SIZE, corner=5, point=(410, 60), keep_aspect=False)
    assert item.scale == 2.0 and item.sy == 1.0 and (item.x, item.y) == (10, 10)  # wider, same height
    item = start.model_copy()
    edit.scale_from_corner(item, start, SIZE, corner=5, point=(410, 60), keep_aspect=True)
    assert item.scale == 2.0 and item.scale_y is None
    item = start.model_copy()
    edit.crop_from_corner(item, start, SIZE, corner=7, point=(110, 60))  # pull the bottom side up 50 px
    assert item.crop == (0, 0, 0, 50) and (item.x, item.y) == (10, 10)


def test_resizing_snaps_to_the_canvas_edges_center_and_other_items():
    """User, 2026-11-20: a color resized past the right edge ended 1948 wide on a 1920 canvas."""
    canvas, size = (1920, 1080), (1920, 1080)
    start = SceneItem(source="c", x=0, y=0, scale=0.5)  # 960x540 at the top left
    item = start.model_copy()
    # drag the bottom-right corner to just past the canvas corner: it lands on it, shape kept
    edit.snap_resize(item, start, size, 3, (1935, 1090), canvas, 20)
    assert item.drawn_size(size) == pytest.approx((1920, 1080)) and (item.x, item.y) == (0, 0)
    # the right side alone, near the center line: lands on it
    item = start.model_copy()
    edit.snap_resize(item, start, size, 5, (968, 300), canvas, 20, keep_aspect=False)
    assert item.drawn_size(size)[0] == pytest.approx(960)
    # a free corner snaps each way on its own, to another item's edge
    item = start.model_copy()
    edit.snap_resize(item, start, size, 3, (1205, 693), canvas, 10, keep_aspect=False, center=False,
                     others=[(1200, 700, 100, 100)])
    assert item.drawn_size(size) == pytest.approx((1200, 700))
    # off (snap 0) or out of reach: it follows the mouse
    item = start.model_copy()
    edit.snap_resize(item, start, size, 5, (1500, 300), canvas, 0, keep_aspect=False)
    assert item.drawn_size(size)[0] == pytest.approx(1500)


def test_a_snapped_item_shows_the_lines_it_sits_on_and_its_pin_snaps_to_the_center():
    canvas, size = (1920, 1080), (200, 100)
    start = SceneItem(source="s", x=0, y=0, align="top_left")
    item = start.model_copy()
    edit.move(item, start, size, (955, 300), canvas, snap=10)  # the pin (top left) comes to the center line
    assert item.x == 960
    assert (0, 960.0) in edit.snap_guides(item, size, canvas)
    item.x, item.y = 0, 980  # left edge and bottom edge on the canvas's
    assert set(edit.snap_guides(item, size, canvas)) >= {(0, 0.0), (1, 1080.0)}
