"""Geometry for editing scene items with the mouse: hit-testing, moving, scaling, cropping.

Everything here works in canvas pixels and knows nothing about Qt, so it is easy to test.
Every drag operation takes the item as it was when the drag started (`start`) and writes the
result into the live item, so repeated mouse moves never accumulate rounding drift.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from sprigcast.core.scene import SceneItem

Point = tuple[float, float]
Size = tuple[int, int]

# Corner indices follow SceneItem.quad(): 0 top-left, 1 top-right, 2 bottom-left, 3 bottom-right.
# Side handles (the middle of each edge) follow: 4 left, 5 right, 6 top, 7 bottom.
OPPOSITE = {0: 3, 1: 2, 2: 1, 3: 0, 4: 5, 5: 4, 6: 7, 7: 6}
SIDES = {4: (0, 2), 5: (1, 3), 6: (0, 1), 7: (2, 3)}  # the two corners each side runs between
MIN_SCALE = 0.01
MIN_CROPPED = 2  # source pixels that always stay visible


def _rotate(dx: float, dy: float, degrees: float) -> Point:
    r = math.radians(degrees)
    c, s = math.cos(r), math.sin(r)
    return dx * c - dy * s, dx * s + dy * c


def _corner(item: SceneItem, size: Size, index: int) -> Point:
    """A corner (0-3) or the middle of a side (4-7), in canvas pixels."""
    quad = item.quad(size)
    if index in SIDES:
        a, b = SIDES[index]
        return (quad[a][0] + quad[b][0]) / 2, (quad[a][1] + quad[b][1]) / 2
    x, y, _u, _v = quad[index]
    return x, y


def contains(item: SceneItem, size: Size, point: Point) -> bool:
    quad = item.quad(size)
    if not quad:
        return False
    cx = sum(p[0] for p in quad) / 4
    cy = sum(p[1] for p in quad) / 4
    lx, ly = _rotate(point[0] - cx, point[1] - cy, -item.rotation)
    w, h = item.drawn_size(size)
    return abs(lx) <= w / 2 and abs(ly) <= h / 2


def hit_test(items: Sequence[SceneItem], sizes: dict[str, Size], point: Point) -> int | None:
    """Index of the top-most visible, unlocked item under the point."""
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        size = sizes.get(item.source)
        if item.visible and not item.locked and size and contains(item, size, point):
            return index
    return None


def handle_at(item: SceneItem, size: Size, point: Point, radius: float) -> int | None:
    """Which handle is within `radius` canvas pixels of the point: a corner (0-3), else a side (4-7)."""
    if not item.quad(size):
        return None
    best, best_d = None, radius
    for index in range(8):
        x, y = _corner(item, size, index)
        d = math.hypot(point[0] - x, point[1] - y) + (0.01 if index > 3 else 0)  # corners win ties
        if d <= best_d:
            best, best_d = index, d
    return best


def _pin(item: SceneItem, size: Size, corner: int, anchor: Point) -> None:
    """Translate the item so that the given corner sits exactly on `anchor`."""
    x, y = _corner(item, size, corner)
    item.x += anchor[0] - x
    item.y += anchor[1] - y


def move(
    item: SceneItem,
    start: SceneItem,
    size: Size,
    delta: Point,
    canvas: Size | None = None,
    snap: float = 0.0,
    *,
    edges: bool = True,
    center: bool = True,
    others: list[tuple[float, float, float, float]] = (),
) -> None:
    """Move by `delta`, then snap within `snap` canvas pixels (like OBS):

    edges   the item's edges to the canvas edges
    center  the item's centre to the canvas centre lines
    others  the item's edges to the edges of other items, given as (x, y, w, h) rectangles
    """
    item.x, item.y = round(start.x + delta[0]), round(start.y + delta[1])  # whole pixels stay crisp
    if canvas is None or snap <= 0 or item.rotation % 360 != 0:
        return
    w, h = item.drawn_size(size)
    for axis, (pos, length, limit) in enumerate(((item.x, w, canvas[0]), (item.y, h, canvas[1]))):
        options = []
        if edges:
            options += [0 - pos, limit - (pos + length)]
        if center:
            options.append(limit / 2 - (pos + length / 2))
        for rect in others:
            o, ol = rect[axis], rect[axis + 2]
            for target in (o, o + ol):  # line up with, or butt against, the other item's edges
                options += [target - pos, target - (pos + length)]
        if not options:
            continue
        nearest = min(options, key=abs)
        if abs(nearest) <= snap:
            if axis == 0:
                item.x += nearest
            else:
                item.y += nearest


def scale_from_corner(item: SceneItem, start: SceneItem, size: Size, corner: int, point: Point,
                      keep_aspect: bool = True) -> None:
    """Resize by dragging a corner (0-3) or a side (4-7); the opposite corner or side stays put.

    keep_aspect: the shape stays the same (one scale). Off, the item stretches: a corner sets
    width and height freely, a side changes only its own direction.
    """
    anchor_index = OPPOSITE[corner]
    anchor = _corner(start, size, anchor_index)
    lx, ly = _rotate(point[0] - anchor[0], point[1] - anchor[1], -start.rotation)
    cw, ch = start.cropped_size(size)
    if not cw or not ch:
        return
    item.x, item.y, item.crop, item.rotation = start.x, start.y, start.crop, start.rotation
    sx, sy = max(MIN_SCALE, abs(lx) / cw), max(MIN_SCALE, abs(ly) / ch)
    if corner in (4, 5):  # left or right side: only the width follows the mouse
        sy = start.sy if not keep_aspect else sx * start.sy / start.scale
    elif corner in (6, 7):
        sx = start.scale if not keep_aspect else sy * start.scale / start.sy
    elif keep_aspect:
        k = max(sx / start.scale, sy / start.sy)
        sx, sy = start.scale * k, start.sy * k
    item.scale = sx
    item.scale_y = None if abs(sy - sx) < 1e-6 else sy
    _pin(item, size, anchor_index, anchor)


def crop_from_corner(item: SceneItem, start: SceneItem, size: Size, corner: int, point: Point) -> None:
    """Crop by dragging a corner (its two edges move) or a side (only that edge moves).

    The opposite corner or side stays put, so a camera can be cut to any shape."""
    anchor_index = OPPOSITE[corner]
    anchor = _corner(start, size, anchor_index)
    lx, ly = _rotate(point[0] - anchor[0], point[1] - anchor[1], -start.rotation)
    sw, sh = size
    left, top, right, bottom = start.crop
    want_w = abs(lx) / start.scale  # visible width in source pixels
    want_h = abs(ly) / start.sy
    if corner in (0, 2, 4):  # left edge moves
        left = int(round(min(max(0, sw - right - want_w), sw - right - MIN_CROPPED)))
    elif corner in (1, 3, 5):  # right edge moves
        right = int(round(min(max(0, sw - left - want_w), sw - left - MIN_CROPPED)))
    if corner in (0, 1, 6):  # top edge moves
        top = int(round(min(max(0, sh - bottom - want_h), sh - bottom - MIN_CROPPED)))
    elif corner in (2, 3, 7):  # bottom edge moves
        bottom = int(round(min(max(0, sh - top - want_h), sh - top - MIN_CROPPED)))
    item.x, item.y, item.scale, item.scale_y, item.rotation = (start.x, start.y, start.scale, start.scale_y,
                                                              start.rotation)
    item.crop = (left, top, right, bottom)
    _pin(item, size, anchor_index, anchor)


def nudge(item: SceneItem, dx: float, dy: float) -> None:
    item.x += dx
    item.y += dy
