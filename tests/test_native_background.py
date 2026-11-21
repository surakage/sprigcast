"""The Background filter: a person found by a small model on the graphics card (native/core/src/segmenter.h), the
room behind them blurred, swapped for a picture or taken out.

The engine runs native/core/models/selfie-wide.scseg, made by native/core/tools/make_segmenter.py, which also runs
the same program in numpy (run_program). The engine's mask must match it."""

import importlib.util
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from sprigcast import sprigcore
from sprigcast.core import filters
from sprigcast.core.filters import BACKGROUND, VideoEffects, video_effects
from sprigcast.core.scene import Filter, Scene, SceneItem
from sprigcast.render import native_snapshot

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "native" / "core" / "models" / "selfie-wide.scseg"


def _tool():
    spec = importlib.util.spec_from_file_location("make_segmenter", ROOT / "native" / "core" / "tools" /
                                                  "make_segmenter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOL = _tool()
W, H = 256, 144  # the model's own size: a picture this size is drawn 1:1, so the engine's input is exact


def person(width: int = W, height: int = H) -> np.ndarray:
    """A drawn person at a desk (the model finds them), RGBA."""
    sx, sy = width / W, height / H
    im = Image.new("RGB", (width, height), (70, 90, 120))
    d = ImageDraw.Draw(im)

    def box(x0, y0, x1, y1):
        return [x0 * sx, y0 * sy, x1 * sx, y1 * sy]

    d.rectangle(box(0, 100, 256, 144), fill=(110, 80, 60))
    d.ellipse(box(88, 95, 168, 190), fill=(40, 40, 50))
    d.rectangle(box(120, 80, 136, 100), fill=(220, 170, 140))
    d.ellipse(box(105, 20, 151, 85), fill=(225, 175, 145))
    d.ellipse(box(103, 14, 153, 45), fill=(50, 30, 20))
    d.ellipse(box(117, 45, 123, 51), fill=(30, 30, 30))
    d.ellipse(box(133, 45, 139, 51), fill=(30, 30, 30))
    return np.asarray(im.convert("RGBA"), np.uint8).copy()


def smoothstep(a, b, x):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


# ---- the filter and the snapshot (no engine needed) -------------------------------------------------------------

def bg(enabled=True, **settings) -> Filter:
    return Filter(id="f", kind=BACKGROUND, name="Background", enabled=enabled, settings=settings)


def test_the_filter_folds_into_effects():
    fx = video_effects([bg(mode="blur", blur=40, edge=10)])
    assert (fx.bg_mode, fx.bg_blur, fx.bg_edge, fx.bg_picture) == (2, 0.4, 0.1, "")
    fx = video_effects([bg(mode="picture", picture_file=" room.png ")])
    assert fx.bg_mode == 3 and fx.bg_picture == "room.png"
    fx = video_effects([bg(mode="remove", picture_file="room.png")])
    assert fx.bg_mode == 1 and fx.bg_picture == ""
    assert video_effects([bg(enabled=False)]).bg_mode == 0


def test_the_filter_is_listed_with_its_settings():
    info = filters.filter_kind(BACKGROUND)
    assert info is not None and info.label == "Backdrop" and info.media == "video"
    assert [f.key for f in info.fields] == ["mode", "blur", "picture_file", "edge"]
    assert [v for v, _label in info.fields[0].choices] == ["blur", "picture", "remove"]  # A to Z
    assert isinstance(info.status("cam"), str)


def test_the_old_plugins_kind_stays_retired():
    from sprigcast.core.scene import RETIRED_FILTER_KINDS

    assert "background" in RETIRED_FILTER_KINDS and BACKGROUND not in RETIRED_FILTER_KINDS


def test_snapshot_carries_the_background_and_lists_its_picture():
    scene = Scene(id="b", name="b", canvas="h", items=[SceneItem(source="cam")])
    fx = VideoEffects(bg_mode=3, bg_blur=0.5, bg_edge=0.2, bg_picture="room.png")
    built = native_snapshot.build([native_snapshot.SceneDraw("b", scene, (8, 8))], {"cam": fx})
    e = built.struct.effects[0]
    assert (e.bg_mode, round(e.bg_blur, 3), round(e.bg_edge, 3)) == (3, 0.5, 0.2)
    assert e.bg_picture == sprigcore.key("backdrop:room.png")
    assert built.backdrops == {"room.png"} and built.masks == set()


def test_the_model_finds_the_person():
    program = TOOL.read_program(str(MODEL))
    assert (program["in_w"], program["in_h"]) == (W, H)
    mask = TOOL.run_program(program, person()[..., :3].astype(np.float32) / 255)
    assert mask.shape == (H, W)
    assert mask[130, 128] > 0.9 and mask[50, 128] > 0.9  # body and face
    assert mask[10, 10] < 0.05 and mask[60, 230] < 0.05 and mask[130, 20] < 0.05  # wall and desk


# ---- the engine ------------------------------------------------------------------------------------------------

needs_engine = pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")


@pytest.fixture(scope="module")
def device():
    with sprigcore.Device(warp=True) as dev:
        yield dev


def draw(device, fx: VideoEffects, picture: np.ndarray, size=(W, H), backdrop: np.ndarray | None = None):
    device.upload("cam", picture)
    if backdrop is not None:
        device.set_mask(native_snapshot.backdrop_name("room.png"), backdrop, False)
    scene = Scene(id="b", name="b", canvas="h", items=[SceneItem(source="cam", align="top_left")])
    built = native_snapshot.build([native_snapshot.SceneDraw("b", scene, size)], {"cam": fx})
    native_snapshot.hand_over(device, built)
    with sprigcore.Canvas(device, size[0], size[1]) as canvas:
        canvas.render("b")
        got = np.frombuffer(canvas.read_bgra(), np.uint8).reshape(size[1], size[0], 4)[..., [2, 1, 0, 3]]
    device.forget("cam")
    return got.astype(np.float32) / 255


@needs_engine
def test_remove_cuts_the_room_out_where_the_model_says(device, tmp_path):
    pic = person()
    want = smoothstep(0.1, 0.9, TOOL.run_program(TOOL.read_program(str(MODEL)), pic[..., :3].astype(np.float32) / 255))
    got = draw(device, VideoEffects(bg_mode=1, bg_edge=1.0), pic)
    diff = np.abs(got[..., 3] - want)
    if diff.max() > 0.06:
        Image.fromarray((got * 255).astype(np.uint8), "RGBA").save(tmp_path / "remove.png")
    assert diff.max() <= 0.06, f"largest difference {diff.max():.3f} (picture in {tmp_path})"
    status = sprigcore.background_status()
    assert status["state"] == "running", status


@needs_engine
def test_blur_keeps_the_person_and_softens_the_room(device):
    pic = person()
    got = draw(device, VideoEffects(bg_mode=2, bg_blur=1.0, bg_edge=0.0), pic)
    src = pic.astype(np.float32) / 255
    assert np.abs(got[130, 128, :3] - src[130, 128, :3]).max() < 0.02  # the person, untouched
    assert got[..., 3].min() > 0.99  # nothing see-through
    # The room's edge at the desk is blurred: the sharp line from wall to desk becomes a slope.
    column = got[90:110, 30, :3].mean(axis=1)
    assert (np.abs(np.diff(column)) < 0.05).all()
    assert np.abs(got[100, 30, :3] - src[100, 30, :3]).max() > 0.02


@needs_engine
def test_picture_fills_the_room(device):
    pic = person()
    room = np.zeros((90, 160, 4), np.uint8)
    room[...] = (200, 20, 160, 255)
    got = draw(device, VideoEffects(bg_mode=3, bg_edge=0.0, bg_picture="room.png"), pic, backdrop=room)
    src = pic.astype(np.float32) / 255
    assert np.abs(got[10, 10, :3] - np.array([200, 20, 160]) / 255).max() < 0.02
    assert np.abs(got[130, 128, :3] - src[130, 128, :3]).max() < 0.02


@needs_engine
def test_a_tall_or_large_picture_works_too(device):
    """Pictures of any shape are letterboxed into the model's size: a 720p camera and a vertical one."""
    big = draw(device, VideoEffects(bg_mode=1, bg_edge=0.5), person(1280, 720), size=(1280, 720))
    assert big[650, 640, 3] > 0.9 and big[50, 50, 3] < 0.1
    tall = np.ascontiguousarray(np.rot90(person(), 1))
    got = draw(device, VideoEffects(bg_mode=1), tall, size=(H, W))
    assert got.shape == (W, H, 4)
