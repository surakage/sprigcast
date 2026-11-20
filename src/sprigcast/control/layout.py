"""Where everything in a bloom or sprig sits, worked out from the garden alone (no drawing): layout.get.

Each item is told with its place as saved (x, y, scales, rotation, crop, bounds, alignment), its source's own
size, and its box on the canvas it ends up on. A sprig is drawn as one picture and then placed, so what's
inside it is carried through the sprig's own place (crop, scale, bounds, rotation) to the bloom's canvas.

No Qt and no renderer here: sizes come in as a plain dict (what the renderer last said each source is).
"""

from __future__ import annotations

import math
from typing import Any, Callable

from sprigcast.core.scene import PART_KIND, Scene, SceneCollection, SceneItem

Point = tuple[float, float]
Carry = Callable[[float, float], Point]  # a point in a layout's own pixels -> the top canvas's pixels

MOST_DEEP = 8  # sprigs inside sprigs: deeper than this isn't followed (a loop can't hang the answer)


def _r(value: float) -> float:
    return round(float(value), 2)


def source_size(col: SceneCollection, item: SceneItem, canvas_id: str,
                sizes: dict[str, tuple[int, int]]) -> tuple[int, int] | None:
    """A source's own size in pixels: a sprig is as big as the canvas of the layout it shows here; anything else is
    what the renderer last said, or else the width and height in its settings (a web page). None: not known."""
    source = next((s for s in col.sources if s.id == item.source), None)
    if source is None:
        return None
    if source.kind == PART_KIND:
        part = col.part_of(source)
        if part is None:
            return None
        shape = col.shape_for(part, canvas_id)
        canvas = next((c for c in col.canvases if c.id == shape.canvas), None)
        return (canvas.width, canvas.height) if canvas is not None else None
    known = sizes.get(source.id)
    if known and known[0] > 0 and known[1] > 0:
        return int(known[0]), int(known[1])
    w, h = source.settings.get("width"), source.settings.get("height")
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in (w, h)):
        return int(w), int(h)
    return None


def carry_into(item: SceneItem, size: tuple[int, int], outer: Carry) -> Carry:
    """How a point inside a placed sprig (in the sprig's own pixels) gets to the top canvas: the sprig's crop,
    the scale its picture is drawn at, where the picture sits in its box, its turn around the pin, then `outer`."""
    left, top, _right, _bottom = item.crop
    sx, sy = item.picture_scale(size)
    rx, ry, _pw, _ph = item.picture_rect(size)
    pin_x, pin_y = item.pin(size)
    rad = math.radians(item.rotation)
    cos_r, sin_r = math.cos(rad), math.sin(rad)

    def carry(x: float, y: float) -> Point:
        px, py = item.x + rx + (x - left) * sx, item.y + ry + (y - top) * sy
        dx, dy = px - pin_x, py - pin_y
        return outer(pin_x + dx * cos_r - dy * sin_r, pin_y + dx * sin_r + dy * cos_r)

    return carry


def bounding(corners: list[Point]) -> dict[str, float]:
    xs, ys = [p[0] for p in corners], [p[1] for p in corners]
    return {"x": _r(min(xs)), "y": _r(min(ys)), "width": _r(max(xs) - min(xs)), "height": _r(max(ys) - min(ys))}


def describe(col: SceneCollection, layout: Scene, sizes: dict[str, tuple[int, int]],
             name_of: Callable[[str], str], key_of: Callable[[str], str], carry: Carry | None = None,
             shown: bool = True, path: tuple[str, ...] = (), depth: int = 0) -> list[dict[str, Any]]:
    """The items of one layout, top of the stack first, each with what's inside it when it's a sprig."""
    carry = carry or (lambda x, y: (x, y))
    sources = {s.id: s for s in col.sources}
    out = []
    for item in reversed(layout.items):
        source = sources.get(item.source)
        if source is None:
            continue
        part = col.part_of(source)
        size = source_size(col, item, layout.canvas, sizes)
        row: dict[str, Any] = {
            "id": key_of(item.source), "name": name_of(item.source),
            "kind": "sprig" if part is not None else source.kind, "sprig": part is not None,
            "visible": item.visible, "shown": shown and item.visible, "locked": item.locked,
            "x": _r(item.x), "y": _r(item.y), "scale_x": round(item.scale, 4), "scale_y": round(item.sy, 4),
            "rotation": _r(item.rotation), "align": item.align,
            "crop": dict(zip(("left", "top", "right", "bottom"), item.crop)),
            "bounds": {"mode": item.bounds, "width": _r(item.bounds_size[0]), "height": _r(item.bounds_size[1]),
                       "align": item.bounds_align},
            "source_size": {"width": size[0], "height": size[1]} if size else None,
            "size": None, "corners": None, "box": None,
        }
        if path:
            row["in"] = list(path)
        corners = item.box(size) if size else []
        if corners:
            w, h = item.drawn_size(size)
            on_canvas = [carry(x, y) for x, y in corners]
            row["size"] = {"width": _r(w), "height": _r(h)}
            row["corners"] = [[_r(x), _r(y)] for x, y in on_canvas]
            row["box"] = bounding(on_canvas)
        if part is not None:
            inner = col.shape_for(part, layout.canvas)
            if size and depth < MOST_DEEP:
                row["items"] = describe(col, inner, sizes, name_of, key_of, carry_into(item, size, carry),
                                        shown and item.visible, (*path, part.name), depth + 1)
            else:
                row["items"] = []
        out.append(row)
    return out


def flatten(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only the things themselves (no sprigs), top of the stack first, as they end up on the canvas."""
    out = []
    for row in rows:
        if row["sprig"]:
            out += flatten(row.get("items", []))
        else:
            out.append(row)
    return out


def overlaps(rows: list[dict[str, Any]], least: float = 1.0) -> list[dict[str, Any]]:
    """Pairs of shown things whose boxes on the canvas share more than `least` pixels each way."""
    flat = [r for r in flatten(rows) if r["shown"] and r["box"]]
    out = []
    for i, a in enumerate(flat):
        for b in flat[i + 1:]:
            ab, bb = a["box"], b["box"]
            w = min(ab["x"] + ab["width"], bb["x"] + bb["width"]) - max(ab["x"], bb["x"])
            h = min(ab["y"] + ab["height"], bb["y"] + bb["height"]) - max(ab["y"], bb["y"])
            if w > least and h > least:
                out.append({"a": a["id"], "a_name": a["name"], "a_in": a.get("in", []),
                            "b": b["id"], "b_name": b["name"], "b_in": b.get("in", []),
                            "box": {"x": _r(max(ab["x"], bb["x"])), "y": _r(max(ab["y"], bb["y"])),
                                    "width": _r(w), "height": _r(h)}})
    return out
