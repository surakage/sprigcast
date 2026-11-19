"""The Move transition drawn by the native engine (docs/native-engine.md "Transitions"): move.cpp works out every
frame from the pieces Python lays out once. It must draw what render/move.py draws, frame for frame."""

from __future__ import annotations

import math
import random

import pytest

from sprigcast import sprigcore
from sprigcast.render import move

needs_engine = pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")


def _quad(rng, size):
    w, h = size
    if rng.random() < 0.15:  # a background: it only fades
        return [(0, 0, 0, 0), (w, 0, 1, 0), (0, h, 0, 1), (w, h, 1, 1)]
    x, y = rng.uniform(-200, w), rng.uniform(-200, h)
    qw, qh = rng.uniform(10, 900), rng.uniform(10, 600)
    a = rng.choice([0.0, 0.0, rng.uniform(-3.2, 3.2)])
    c, s = math.cos(a), math.sin(a)
    u0, v0 = rng.choice([(0, 0), (0.1, 0.2)])
    u1, v1 = rng.choice([(1, 1), (0.9, 0.7)])
    if rng.random() < 0.2:
        u0, u1 = u1, u0  # mirrored
    corners = [(0, 0, u0, v0), (qw, 0, u1, v0), (0, qh, u0, v1), (qw, qh, u1, v1)]
    return [(x + px * c - py * s, y + px * s + py * c, u, v) for px, py, u, v in corners]


def _pieces(rng, count, size):
    out = []
    for _ in range(count):
        source = f"s{rng.randint(0, 8)}"
        name = rng.choice(["cam", "logo", "", "text"])
        out.append(move.Piece(source, name, source, "c", _quad(rng, size), rng.choice([1.0, 0.5, 0.0, 0.8])))
    return out


def _as_engine(pieces):
    return [(p.source, None, p.quad, p.opacity, None) for p in pieces]


@needs_engine
def test_the_engine_moves_every_piece_where_python_would():
    rng = random.Random(7)
    checked = 0
    for _ in range(400):
        size = rng.choice([(1920, 1080), (1080, 1920), (1280, 720)])
        old, new = _pieces(rng, rng.randint(0, 7), size), _pieces(rng, rng.randint(0, 7), size)
        easing, unmatched = rng.choice(move.EASINGS), rng.choice(move.UNMATCHED)
        spec = sprigcore.move_spec(_as_engine(old), _as_engine(new), move.pair(old, new), easing, unmatched)
        for progress in (0.0, 0.1, 0.3, 0.49, 0.5, 0.51, 0.7, 0.95, 1.0, rng.random()):
            want = move.frame(old, new, progress, easing, unmatched, size)
            got = sprigcore.move_frame(spec, progress, *size)
            assert len(got) == len(want)
            for draw, (source, part, quad, opacity, effects) in zip(want, got):
                assert source == sprigcore.key(draw.source) and part == 0 and effects == -1
                assert opacity == pytest.approx(draw.opacity, abs=1e-4)
                for mine, theirs in zip(quad, draw.quad):
                    assert mine == pytest.approx(theirs, abs=0.01)
            checked += 1
    assert checked == 4000


@needs_engine
def test_a_move_that_doesnt_hold_together_is_refused():
    spec = sprigcore.move_spec([], [], {}, "smooth", "fade")
    assert sprigcore.move_frame(spec, 0.5, 64, 36) == []
    spec.easing = 42
    with pytest.raises(sprigcore.NativeError):
        sprigcore.move_frame(spec, 0.5, 64, 36)


def test_move_names_are_the_scenes():
    from sprigcast.core.scene import MOVE_EASINGS, MOVE_UNMATCHED

    assert sprigcore.MOVE_EASINGS == MOVE_EASINGS and sprigcore.MOVE_UNMATCHED == MOVE_UNMATCHED
