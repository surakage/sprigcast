"""The Move transition: what both blooms show glides from its old spot to its new one."""

import math
import time

import numpy as np
import pytest

from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig, Transition
from sprigcast.render import move
from sprigcast.render.engine import Engine

W, H = 64, 36


def piece(key, x, y, w, h, name=None, crop=(0.0, 0.0, 1.0, 1.0), turn=0.0, opacity=1.0):
    item = SceneItem(source=key, x=x, y=y, rotation=turn)
    quad = item.quad((w, h))
    u0, v0, u1, v1 = crop
    quad = [(qx, qy, u0 + (u1 - u0) * qu, v0 + (v1 - v0) * qv) for qx, qy, qu, qv in quad]
    return move.Piece(key, (name or key).casefold(), key, "h", quad, opacity)


def close(a, b, tol=1e-6):
    return all(abs(p - q) <= tol for pa, pb in zip(a, b) for p, q in zip(pa, pb))


@pytest.mark.parametrize("easing", move.EASINGS)
def test_every_easing_starts_and_ends_still(easing):
    assert move.ease(easing, 0.0) == pytest.approx(0.0, abs=1e-9)
    assert move.ease(easing, 1.0) == pytest.approx(1.0, abs=1e-9)
    assert move.ease(easing, -1) == move.ease(easing, 0.0) and move.ease(easing, 2) == move.ease(easing, 1.0)


def test_spring_overshoots_a_little_and_smooth_never_does():
    assert max(move.ease("spring", i / 100) for i in range(101)) > 1.05
    assert max(move.ease("spring", i / 100) for i in range(101)) < 1.15
    values = [move.ease("smooth", i / 100) for i in range(101)]
    assert values == sorted(values) and max(values) <= 1.0
    # Flat at both ends: the first and last steps are tiny, so nothing jumps at the start or the end.
    assert values[1] < 0.001 and 1.0 - values[99] < 0.001


def test_pairs_by_source_then_by_name():
    old = [piece("cam", 0, 0, 10, 10), piece("bg", 0, 0, 64, 36), piece("x1", 0, 0, 5, 5, name="Logo")]
    new = [piece("bg2", 0, 0, 64, 36), piece("x2", 0, 0, 5, 5, name="logo"), piece("cam", 9, 9, 10, 10)]
    assert move.pair(old, new) == {2: 0, 1: 2}
    # A source placed twice pairs in order.
    twice = [piece("cam", 0, 0, 4, 4), piece("cam", 20, 0, 4, 4)]
    assert move.pair(twice, list(reversed(twice))) == {0: 0, 1: 1}


@pytest.mark.parametrize("unmatched", move.UNMATCHED)
@pytest.mark.parametrize("easing", move.EASINGS)
def test_starts_exactly_on_the_old_bloom_and_ends_on_the_new(easing, unmatched):
    old = [piece("bg", 0, 0, 64, 36), piece("cam", 40, 20, 16, 9, crop=(0.1, 0, 0.9, 1)), piece("old", 2, 2, 8, 8)]
    new = [piece("bg2", 0, 0, 64, 36), piece("cam", 2, 2, 32, 18, turn=30), piece("new", 50, 2, 8, 8)]
    start = move.frame(old, new, 0.0, easing, unmatched, (W, H))
    assert [(d.source, d.opacity) for d in start] == [("bg", 1.0), ("cam", 1.0), ("old", 1.0)]
    assert all(close(d.quad, p.quad) for d, p in zip(start, old))
    end = move.frame(old, new, 1.0, easing, unmatched, (W, H))
    assert [(d.source, d.opacity) for d in end] == [("bg2", 1.0), ("cam", 1.0), ("new", 1.0)]
    assert all(close(d.quad, p.quad, 1e-6) for d, p in zip(end, new))


def test_a_paired_piece_glides_keeping_its_size_through_a_turn():
    old = [piece("cam", 0, 0, 20, 10)]
    new = [piece("cam", 0, 0, 20, 10, turn=90)]
    for t in (0.25, 0.5, 0.75):
        (d,) = move.frame(old, new, t, "linear")
        (x0, y0, *_), (x1, y1, *_), (x2, y2, *_) = d.quad[:3]
        assert math.hypot(x1 - x0, y1 - y0) == pytest.approx(20)  # corners interpolated would shrink it
        assert math.hypot(x2 - x0, y2 - y0) == pytest.approx(10)
        assert math.degrees(math.atan2(y1 - y0, x1 - x0)) == pytest.approx(90 * t)
    # The shortest way round: 350 degrees to 10 turns 20 degrees forward, not 340 back.
    (d,) = move.frame([piece("cam", 0, 0, 20, 10, turn=350)], [piece("cam", 0, 0, 20, 10, turn=10)], 0.5, "linear")
    (x0, y0, *_), (x1, y1, *_) = d.quad[:2]
    assert math.degrees(math.atan2(y1 - y0, x1 - x0)) == pytest.approx(0, abs=1e-6)


def test_crop_slides_and_a_spring_never_reaches_past_the_picture():
    old = [piece("cam", 0, 0, 20, 10, crop=(0.0, 0.0, 1.0, 1.0))]
    new = [piece("cam", 0, 0, 20, 10, crop=(0.2, 0.1, 0.8, 0.9))]
    (d,) = move.frame(old, new, 0.5, "linear")
    assert d.quad[0][2:] == pytest.approx((0.1, 0.05)) and d.quad[3][2:] == pytest.approx((0.9, 0.95))
    for i in range(101):
        (d,) = move.frame(old, new, i / 100, "spring")
        assert all(0.0 <= u <= 1.0 and 0.0 <= v <= 1.0 for *_, u, v in d.quad)


def test_lone_pieces_hand_over_without_a_see_through_dip():
    old = [piece("bg", 0, 0, 64, 36)]
    new = [piece("bg2", 0, 0, 64, 36)]
    for i in range(101):
        draws = move.frame(old, new, i / 100)
        alphas = {d.source: d.opacity for d in draws}
        a, b = alphas.get("bg", 0.0), alphas.get("bg2", 0.0)
        assert [d.source for d in draws] == [s for s in ("bg", "bg2") if alphas.get(s)]  # arriving on top
        assert b + a * (1 - b) == pytest.approx(1.0) or max(a, b) == pytest.approx(1.0)


def test_what_sat_on_a_paired_piece_stays_on_it():
    old = [piece("game", 0, 0, 64, 36), piece("cam", 40, 20, 16, 9), piece("frame", 40, 20, 16, 9)]
    new = [piece("cam", 0, 0, 64, 36), piece("chat", 40, 2, 16, 30)]
    order = [d.source for d in move.frame(old, new, 0.01)]
    assert order.index("frame") > order.index("cam")  # no jump under the camera at the start
    assert order.index("game") < order.index("cam")


def test_two_pieces_that_swap_places_swap_mid_move():
    old = [piece("a", 0, 0, 10, 10), piece("b", 5, 5, 10, 10)]
    new = [piece("b", 5, 5, 10, 10), piece("a", 0, 0, 10, 10)]
    assert [d.source for d in move.frame(old, new, 0.2)] == ["a", "b"]
    assert [d.source for d in move.frame(old, new, 0.8)] == ["b", "a"]


def test_scale_and_slide_move_what_only_one_bloom_shows():
    old = [piece("logo", 4, 4, 10, 10)]
    for kind in ("scale", "slide"):
        (d,) = move.frame(old, [], 0.7, "smooth", kind, (W, H))
        if kind == "scale":
            assert math.hypot(d.quad[1][0] - d.quad[0][0], d.quad[1][1] - d.quad[0][1]) < 10
        else:
            assert d.quad[0][0] < 4 or d.quad[0][1] < 4  # off toward the nearest edge (top-left corner)
    (d,) = move.frame([], [piece("logo", 50, 4, 10, 10)], 0.3, "smooth", "slide", (W, H))
    assert d.quad[0][0] > 50  # comes in from the right edge
    for kind in ("scale", "slide"):  # a background only fades: moved, it would bare the canvas's edges
        (d,) = move.frame([piece("bg", 0, 0, W, H)], [], 0.7, "smooth", kind, (W, H))
        assert close(d.quad, piece("bg", 0, 0, W, H).quad) and 0 < d.opacity < 1


def test_paired_by_name_hands_one_source_over_to_the_other():
    old = [piece("cam1", 0, 0, 10, 10, name="Camera")]
    new = [piece("cam2", 30, 0, 10, 10, name="camera")]
    draws = move.frame(old, new, 0.25, "linear")
    assert [d.source for d in draws] == ["cam1", "cam2"]
    assert draws[0].quad[0][:2] == pytest.approx(draws[1].quad[0][:2])  # one spot, two pictures
    assert draws[0].quad[0][0] == pytest.approx(7.5)
    assert draws[0].opacity == 1.0 and 0.0 < draws[1].opacity < 1.0  # the new one fades in on top


# Blooms with sprigs --------------------------------------------------------------------------------


def sprig_collection(cam_x: float, scaled: bool = False, filtered: bool = False) -> SceneCollection:
    canvases = [Canvas(id="h", width=W, height=H)]
    sources = [SourceConfig(id=s, kind="color", name=s.title()) for s in ("cam", "bg", "logo")]
    sources += [SourceConfig(id="pa", kind="scene", name="Sprig A", settings={"scene": "spa"}),
                SourceConfig(id="pb", kind="scene", name="Sprig B", settings={"scene": "spb"}),
                SourceConfig(id="pc", kind="scene", name="Corner", settings={"scene": "spc"})]
    if filtered:
        from sprigcast.core.scene import Filter

        sources[3].filters.append(Filter(id="f", kind="color_correction", name="Look", settings={"gamma": 0.5}))
    scenes = [
        Scene(id="spa", name="Sprig A", canvas="h", kind="part",
              items=[SceneItem(source="bg"), SceneItem(source="cam", x=cam_x, y=4), SceneItem(source="pc")]),
        Scene(id="spb", name="Sprig B", canvas="h", kind="part", items=[SceneItem(source="cam", x=2, y=2)]),
        Scene(id="spc", name="Corner", canvas="h", kind="part", items=[SceneItem(source="logo")]),
        Scene(id="a", name="A", canvas="h", items=[SceneItem(source="pa", scale=0.5 if scaled else 1.0)]),
        Scene(id="b", name="B", canvas="h", items=[SceneItem(source="pb")]),
    ]
    return SceneCollection(canvases=canvases, sources=sources, scenes=scenes, active_scenes={"h": "a"},
                           transition=Transition(kind="move", duration_ms=400))


def test_sprigs_that_fill_the_canvas_open_up_so_what_is_inside_moves_on_its_own():
    col = sprig_collection(40)
    engine = Engine(col)
    sizes = {"cam": (16, 9), "bg": (W, H), "logo": (4, 4)}
    pieces = engine._move_pieces(col.scene("a"), sizes)
    assert [p.source for p in pieces] == ["bg", "cam", "logo"]  # "Corner" fills the canvas too
    assert pieces[1].quad[0][:2] == (40, 4)
    assert [p.source for p in engine._move_pieces(col.scene("b"), sizes)] == ["cam"]
    # A sprig placed smaller (or with filters) is drawn whole, as its own texture: it moves as one.
    small = sprig_collection(40, scaled=True)
    assert [p.source for p in Engine(small)._move_pieces(small.scene("a"), sizes)] == ["pa"]
    filtered = Engine(sprig_collection(40, filtered=True))
    filtered._filtered = frozenset({"pa"})
    assert [p.source for p in filtered._move_pieces(filtered._collection.scene("a"), sizes)] == ["pa"]
    # Left out of screenshots, however deep.
    assert [p.source for p in engine._move_pieces(col.scene("a"), sizes, frozenset({"logo"}))] == ["bg", "cam"]


def test_engine_runs_a_move_and_skips_the_sprigs_it_opened_up():
    before, after = sprig_collection(40), sprig_collection(40)
    after.active_scenes["h"] = "b"
    engine = Engine(after)
    engine._start_transitions(before, after)
    running = engine._transitions["h"]
    assert running.settings.kind == "move" and running.duration == pytest.approx(0.4)

    class Fake:
        textures, parts = {}, {}

        def upload(self, *a):
            pass

    extra = engine._transition_step(Fake(), "h", running.began + 0.2)
    assert extra["transition"].kind == "move" and callable(extra["move"])
    engine._sources = {}
    assert extra["move"]() == []  # no pictures yet: nothing to draw, and no error
    drawn = engine._move_light(engine._collection, engine._drawn_scenes(engine._collection))
    # The bloom moved out of keeps no sprig drawn just for it: its sprigs were opened up.
    assert all(s.id != "a" for s in drawn) and {s.id for s in drawn} == {"b"}


def test_old_files_load_with_move_settings_defaulted():
    t = Transition.model_validate({"kind": "fade", "duration_ms": 300})
    assert t.easing == "smooth" and t.unmatched == "fade"
    assert Transition.model_validate({"kind": "move", "easing": "spring", "unmatched": "scale"}).easing == "spring"


# Drawn on the GPU --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def compositor():
    pytest.importorskip("moderngl")
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    yield comp
    comp.release()


def _img(comp, canvas):
    return np.frombuffer(comp.read(canvas.id), dtype=np.uint8).reshape(canvas.height, canvas.width, 4)


@pytest.mark.parametrize("shape", [(W, H), (H, W)])
def test_compositor_draws_the_glide_on_both_canvases(compositor, shape):
    canvas = Canvas(id="mv", width=shape[0], height=shape[1])
    compositor.upload("red", np.full((8, 8, 4), (255, 0, 0, 255), np.uint8))
    sizes = {"red": (8, 8)}
    old = Scene(id="a", name="A", canvas="mv", items=[SceneItem(source="red", x=0, y=0)])
    end_x = shape[0] - 8
    new = Scene(id="b", name="B", canvas="mv", items=[SceneItem(source="red", x=end_x, y=0)])
    t = Transition(kind="move", easing="linear")

    def pieces(scene):
        return [move.Piece("red", "red", "red", "mv", scene.items[0].quad((8, 8)))]

    compositor.render(canvas, new, sizes, old, 0.5, t, move=lambda: move.frame(pieces(old), pieces(new), 0.5,
                                                                               "linear", "fade", shape))
    img = _img(compositor, canvas)
    mid = round(end_x / 2) + 4
    assert img[4, mid][0] == 255 and img[4, 1][3] == 0 and img[4, shape[0] - 2][3] == 0
    assert compositor.targets["mv"].in_fbo is None  # no extra canvas textures for a Move
    # No pop at the end: the last frame of the move is the new bloom drawn the usual way.
    compositor.render(canvas, new, sizes, old, 0.999999, t, move=lambda: move.frame(pieces(old), pieces(new),
                                                                                    1.0, "linear", "fade", shape))
    last = _img(compositor, canvas).copy()
    compositor.render(canvas, new, sizes)
    assert np.array_equal(last, _img(compositor, canvas))
    compositor.forget("red")


# The Transitions card ------------------------------------------------------------------------------


def test_panel_offers_move_with_its_easing_and_what_the_rest_does():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from sprigcast.app.transitions_panel import TransitionsPanel, summary

    t = Transition()
    seen = []
    panel = TransitionsPanel(t, lambda: seen.append(1))
    panel.resize(400, 600)
    panel.show()
    app.processEvents()
    assert not panel.easing.isVisibleTo(panel)
    panel.kind_buttons["move"].click()
    assert t.kind == "move" and panel.easing.isVisibleTo(panel) and panel.unmatched.isVisibleTo(panel)
    assert panel.length.isVisibleTo(panel) and summary(t) == "Move · 0.3 s"
    panel.easing.actions_by_value["spring"].trigger()
    panel.unmatched.actions_by_value["scale"].trigger()
    assert (t.easing, t.unmatched) == ("spring", "scale") and seen
    assert panel.easing.text().startswith("Spring") and panel.unmatched.actions_by_value["scale"].isChecked()
    panel.load(Transition(kind="move", easing="linear"))
    assert panel.easing.text().startswith("Linear") and panel.unmatched.text().startswith("Fade")
    # The nine choices sit in three full rows.
    assert panel._columns == 3
    panel.close()


def test_obs_move_transitions_import_as_move_and_the_obs_door_names_it():
    from sprigcast.control.api import OBS_TRANSITION_KINDS
    from sprigcast.core.convert import _transition

    notes = []
    t = _transition({"id": "move_transition", "name": "Move", "settings": {}}, 700, notes)
    assert t.kind == "move" and t.duration_ms == 700 and notes == []
    assert OBS_TRANSITION_KINDS["move"] == "move_transition"
