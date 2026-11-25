"""The Move transition: what is in both blooms glides from where it was to where it goes.

Each bloom is laid out as a flat list of pieces (sources with their corners on the canvas), opening up
sprigs that fill the canvas one to one so the sources inside them can move on their own. Pieces are
paired by source id, then by name. Every frame of the change is then one list of quads drawn straight
onto the canvas: no extra textures and no mixing pass, so it costs less than a fade.

Pure Python and no GL here, so it can be tested on its own (the compositor draws the result).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from sprigcast.core.scene import MOVE_EASINGS as EASINGS
from sprigcast.core.scene import MOVE_UNMATCHED as UNMATCHED

__all__ = ["EASINGS", "UNMATCHED", "Draw", "Piece", "between", "both", "ease", "flatten", "frame", "pair"]
SCALE_TO = 0.8  # "scale": what is leaving shrinks to this, what is arriving grows from it
SLIDE_EDGE = 0.25  # "slide": how far past the nearest edge, as a share of the canvas, it starts or ends
MAX_DEPTH = 8  # sprigs inside sprigs opened up at most this deep


def ease(name: str, t: float) -> float:
    """Where a change is (0 .. 1, "spring" overshoots a little before settling) at time t (0 .. 1)."""
    t = min(1.0, max(0.0, t))
    if name == "linear":
        return t
    if name == "ease_in":
        return t * t * t
    if name == "ease_out":
        return 1.0 - (1.0 - t) ** 3
    if name == "spring":  # back-out: passes its spot by about 8 % and comes back
        c = 1.70158
        u = t - 1.0
        return 1.0 + (c + 1.0) * u * u * u + c * u * u
    # smooth: cubic ease-in-out (flat at both ends, so nothing jumps at the start or the end)
    return 4 * t * t * t if t < 0.5 else 1.0 - (-2.0 * t + 2.0) ** 3 / 2


def _smooth(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3.0 - 2.0 * t)


@dataclass
class Piece:
    """One thing drawn on the canvas: a source (or a sprig drawn whole) and its corners."""

    key: str  # source id: what it is paired by
    name: str  # source name, lower case: paired by this when the ids differ
    source: str  # what to draw: a source id (its texture) or a sprig placement (its sprig texture)
    canvas: str  # the canvas id the sprig textures are looked up for
    quad: list  # 4 x (x, y, u, v): top-left, top-right, bottom-left, bottom-right
    opacity: float = 1.0
    holds: frozenset = frozenset()  # a sprig drawn whole: the sources inside it, however deep
    part: str = ""  # a sprig drawn whole: the sprig it shows (two placements of one sprig are the same picture)


@dataclass
class Draw:
    source: str
    canvas: str
    quad: list
    opacity: float


def _is_whole_canvas(quad: list, size: tuple[float, float], tol: float = 0.5) -> bool:
    """True when a sprig's quad covers the canvas exactly, one pixel to one pixel, uncropped."""
    w, h = size
    want = ((0.0, 0.0, 0.0, 0.0), (w, 0.0, 1.0, 0.0), (0.0, h, 0.0, 1.0), (w, h, 1.0, 1.0))
    for (x, y, u, v), (wx, wy, wu, wv) in zip(quad, want):
        if abs(x - wx) > tol or abs(y - wy) > tol or abs(u - wu) > 1e-4 or abs(v - wv) > 1e-4:
            return False
    return True


def flatten(scene, collection, sizes: dict, part_size: Callable[[str, str], tuple | None],
            view: Callable, plain: Callable[[str], bool], canvas_size: tuple[int, int],
            hidden: frozenset = frozenset(), keep: frozenset = frozenset()) -> list[Piece]:
    """A bloom as the pieces it draws, bottom first.

    part_size(source id, canvas id): the size of a sprig placement's texture (None: not drawn yet).
    view(layout, placement source): (the layout as it shows now, its per-item fades or None).
    plain(source id): True when the source has no video filters (a sprig with filters is drawn whole).
    hidden: sources left out wherever they are (screenshots).
    keep: sprigs (part ids) drawn whole even when they fill the canvas (the other bloom draws them whole: see
    both).
    """
    sources = {s.id: s for s in collection.sources}
    out: list[Piece] = []

    def inside(part, canvas_id: str, depth: int, seen: frozenset) -> frozenset:
        found: set[str] = set()
        for item in collection.shape_for(part, canvas_id).items:
            found.add(item.source)
            inner = collection.part_of(sources[item.source]) if item.source in sources else None
            if inner is not None and depth < MAX_DEPTH and inner.id not in seen:
                found |= inside(inner, canvas_id, depth + 1, seen | {inner.id})
        return frozenset(found)

    def walk(layout, opacity: float, fades, depth: int, seen: frozenset) -> None:
        for item in layout.items:
            if not item.visible or item.source in hidden:
                continue
            source = sources.get(item.source)
            if source is None:
                continue
            alpha = opacity * (fades.get(item.source, 1.0) if fades else 1.0)
            part = collection.part_of(source)
            if part is not None:
                size = part_size(item.source, layout.canvas)
                if not size:
                    continue
                quad = item.quad(size)
                if not quad:
                    continue
                shape = collection.shape_for(part, layout.canvas)
                if (depth < MAX_DEPTH and part.id not in seen and plain(item.source) and part.id not in keep
                        and tuple(size) == tuple(canvas_size) and _is_whole_canvas(quad, canvas_size)):
                    inner, inner_fades = view(shape, source)
                    walk(inner, alpha, inner_fades, depth + 1, seen | {part.id})
                    continue
                out.append(Piece(item.source, source.name.casefold(), item.source, layout.canvas, quad, alpha,
                                 inside(part, layout.canvas, depth, seen | {part.id}), part.id))
                continue
            size = sizes.get(item.source)
            if not size:
                continue
            quad = item.quad(size)
            if quad:
                out.append(Piece(item.source, source.name.casefold(), item.source, layout.canvas, quad, alpha))

    walk(scene, 1.0, None, 0, frozenset())
    return out


def both(lay_out: Callable[[frozenset], tuple[list, list]]) -> tuple[list, list]:
    """The two blooms' pieces, with a sprig drawn whole in one bloom drawn whole in the other too.

    A sprig that fills the canvas is opened up into its pieces; the same sprig smaller in the other bloom is one
    piece. Paired with nothing, the full-screen camera on one bloom and the shrunk one on the other just faded
    (user 2026-11-25: "i have it on fullscreen on one bloom and shrunk it on another but it didnt do the
    animation move"). The same sprig counts however it is placed: the vertical Webcam is placed once per bloom
    (PC 2026-11-25). lay_out(keep) -> (old pieces, new pieces)."""
    keep: frozenset = frozenset()
    for _ in range(MAX_DEPTH):
        old, new = lay_out(keep)
        whole = frozenset(p.part for p in (*old, *new) if p.part)
        if whole <= keep:
            break
        keep |= whole
    return old, new


def same(a: Piece, b: Piece) -> bool:
    """The same picture: one source, or one sprig placed twice. It glides as one, its crop sliding."""
    return a.source == b.source or bool(a.part and a.part == b.part)


def _holds(a: Piece, b: Piece) -> bool:
    """One is a sprig drawn whole with the other's source inside it (a camera on its own, and in a frame)."""
    return (a.key in b.holds) != (b.key in a.holds)


def pair(old: list[Piece], new: list[Piece]) -> dict[int, int]:
    """new index -> old index for pieces in both: the same source first (in order, when one is placed
    twice), then the same sprig placed again, then the same name among what's left, then a source and a sprig
    that holds it."""
    pairs: dict[int, int] = {}
    taken: set[int] = set()
    for rule in ("key", "part", "name", "holds"):
        for j, piece in enumerate(new):
            if j in pairs or (rule in ("name", "part") and not getattr(piece, rule)):
                continue
            for i, before in enumerate(old):
                if i in taken:
                    continue
                if (_holds(before, piece) if rule == "holds" else getattr(before, rule) == getattr(piece, rule)):
                    pairs[j] = i
                    taken.add(i)
                    break
    return pairs


def _shape(quad: list) -> tuple:
    """A quad as (centre x, centre y, width, angle, height, shear) plus its four uv's."""
    (x0, y0, *_), (x1, y1, *_), (x2, y2, *_), (x3, y3, *_) = quad
    ex, ey = (x1 - x0, y1 - y0), (x2 - x0, y2 - y0)
    w = math.hypot(*ex)
    angle = math.atan2(ex[1], ex[0]) if w > 1e-9 else 0.0
    c, s = math.cos(angle), math.sin(angle)
    shear = ey[0] * c + ey[1] * s  # along the top edge
    height = -ey[0] * s + ey[1] * c  # across it (negative when mirrored)
    return ((x0 + x3) / 2, (y0 + y3) / 2, w, angle, height, shear)


def _quad(shape: tuple, uvs: list) -> list:
    cx, cy, w, angle, height, shear = shape
    c, s = math.cos(angle), math.sin(angle)
    ex = (w * c, w * s)
    ey = (shear * c - height * s, shear * s + height * c)
    x0, y0 = cx - (ex[0] + ey[0]) / 2, cy - (ex[1] + ey[1]) / 2
    corners = ((x0, y0), (x0 + ex[0], y0 + ex[1]), (x0 + ey[0], y0 + ey[1]),
               (x0 + ex[0] + ey[0], y0 + ex[1] + ey[1]))
    return [(x, y, u, v) for (x, y), (u, v) in zip(corners, uvs)]


def _uvs(quad: list) -> list:
    return [(q[2], q[3]) for q in quad]


def between(a: list, b: list, e: float) -> list:
    """The quad e of the way from a to b: centre, size and turn move together (the shortest way round),
    so a turning picture keeps its size, and its crop slides from one to the other."""
    sa, sb = _shape(a), _shape(b)
    turn = (sb[3] - sa[3] + math.pi) % math.tau - math.pi
    shape = tuple(p + (q - p) * e for p, q in zip(sa, sb))
    shape = (*shape[:3], sa[3] + turn * e, *shape[4:])
    ua, ub = _uvs(a), _uvs(b)
    uvs = []
    for (u0, v0), (u1, v1) in zip(ua, ub):
        u, v = u0 + (u1 - u0) * e, v0 + (v1 - v0) * e
        # A spring passes its end: keep the crop within the two it moves between (never past the picture).
        uvs.append((min(max(u, min(u0, u1)), max(u0, u1)), min(max(v, min(v0, v1)), max(v0, v1))))
    return _quad(shape, uvs)


def _scaled(quad: list, k: float) -> list:
    cx, cy, w, angle, height, shear = _shape(quad)
    return _quad((cx, cy, w * k, angle, height * k, shear * k), _uvs(quad))


def _covers(quad: list, canvas: tuple[int, int]) -> bool:
    """True when the quad covers the whole canvas (a background)."""
    xs, ys = [q[0] for q in quad], [q[1] for q in quad]
    if min(xs) > 0.5 or min(ys) > 0.5 or max(xs) < canvas[0] - 0.5 or max(ys) < canvas[1] - 0.5:
        return False
    (x0, y0, *_), (x1, y1, *_), (x2, y2, *_) = quad[:3]
    return abs((x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0)) >= canvas[0] * canvas[1] - 1.0  # not turned


def _slid(quad: list, k: float, canvas: tuple[int, int]) -> list:
    """The quad pushed k of the way out past the canvas edge nearest its centre."""
    cw, ch = canvas
    xs, ys = [q[0] for q in quad], [q[1] for q in quad]
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    gaps = {(-1, 0): max(xs), (1, 0): cw - min(xs), (0, -1): max(ys), (0, 1): ch - min(ys)}
    toward = {(-1, 0): cx, (1, 0): cw - cx, (0, -1): cy, (0, 1): ch - cy}
    way = min(toward, key=toward.get)
    reach = gaps[way] + SLIDE_EDGE * (cw if way[0] else ch)
    dx, dy = way[0] * reach * k, way[1] * reach * k
    return [(x + dx, y + dy, u, v) for x, y, u, v in quad]


def frame(old: list[Piece], new: list[Piece], progress: float, easing: str = "smooth",
          unmatched: str = "fade", canvas: tuple[int, int] = (1920, 1080)) -> list[Draw]:
    """What to draw `progress` (0 .. 1) of the way through, bottom first.

    At 0 it draws exactly the old bloom and at 1 exactly the new one. Paired pieces glide between their
    two spots. What only the new bloom has comes in during the first half and what only the old one has
    leaves during the second, so wherever both cover the same spot one of them is always fully there: no
    see-through dip in the middle, as two fades at once would have.
    """
    t = min(1.0, max(0.0, progress))
    e = ease(easing, t)
    arrive = _smooth(t / 0.5)
    leave = 1.0 - _smooth((t - 0.5) / 0.5)
    move = ease("smooth" if easing == "spring" else easing, t)  # what comes and goes never overshoots
    pairs = pair(old, new)

    def coming_or_going(piece: Piece, k: float, alpha: float) -> list[Draw]:
        quad = piece.quad
        if _covers(quad, canvas):
            pass  # a background just fades: scaled or slid, it would show bare canvas round its edges
        elif unmatched == "scale":
            quad = _scaled(quad, 1.0 - (1.0 - SCALE_TO) * k)
        elif unmatched == "slide":
            quad = _slid(quad, k, canvas)
        return [Draw(piece.source, piece.canvas, quad, piece.opacity * alpha)]

    def gliding(j: int) -> list[Draw]:
        piece, before = new[j], old[pairs[j]]
        quad = between(before.quad, piece.quad, e)
        alpha = before.opacity + (piece.opacity - before.opacity) * t
        if same(before, piece):
            return [Draw(piece.source, piece.canvas, quad, alpha)]
        # Paired by name: two sources, so the old one hands over to the new one as they move.
        shape = _shape(quad)
        return [Draw(before.source, before.canvas, _quad(shape, _uvs(before.quad)), before.opacity * leave),
                Draw(piece.source, piece.canvas, _quad(shape, _uvs(piece.quad)), piece.opacity * arrive)]

    leaving = {i: (lambda p=p: coming_or_going(p, move, leave)) for i, p in enumerate(old)}
    arriving = {j: (lambda p=p: coming_or_going(p, 1.0 - move, arrive)) for j, p in enumerate(new)}
    # Stacking: paired pieces in the old bloom's order for the first half and the new one's after, so two
    # that swap places swap mid-move, not with a jump at the start. A lone piece stays just above the paired
    # piece it sat on in its own bloom (below everything when none), and what arrives goes above what
    # leaves, so it fades in on top of it, never under it.
    old_to_new = {i: j for j, i in pairs.items()}
    paired = sorted(pairs, key=pairs.get) if t < 0.5 else sorted(pairs)
    after: dict = {}  # (paired new index or None, "old" | "new") -> lone pieces drawn right after it

    def tuck(kind: str, count: int, to_new: dict) -> None:
        anchor = None
        for k in range(count):
            if k in to_new:
                anchor = to_new[k]
            else:
                after.setdefault((anchor, kind), []).append((kind, k))

    tuck("old", len(old), old_to_new)
    tuck("new", len(new), {j: j for j in pairs})
    order = [*after.get((None, "old"), ()), *after.get((None, "new"), ())]
    for j in paired:
        order += [("new", j), *after.get((j, "old"), ()), *after.get((j, "new"), ())]
    out: list[Draw] = []
    for kind, k in order:
        if kind == "old":
            out.extend(leaving[k]())
        else:
            out.extend(gliding(k) if k in pairs else arriving[k]())
    return [d for d in out if d.opacity > 0.0]
