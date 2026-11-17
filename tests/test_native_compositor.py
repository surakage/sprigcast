"""The native compositor against today's (docs/native-engine.md, phase 2), and the snapshot it draws from.

The pictures in tests/native_golden are today's compositor's drawing of the cases in tests/native_cases.py.
The engine must draw the same; today's compositor must still draw them too, or they are remade."""

import numpy as np
import pytest
from PIL import Image

from tests import native_cases as nc
from sprigcast import sprigcore
from sprigcast.core.filters import VideoEffects
from sprigcast.core.scene import Scene, SceneItem
from sprigcast.render import native_snapshot

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
