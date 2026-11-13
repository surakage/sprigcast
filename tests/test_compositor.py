"""GPU tests. Skipped automatically when no OpenGL 3.3 context can be created (for example in CI)."""

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl")

from sprigcast.core.scene import Canvas, Scene, SceneItem  # noqa: E402


@pytest.fixture(scope="module")
def compositor():
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    yield comp
    comp.release()


def pixels(comp, canvas):
    return np.frombuffer(comp.read(canvas.id), dtype=np.uint8).reshape(canvas.height, canvas.width, 4)


def test_items_draw_at_position_back_to_front(compositor):
    canvas = Canvas(id="h", width=64, height=36)
    compositor.upload("red", np.full((36, 64, 4), (255, 0, 0, 255), dtype=np.uint8))
    compositor.upload("green", np.full((10, 10, 4), (0, 255, 0, 255), dtype=np.uint8))
    scene = Scene(id="s", name="s", canvas="h", items=[SceneItem(source="red"), SceneItem(source="green", x=4, y=2)])
    compositor.render(canvas, scene, {"red": (64, 36), "green": (10, 10)})
    img = pixels(compositor, canvas)
    assert tuple(img[0, 0]) == (255, 0, 0, 255)  # top-left is background
    assert tuple(img[3, 5]) == (0, 255, 0, 255)  # row 3, col 5 is inside the green box (top rows first)
    assert tuple(img[20, 30]) == (255, 0, 0, 255)


def test_hidden_items_are_skipped(compositor):
    canvas = Canvas(id="h", width=64, height=36)
    scene = Scene(id="s", name="s", canvas="h", items=[SceneItem(source="red", visible=False)])
    compositor.render(canvas, scene, {"red": (64, 36)})
    assert tuple(pixels(compositor, canvas)[10, 10]) == (0, 0, 0, 0)  # see-through, black once alpha is dropped


def test_vertical_canvas_renders_independently(compositor):
    vertical = Canvas(id="v", width=36, height=64)
    scene = Scene(id="sv", name="sv", canvas="v", items=[SceneItem(source="green", x=0, y=54)])
    compositor.render(vertical, scene, {"green": (10, 10)})
    img = pixels(compositor, vertical)
    assert img.shape == (64, 36, 4)
    assert tuple(img[60, 5]) == (0, 255, 0, 255)
    assert tuple(img[5, 5]) == (0, 0, 0, 0)


def test_bgra_frames_are_swizzled_on_the_gpu(compositor):
    canvas = Canvas(id="b", width=8, height=8)
    bgra_red = np.full((8, 8, 4), (0, 0, 255, 255), dtype=np.uint8)  # red, stored as B G R A
    compositor.upload("cap", bgra_red, "BGRA")
    scene = Scene(id="s", name="s", canvas="b", items=[SceneItem(source="cap")])
    compositor.render(canvas, scene, {"cap": (8, 8)})
    assert tuple(pixels(compositor, canvas)[4, 4]) == (255, 0, 0, 255)


def test_three_channel_bgr_frames_render_opaque(compositor):
    canvas = Canvas(id="c3", width=6, height=4)
    bgr_blue = np.zeros((4, 6, 3), dtype=np.uint8)
    bgr_blue[..., 0] = 255  # odd width x 3 bytes: rows are not 4-byte aligned
    compositor.upload("cam", bgr_blue, "BGR")
    scene = Scene(id="s", name="s", canvas="c3", items=[SceneItem(source="cam")])
    compositor.render(canvas, scene, {"cam": (6, 4)})
    assert tuple(pixels(compositor, canvas)[2, 5]) == (0, 0, 255, 255)


def test_fade_blends_outgoing_scene_over_new_one(compositor):
    canvas = Canvas(id="f", width=32, height=18)
    compositor.upload("red", np.full((18, 32, 4), (255, 0, 0, 255), dtype=np.uint8))
    compositor.upload("blue", np.full((18, 32, 4), (0, 0, 255, 255), dtype=np.uint8))
    old = Scene(id="a", name="a", canvas="f", items=[SceneItem(source="red")])
    new = Scene(id="b", name="b", canvas="f", items=[SceneItem(source="blue")])
    sizes = {"red": (32, 18), "blue": (32, 18)}
    compositor.render(canvas, new, sizes, previous=old, progress=0.25)
    r, g, b, a = pixels(compositor, canvas)[9, 16]
    assert abs(int(r) - 191) <= 2 and abs(int(b) - 64) <= 2 and a == 255
    compositor.render(canvas, new, sizes, previous=old, progress=1.0)
    assert tuple(pixels(compositor, canvas)[0, 31]) == (0, 0, 255, 255)
    compositor.render(canvas, new, sizes, previous=old, progress=0.0)
    assert tuple(pixels(compositor, canvas)[17, 0]) == (255, 0, 0, 255)  # orientation preserved at the corners


def test_fade_from_a_half_empty_scene_keeps_new_scene_visible_in_the_gap(compositor):
    """The outgoing scene's see-through area must not paint black over the incoming one."""
    canvas = Canvas(id="h", width=64, height=36)
    compositor.upload("red", np.full((36, 64, 4), (255, 0, 0, 255), dtype=np.uint8))
    compositor.upload("blue", np.full((36, 32, 4), (0, 0, 255, 255), dtype=np.uint8))
    old = Scene(id="o", name="o", canvas="h", items=[SceneItem(source="blue")])  # left half only
    new = Scene(id="n", name="n", canvas="h", items=[SceneItem(source="red")])
    compositor.render(canvas, new, {"red": (64, 36), "blue": (32, 36)}, previous=old, progress=0.5)
    img = pixels(compositor, canvas)
    assert tuple(img[10, 50]) == (255, 0, 0, 255)  # right half: only the new scene
    r, g, b, a = (int(v) for v in img[10, 10])
    assert abs(r - 128) <= 2 and abs(b - 128) <= 2 and a == 255  # left half: half and half


def test_colour_source_fills_its_size_from_a_tiny_texture(compositor):
    from sprigcast.core.source import create_source

    canvas = Canvas(id="h", width=64, height=36)
    src = create_source("color", "panel", {"color": "#3ddc84", "width": 32, "height": 18})
    compositor.upload("panel", src.video_frame())
    assert compositor.textures["panel"].size == (2, 2)
    item = SceneItem(source="panel", x=16, y=9, crop=(8, 0, 0, 0))  # 8 px cut off the left
    compositor.render(canvas, Scene(id="s", name="s", canvas="h", items=[item]), {"panel": src.size})
    img = pixels(compositor, canvas)
    for row, col in ((9, 16), (26, 39), (17, 28)):  # corners and middle of the 24x18 box: full colour to the edge
        assert tuple(img[row, col]) == (0x3D, 0xDC, 0x84, 255)
    for row, col in ((8, 20), (27, 20), (17, 15), (17, 40)):  # just outside it
        assert tuple(img[row, col]) == (0, 0, 0, 0)
