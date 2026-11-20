"""Geometry for editing scene items with the mouse: hit-testing, moving, scaling, cropping.

Everything here works in canvas pixels and knows nothing about Qt, so it is easy to test.
Every drag operation takes the item as it was when the drag started (`start`) and writes the
result into the live item, so repeated mouse moves never accumulate rounding drift.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from sprigcast.core.scene import SceneItem, align_share

Point = tuple[float, float]
Size = tuple[int, int]

# Corner indices follow SceneItem.box(): 0 top-left, 1 top-right, 2 bottom-left, 3 bottom-right.
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
    box = item.box(size)
    if index in SIDES:
        a, b = SIDES[index]
        return (box[a][0] + box[b][0]) / 2, (box[a][1] + box[b][1]) / 2
    return box[index]


def contains(item: SceneItem, size: Size, point: Point) -> bool:
    box = item.box(size)
    if not box:
        return False
    cx = sum(p[0] for p in box) / 4
    cy = sum(p[1] for p in box) / 4
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
    if not item.box(size):
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


def _tidy(item: SceneItem) -> None:
    """Drop float noise from a drag (PC test, 2026-11-20: a snapped x of 959.9999999999995 was saved and shown).
    Places and boxes to a millionth of a pixel, scales to a billionth: nothing you could see moves."""
    item.x, item.y = round(item.x, 6) + 0.0, round(item.y, 6) + 0.0
    item.scale = round(item.scale, 9)
    if item.scale_y is not None:
        item.scale_y = round(item.scale_y, 9)
    if item.bounded:
        item.bounds_size = (round(item.bounds_size[0], 6), round(item.bounds_size[1], 6))


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
    center  the item's center (and its pin point) to the canvas center lines
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
            pin = pos + length * align_share(item.align)[axis]  # the pin point, onto the center lines too
            options.append(limit / 2 - pin)
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
    _tidy(item)


def scale_from_corner(item: SceneItem, start: SceneItem, size: Size, corner: int, point: Point,
                      keep_aspect: bool = True) -> None:
    """Resize by dragging a corner (0-3) or a side (4-7); the opposite corner or side stays put.

    keep_aspect: the shape stays the same (one scale). Off, the item stretches: a corner sets
    width and height freely, a side changes only its own direction.
    """
    anchor_index = OPPOSITE[corner]
    anchor = _corner(start, size, anchor_index)
    lx, ly = _rotate(point[0] - anchor[0], point[1] - anchor[1], -start.rotation)
    if start.bounded:  # the box is what resizes; the picture refits inside it
        bw, bh = start.bounds_size
        w, h = max(2.0, abs(lx)), max(2.0, abs(ly))
        if corner in (4, 5):
            h = w * bh / bw if keep_aspect else bh
        elif corner in (6, 7):
            w = h * bw / bh if keep_aspect else bw
        elif keep_aspect:
            k = max(w / bw, h / bh)
            w, h = bw * k, bh * k
        item.x, item.y, item.crop, item.rotation = start.x, start.y, start.crop, start.rotation
        item.bounds_size = (w, h)
        _pin(item, size, anchor_index, anchor)
        _tidy(item)
        return
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
    _tidy(item)


def snap_resize(
    item: SceneItem,
    start: SceneItem,
    size: Size,
    corner: int,
    point: Point,
    canvas: Size | None,
    snap: float,
    keep_aspect: bool = True,
    *,
    edges: bool = True,
    center: bool = True,
    others: list[tuple[float, float, float, float]] = (),
) -> None:
    """Resize like `scale_from_corner`, then pull the dragged edge onto a line within `snap` canvas pixels:
    the canvas edges, its center lines, other items' edges (user, 2026-11-20: resizing didn't snap).

    A corner that keeps its shape (or a side) lands on the nearer line of the two; a free corner snaps each
    way on its own. Rotated items don't snap, as with moving."""
    scale_from_corner(item, start, size, corner, point, keep_aspect)
    if canvas is None or snap <= 0 or start.rotation % 360 != 0:
        return
    moved = _corner(item, size, corner)
    anchor = _corner(start, size, OPPOSITE[corner])
    axes = (0,) if corner in (4, 5) else (1,) if corner in (6, 7) else (0, 1)
    pulls: dict[int, float] = {}
    for axis in axes:
        lines = []
        if edges:
            lines += [0, canvas[axis]]
        if center:
            lines.append(canvas[axis] / 2)
        for rect in others:
            lines += [rect[axis], rect[axis] + rect[axis + 2]]
        if lines:
            nearest = min((line - moved[axis] for line in lines), key=abs)
            if abs(nearest) <= snap and moved[axis] != anchor[axis]:
                pulls[axis] = nearest
    if not pulls:
        return
    if len(axes) == 2 and not keep_aspect:  # a free corner: each way on its own
        target = (moved[0] + pulls.get(0, 0.0), moved[1] + pulls.get(1, 0.0))
    else:  # one size for both ways: grow or shrink toward the nearer line
        axis = min(pulls, key=lambda a: abs(pulls[a]))
        f = (moved[axis] + pulls[axis] - anchor[axis]) / (moved[axis] - anchor[axis])
        target = (anchor[0] + (moved[0] - anchor[0]) * f, anchor[1] + (moved[1] - anchor[1]) * f)
    scale_from_corner(item, start, size, corner, target, keep_aspect)


def crop_from_corner(item: SceneItem, start: SceneItem, size: Size, corner: int, point: Point) -> None:
    """Crop by dragging a corner (its two edges move) or a side (only that edge moves).

    The opposite corner or side stays put, so a camera can be cut to any shape."""
    anchor_index = OPPOSITE[corner]
    anchor = _corner(start, size, anchor_index)
    lx, ly = _rotate(point[0] - anchor[0], point[1] - anchor[1], -start.rotation)
    sw, sh = size
    left, top, right, bottom = start.crop
    k_x, k_y = start.picture_scale(size)
    want_w = abs(lx) / k_x  # visible width in source pixels
    want_h = abs(ly) / k_y
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


def cover(size: tuple[int, int], canvas: tuple[int, int]) -> tuple[tuple[int, int, int, int], float]:
    """(crop, scale) that make a `size` picture cover a `canvas` exactly, cropping evenly what sticks out."""
    w, h = max(1, size[0]), max(1, size[1])
    scale = max(canvas[0] / w, canvas[1] / h)
    # Rounded down: a pixel or two past the edge rather than a hairline gap.
    cx = max(0, math.floor((w - canvas[0] / scale) / 2))
    cy = max(0, math.floor((h - canvas[1] / scale) / 2))
    return (cx, cy, cx, cy), scale


def snap_guides(item: SceneItem, size: Size, canvas: Size, *, edges: bool = True, center: bool = True,
                others: list[tuple[float, float, float, float]] = ()) -> list[tuple[int, float]]:
    """The lines the item now sits on, as (axis, position): drawn while dragging so you see it snapped."""
    if item.rotation % 360 != 0:
        return []
    w, h = item.drawn_size(size)
    found = []
    for axis, (pos, length) in enumerate(((item.x, w), (item.y, h))):
        mine = (pos, pos + length / 2, pos + length, pos + length * align_share(item.align)[axis])
        lines = ([0, canvas[axis]] if edges else []) + ([canvas[axis] / 2] if center else [])
        for rect in others:
            lines += [rect[axis], rect[axis] + rect[axis + 2]]
        for line in dict.fromkeys(lines):
            if any(abs(line - v) < 0.5 for v in mine):
                found.append((axis, float(line)))
    return found
