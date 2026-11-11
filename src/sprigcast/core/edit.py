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
OPPOSITE = {0: 3, 1: 2, 2: 1, 3: 0}
MIN_SCALE = 0.01
MIN_CROPPED = 2  # source pixels that always stay visible


def _rotate(dx: float, dy: float, degrees: float) -> Point:
    r = math.radians(degrees)
    c, s = math.cos(r), math.sin(r)
    return dx * c - dy * s, dx * s + dy * c


def _corner(item: SceneItem, size: Size, index: int) -> Point:
    x, y, _u, _v = item.quad(size)[index]
    return x, y


def contains(item: SceneItem, size: Size, point: Point) -> bool:
    quad = item.quad(size)
    if not quad:
        return False
    cx = sum(p[0] for p in quad) / 4
    cy = sum(p[1] for p in quad) / 4
    lx, ly = _rotate(point[0] - cx, point[1] - cy, -item.rotation)
    w, h = (d * item.scale for d in item.cropped_size(size))
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
    """Which corner handle (0-3) is within `radius` canvas pixels of the point, if any."""
    best, best_d = None, radius
    for index, (x, y, _u, _v) in enumerate(item.quad(size)):
        d = math.hypot(point[0] - x, point[1] - y)
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
    w, h = (d * item.scale for d in item.cropped_size(size))
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


def scale_from_corner(item: SceneItem, start: SceneItem, size: Size, corner: int, point: Point) -> None:
    """Uniform scale by dragging a corner; the opposite corner stays where it was."""
    anchor_index = OPPOSITE[corner]
    anchor = _corner(start, size, anchor_index)
    lx, ly = _rotate(point[0] - anchor[0], point[1] - anchor[1], -start.rotation)
    cw, ch = start.cropped_size(size)
    if not cw or not ch:
        return
    item.x, item.y, item.crop, item.rotation = start.x, start.y, start.crop, start.rotation
    item.scale = max(MIN_SCALE, abs(lx) / cw, abs(ly) / ch)
    _pin(item, size, anchor_index, anchor)


def crop_from_corner(item: SceneItem, start: SceneItem, size: Size, corner: int, point: Point) -> None:
    """Crop by dragging a corner: the two edges at that corner move, the opposite corner stays put."""
    anchor_index = OPPOSITE[corner]
    anchor = _corner(start, size, anchor_index)
    lx, ly = _rotate(point[0] - anchor[0], point[1] - anchor[1], -start.rotation)
    sw, sh = size
    left, top, right, bottom = start.crop
    want_w = abs(lx) / start.scale  # visible width in source pixels
    want_h = abs(ly) / start.scale
    if corner in (0, 2):  # left edge moves
        left = int(round(min(max(0, sw - right - want_w), sw - right - MIN_CROPPED)))
    else:  # right edge moves
        right = int(round(min(max(0, sw - left - want_w), sw - left - MIN_CROPPED)))
    if corner in (0, 1):  # top edge moves
        top = int(round(min(max(0, sh - bottom - want_h), sh - bottom - MIN_CROPPED)))
    else:  # bottom edge moves
        bottom = int(round(min(max(0, sh - top - want_h), sh - top - MIN_CROPPED)))
    item.x, item.y, item.scale, item.rotation = start.x, start.y, start.scale, start.rotation
    item.crop = (left, top, right, bottom)
    _pin(item, size, anchor_index, anchor)


def nudge(item: SceneItem, dx: float, dy: float) -> None:
    item.x += dx
    item.y += dy
