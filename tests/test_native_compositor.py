"""The native compositor against today's (docs/native-engine.md, phase 2), and the snapshot it draws from.

The pictures in tests/native_golden are today's compositor's drawing of the cases in tests/native_cases.py.
The engine must draw the same; today's compositor must still draw them too, or they are remade."""

import time

import numpy as np
import pytest
from PIL import Image

from sprigcast import sprigcore
from sprigcast.core.filters import VideoEffects
from sprigcast.core.scene import Scene, SceneItem
from sprigcast.render import native_snapshot
from tests import native_cases as nc

CASES = nc.cases(nc.GOLDEN)
IDS = [c.name for c in CASES]


def golden(case) -> np.ndarray:
    return np.asarray(Image.open(nc.GOLDEN / f"{case.name}.png").convert("RGBA"))


# ---- the snapshot (no engine needed) ---------------------------------------------------------------------------

def test_snapshot_keeps_visible_items_in_order_with_shared_effects():
    fx = VideoEffects(blur=2.0)
    scene = Scene(id="b", name="b", canvas="h", items=[
        SceneItem(source="a"), SceneItem(source="hidden", visible=False), SceneItem(source="c", scale_y=0.5,
                                                                                     align="bottom_right")])
    built = native_snapshot.build([native_snapshot.SceneDraw("b", scene, (64, 36), {"c": 0.25})],
                                  {"a": fx, "c": fx}, {("c", "b"): "sprig"})
    s = built.struct
    assert (s.scene_count, s.item_count, s.effects_count) == (1, 2, 1)
    a, c = s.items[0], s.items[1]
    assert a.source == sprigcore.key("a") and a.part == 0 and a.scale_y == -1.0 and a.fade == 1.0
    assert c.part == sprigcore.key("sprig") and c.scale_y == 0.5 and c.fade == 0.25 and c.align == 8
    assert a.effects == c.effects == 0 and s.effects[0].blur == 2.0
    assert s.scenes[0].width == 64 and s.scenes[0].first_item == 0 and s.scenes[0].item_count == 2


def test_untouched_effects_are_left_out():
    scene = Scene(id="b", name="b", canvas="h", items=[SceneItem(source="a")])
    built = native_snapshot.build([native_snapshot.SceneDraw("b", scene, (8, 8))], {"a": VideoEffects()})
    assert built.struct.effects_count == 0 and built.struct.items[0].effects == -1


def test_looks_and_shape_pictures_are_listed():
    scene = Scene(id="b", name="b", canvas="h", items=[SceneItem(source="a")])
    built = native_snapshot.build([native_snapshot.SceneDraw("b", scene, (8, 8))],
                                  {"a": VideoEffects(lut="look:warm", shape=7, mask="m.png")})
    assert built.looks == {"look:warm"} and built.masks == {"m.png"}


def test_keys_are_stable_and_never_zero():
    assert sprigcore.key("horizontal") == sprigcore.key("horizontal") != sprigcore.key("vertical")
    assert all(sprigcore.key(str(i)) for i in range(1000))


# ---- today's compositor still draws the pictures -----------------------------------------------------------------

@pytest.fixture(scope="module")
def python_compositor():
    pytest.importorskip("moderngl")
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no OpenGL context: {exc}")
    yield comp
    comp.release()


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_golden_pictures_match_todays_compositor(python_compositor, case):
    stats = nc.compare(nc.draw_python(python_compositor, case), golden(case))
    assert nc.close_enough(stats), f"{case.name}: {stats}; if the change is meant, run python tests/native_cases.py"


@pytest.mark.parametrize("shape", nc.NV12_SHAPES, ids=[n for n, _s, _h in nc.NV12_SHAPES])
def test_golden_nv12_matches_todays_compositor(python_compositor, shape):
    name, size, how = shape
    want = np.asarray(Image.open(nc.GOLDEN / f"{name}.png").convert("L"))
    stats = nc.compare(nc.draw_python(python_compositor, CASES[0], (size, how))[..., None], want[..., None])
    assert nc.close_enough(stats), f"{name}: {stats}; if the change is meant, run python tests/native_cases.py"


# ---- the native engine draws the same ----------------------------------------------------------------------------

needs_engine = pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")


@pytest.fixture(scope="module")
def device():
    with sprigcore.Device(warp=True) as dev:
        yield dev


@needs_engine
@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_native_engine_draws_like_todays_compositor(device, case, tmp_path):
    got = nc.draw_native(device, case)
    stats = nc.compare(got, golden(case))
    if not nc.close_enough(stats):
        Image.fromarray(got, "RGBA").save(tmp_path / f"{case.name}-native.png")
    assert nc.close_enough(stats), f"{case.name}: {stats} (native picture in {tmp_path})"


@needs_engine
@pytest.mark.parametrize("shape", nc.NV12_SHAPES, ids=[n for n, _s, _h in nc.NV12_SHAPES])
def test_native_nv12_is_what_todays_compositor_gives_encoders(device, shape, tmp_path):
    name, size, how = shape
    want = np.asarray(Image.open(nc.GOLDEN / f"{name}.png").convert("L"))
    got = nc.draw_native(device, CASES[0], (size, how))
    stats = nc.compare(got[..., None], want[..., None])
    if not nc.close_enough(stats):
        Image.fromarray(got, "L").save(tmp_path / f"{name}-native.png")
    assert nc.close_enough(stats), f"{name}: {stats} (native picture in {tmp_path})"


@needs_engine
@pytest.mark.parametrize("kind", [1, 2])
def test_a_fault_shuts_the_device_off_not_the_app(kind):
    """The crash gate: a C++ exception or an access violation inside the engine becomes an error, the device
    refuses further work, and a new device works."""
    dev = sprigcore.Device(warp=True)
    lib = sprigcore._lib()
    assert lib.sc_debug_fault(dev.handle, kind) == -5
    with pytest.raises(sprigcore.NativeError) as e:
        sprigcore.Canvas(dev, 8, 8)
    assert e.value.code == sprigcore.FAULTED
    dev.close()
    with sprigcore.Device(warp=True) as fresh, sprigcore.Canvas(fresh, 8, 8) as c:
        c.clear(0, 1, 0)


@needs_engine
def test_a_sprig_snapshot_is_the_engines_own_copy(device):
    """The engine draws from its copy: the arrays Python built can go right after handing over."""
    pic = nc.picture(16, 16)
    device.upload("a", pic)
    scene = Scene(id="b", name="b", canvas="h", items=[SceneItem(source="a", align="top_left")])
    built = native_snapshot.build([native_snapshot.SceneDraw("b", scene, (16, 16))], {})
    device.set_snapshot(built.struct)
    del built
    import gc

    gc.collect()
    with sprigcore.Canvas(device, 16, 16) as c:
        c.render("b")
        got = np.frombuffer(c.read_bgra(), np.uint8).reshape(16, 16, 4)[..., [2, 1, 0, 3]]
    assert np.abs(got.astype(int) - pic.astype(int)).max() <= 1
    device.forget("a")


@needs_engine
def test_a_held_copy_keeps_the_picture_while_the_source_moves_on(device):
    """Device.hold: a still copy of a source's picture, or of a sprig's drawing, as a source of its own (an item
    frozen on its own is drawn from it). The source itself goes on live."""
    first, second = nc.picture(16, 16, seed=1), nc.picture(16, 16, seed=5)

    def drawn(key):
        with sprigcore.Canvas(device, 16, 16) as c:
            c.render(key)
            return np.frombuffer(c.read_bgra(), np.uint8).reshape(16, 16, 4)[..., [2, 1, 0, 3]].astype(int)

    assert device.hold("still", source="a") is False  # nothing to copy yet
    device.upload("a", first)
    inner = native_snapshot.SceneDraw("inner", nc.scene("inner", [SceneItem(source="a", align="top_left")]), (16, 16))
    live = Scene(id="b", name="b", canvas="h", items=[SceneItem(source="a", align="top_left")])
    froze = Scene(id="f", name="f", canvas="h", items=[SceneItem(source="a", align="top_left")])
    sprig = Scene(id="s", name="s", canvas="h", items=[SceneItem(source="p", align="top_left")])
    draws = [inner, native_snapshot.SceneDraw("b", live, (16, 16)), native_snapshot.SceneDraw("f", froze, (16, 16)),
             native_snapshot.SceneDraw("s", sprig, (16, 16))]
    parts = {("p", "s"): "inner"}
    native_snapshot.hand_over(device, native_snapshot.build(draws, {}, parts), native_snapshot.Resources())
    assert device.hold("still", source="a") and device.hold("still-sprig", scene="inner")
    assert device.hold("none", scene="no such scene") is False
    device.upload("a", second)
    built = native_snapshot.build(draws, {}, parts, {"f/a": "still", "s/p": "still-sprig"})
    native_snapshot.hand_over(device, built, native_snapshot.Resources())
    assert np.abs(drawn("b") - second.astype(int)).max() <= 1  # live where it isn't frozen
    assert np.abs(drawn("f") - first.astype(int)).max() <= 1
    assert np.abs(drawn("s") - first.astype(int)).max() <= 2
    for gone in ("a", "still", "still-sprig"):
        device.forget(gone)


@needs_engine
def test_a_sprig_laid_over_the_whole_canvas_draws_as_its_items_would(device):
    """A sprig placed at 0, 0 at the canvas's size, untouched (what a nested scene from OBS becomes), is drawn
    straight onto the canvas: no picture of its own. The canvas must come out as if its items were placed there,
    sprigs inside it included, and the same sprig placed small elsewhere still gets its picture."""
    w, h = nc.W, nc.H
    uploads = [("bg", nc.picture(w, h, seed=9), "RGBA", None), ("a", nc.picture(64, 48, seed=1, alpha=True), "RGBA", None),
               ("e", nc.picture(48, 32, seed=2, alpha=True), "RGBA", None)]
    a = SceneItem(source="a", x=10, y=10, align="top_left")
    e = SceneItem(source="e", x=40, y=20, align="top_left")
    small = SceneItem(source="ps", x=80, y=40, scale=0.5, align="top_left")
    inner = native_snapshot.SceneDraw("inner", nc.scene("inner", [a]), (w, h))
    outer = native_snapshot.SceneDraw("outer", nc.scene("outer", [SceneItem(source="pi", align="top_left"), e]), (w, h))
    nested = nc.Case("laid_over", uploads,
                     [inner, outer, nc.bloom([SceneItem(source="bg", align="top_left"),
                                              SceneItem(source="po", align="top_left"), small])],
                     {}, {("pi", "outer"): "inner", ("po", "h"): "outer", ("ps", "h"): "inner"})
    flat = nc.Case("laid_over_flat", uploads,
                   [inner, nc.bloom([SceneItem(source="bg", align="top_left"), a, e, small])],
                   {}, {("ps", "h"): "inner"})
    got, want = nc.draw_native(device, nested), nc.draw_native(device, flat)
    assert np.abs(got.astype(int) - want.astype(int)).max() <= 2
    without = nc.Case("laid_over_bare", uploads, [nc.bloom([SceneItem(source="bg", align="top_left")])], {}, {})
    bare = nc.draw_native(device, without)
    assert np.abs(got[45:, 85:].astype(int) - bare[45:, 85:].astype(int)).max() > 16  # the small one is there
    # Faded, the whole sprig fades as one picture (not each item over the other): that keeps its own picture.
    faded = nc.Case("laid_over_faded", uploads,
                    [inner, outer, nc.bloom([SceneItem(source="bg", align="top_left"),
                                             SceneItem(source="po", align="top_left")], {"po": 0.5})],
                    {}, {("pi", "outer"): "inner", ("po", "h"): "outer"})
    half = nc.draw_native(device, faded).astype(int)
    full = nc.draw_native(device, nc.Case("laid_over_full", uploads, faded.scenes[:2] + [nc.bloom(
        [SceneItem(source="bg", align="top_left"), SceneItem(source="po", align="top_left")])], {},
        faded.parts)).astype(int)
    assert np.abs(half - (full + bare.astype(int)) // 2).max() <= 3


@needs_engine
def test_a_sprig_that_only_wraps_one_picture_draws_as_that_picture_would(device):
    """OBS gardens wrap each browser page in a nested scene and cut, scale and place that: the sprig holds one
    source of its own size, untouched. Its placement is drawn straight from the source (no picture for the
    sprig), and must look as the source placed that way would: cut, scaled, turned, filtered, faded, and through
    a second wrapper too. A wrapper whose source is moved inside it is not one: it keeps its picture."""
    w, h = nc.W, nc.H
    uploads = [("bg", nc.picture(w, h, seed=9), "RGBA", None), ("page", nc.picture(w, h, seed=3, alpha=True), "RGBA", None)]
    how = {"x": 30, "y": 12, "scale": 0.59, "crop": (20, 6, 30, 10), "rotation": 8, "align": "top_left"}
    fx = {"placed": VideoEffects(shape=nc.SHAPE_IDS["circle"], border=2.0, opacity=0.8)}
    wrap = native_snapshot.SceneDraw("wrap", nc.scene("wrap", [SceneItem(source="page", align="top_left")]), (w, h))
    wrap2 = native_snapshot.SceneDraw("wrap2", nc.scene("wrap2", [SceneItem(source="inner", align="top_left")]), (w, h))
    bg = SceneItem(source="bg", align="top_left")
    wrapped = nc.Case("wrapped", uploads, [wrap, wrap2, nc.bloom([bg, SceneItem(source="placed", **how)], {"placed": 0.7})],
                      fx, {("inner", "wrap2"): "wrap", ("placed", "h"): "wrap2"})
    direct = nc.Case("wrapped_direct", uploads, [nc.bloom([bg, SceneItem(source="page", **how)], {"page": 0.7})],
                     {"page": fx["placed"]}, {})
    got, want = nc.draw_native(device, wrapped), nc.draw_native(device, direct)
    stats = nc.compare(got, want)
    assert stats["max"] <= 3, stats
    assert nc.compare(got, nc.draw_native(device, nc.Case("bare", uploads, [nc.bloom([bg])])))["mean"] > 1  # it shows
    moved = native_snapshot.SceneDraw("wrap", nc.scene("wrap", [SceneItem(source="page", x=40, y=0, align="top_left")]),
                                      (w, h))
    off = nc.draw_native(device, nc.Case("wrapped_moved", uploads, [moved, nc.bloom([bg, SceneItem(source="placed", **how)])],
                                         {}, {("placed", "h"): "wrap"}))
    plain = nc.draw_native(device, nc.Case("wrapped_plain", uploads, [wrap, nc.bloom([bg, SceneItem(source="placed", **how)])],
                                           {}, {("placed", "h"): "wrap"}))
    assert nc.compare(off, plain)["mean"] > 0.5  # the page sits 40 px to the right inside its sprig


def _nv12(i420_rows: np.ndarray, w: int, h: int) -> np.ndarray:
    """The same picture as NV12: Y rows, then U and V interleaved (UVUV...)."""
    uv = i420_rows[h:]
    inter = np.empty_like(uv)
    inter[:, 0::2], inter[:, 1::2] = uv[:, : w // 2], uv[:, w // 2:]
    return np.concatenate([i420_rows[:h], inter])


@needs_engine
def test_an_nv12_picture_draws_exactly_as_its_i420_twin(device):
    """Zero-copy, step 1: an NV12 picture (repacked on the card, or here on a card that can't read NV12)
    lands in the compositor line for line the same as the I420 upload of it."""
    w, h = 48, 32
    i420 = nc.i420(nc.picture(w, h, seed=3))
    nv12 = _nv12(i420, w, h)
    drawn = []
    for key, rows, order in (("i", i420, "I420/709/tv"), ("n", nv12, "NV12/709/tv"), ("n", nv12, "NV12/709/tv")):
        device.upload(key, rows, order)
        scene = Scene(id="s" + key, name="s", canvas="h", items=[SceneItem(source=key, align="top_left")])
        built = native_snapshot.build([native_snapshot.SceneDraw("s" + key, scene, (w, h))], {})
        device.set_snapshot(built.struct)
        with sprigcore.Canvas(device, w, h) as c:
            c.render("s" + key)
            drawn.append(c.read_bgra())
    assert drawn[0] == drawn[1] == drawn[2]  # the second NV12 upload reuses the source's textures
    device.forget("i")
    device.forget("n")


# ---- the engine's own frame loop (phase 2, part 2) ------------------------------------------------------------------

STILL_CASES = [c for c in CASES if c.times == (0.0,) and c.transition is None]  # the engine draws at its own clock


@needs_engine
@pytest.mark.parametrize("case", STILL_CASES, ids=[c.name for c in STILL_CASES])
def test_the_engines_frame_loop_draws_like_todays_compositor(case, tmp_path):
    """The same cases, drawn by the engine's frame thread from work handed over without waiting."""
    main = case.scenes[-1]
    with sprigcore.Engine(warp=True, fps=(30, 1)) as eng:
        for source_id, array, order, size in case.uploads:
            eng.upload(source_id, array, order, size, keep=True)
        native_snapshot.hand_over(eng, native_snapshot.build(case.scenes, case.effects, case.parts),
                                  native_snapshot.Resources())
        eng.set_canvas(0, *main.size)
        eng.show(0, main.key)
        bgra = np.frombuffer(eng.read_bgra(0), np.uint8).reshape(main.size[1], main.size[0], 4)
        stats = eng.stats()
    got = np.ascontiguousarray(bgra[..., [2, 1, 0, 3]])
    diff = nc.compare(got, golden(case))
    if not nc.close_enough(diff):
        Image.fromarray(got, "RGBA").save(tmp_path / f"{case.name}-engine.png")
    assert nc.close_enough(diff), f"{case.name}: {diff} (engine picture in {tmp_path})"
    assert stats["running"] and not stats["faulted"] and stats["ticks"] >= 2


@needs_engine
def test_the_engine_rebuilds_after_faults_and_its_kept_pictures_come_back(device):
    pic = nc.picture(16, 16)
    scene = Scene(id="b", name="b", canvas="h", items=[SceneItem(source="a", align="top_left")])
    built = native_snapshot.build([native_snapshot.SceneDraw("b", scene, (16, 16))], {})
    device.upload("a", pic)
    device.set_snapshot(built.struct)
    with sprigcore.Canvas(device, 16, 16) as c:
        c.render("b")
        want = c.read_bgra()
    device.forget("a")
    with sprigcore.Engine(warp=True, fps=(30, 1)) as eng:
        eng.upload("a", pic, keep=True)
        eng.set_snapshot(built.struct)
        eng.set_canvas(0, 16, 16)
        eng.show(0, "b")
        assert eng.read_bgra(0) == want
        for n, kind in enumerate((1, 3), start=1):  # a C++ exception, a lost graphics card
            eng.debug_fault(kind)
            deadline = time.monotonic() + 3
            while eng.stats()["rebuilds"] < n and time.monotonic() < deadline:
                time.sleep(0.02)
            stats = eng.stats()
            assert stats["rebuilds"] == n and not stats["faulted"]
            assert eng.read_bgra(0) == want
        eng.set_canvas(0, 0, 0)
        with pytest.raises(sprigcore.NativeError):
            eng.read_bgra(0)


def _next_output_frame(eng, share: bool, within: float) -> bool:
    """Whether output slot 0 of canvas 0 hands over a frame within `within` seconds (given straight back)."""
    end = time.monotonic() + within
    while time.monotonic() < end:
        if share:
            state, frame, _dropped = eng.take_texture(0, 0)
            if state < 0:
                pytest.skip("this device can't keep output frames on the card")
            if frame is not None:
                texture, pool, entry, _drawn = frame
                sprigcore.com_release(texture)
                eng.give_back(0, 0, pool, entry)
                return True
        elif eng.take_output(0, 0, 64, 36) is not None:
            return True
        time.sleep(0.01)
    return False


@needs_engine
@pytest.mark.parametrize("share", [True, False], ids=["on-card", "memory"])
def test_outputs_made_again_while_a_frame_is_stuck_still_get_frames(share):
    """Stuck-frame crash run (2026-11-17): the outputs were made again while the frame thread was stuck; it came
    back to the same size under a new setting, kept its old texture pool, and the stream froze for good."""
    with sprigcore.Engine(warp=True, fps=(30, 1)) as eng:
        eng.set_canvas(0, 64, 36)
        eng.set_output(0, 0, 64, 36, share=share)
        assert _next_output_frame(eng, share, 5.0)
        eng.debug_fault(4)  # the next frame sticks for 3 s
        time.sleep(0.5)
        assert eng.stats()["running"]
        eng.set_output(0, 0, 0, 0)  # what reset_outputs and the next tick do, both inside the stuck frame
        eng.set_output(0, 0, 64, 36, share=share)
        time.sleep(3.0)
        assert _next_output_frame(eng, share, 5.0), "no frames after the stuck frame"
        assert not eng.stats()["faulted"]


@needs_engine
@pytest.mark.parametrize("shape", nc.NV12_SHAPES[:2], ids=[n for n, _s, _h in nc.NV12_SHAPES[:2]])
def test_the_engines_outputs_in_memory_are_the_picture(shape, tmp_path):
    """Virtual camera on native (PC, 2026-11-17): every frame flat dark green. Its frames come from an engine
    output read back to memory: those frames must be the canvas, as Python's compositor gives encoders."""
    name, size, how = shape
    case = CASES[0]
    main = case.scenes[-1]
    want = np.asarray(Image.open(nc.GOLDEN / f"{name}.png").convert("L"))
    with sprigcore.Engine(warp=True, fps=(30, 1)) as eng:
        for source_id, array, order, layout in case.uploads:
            eng.upload(source_id, array, order, layout, keep=True)
        native_snapshot.hand_over(eng, native_snapshot.build(case.scenes, case.effects, case.parts),
                                  native_snapshot.Resources())
        eng.set_canvas(0, *main.size)
        eng.show(0, main.key)
        eng.read_bgra(0)  # drawn by now
        eng.set_output(0, 0, size[0], size[1], how)
        got, end = None, time.monotonic() + 5.0
        while time.monotonic() < end:
            frame = eng.take_output(0, 0, size[0], size[1])
            if frame is not None:
                got = frame[0]  # the newest one wins: the first may predate the picture
            elif got is not None:
                break
            time.sleep(0.05)
    assert got is not None, "no output frame"
    planes = nc.nv12_picture(got, size)
    stats = nc.compare(planes[..., None], want[..., None])
    if not nc.close_enough(stats):
        Image.fromarray(planes, "L").save(tmp_path / f"{name}-engine-output.png")
    assert nc.close_enough(stats), f"{name}: {stats} (engine output in {tmp_path})"

