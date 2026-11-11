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
    assert tuple(pixels(compositor, canvas)[10, 10]) == (0, 0, 0, 255)


def test_vertical_canvas_renders_independently(compositor):
    vertical = Canvas(id="v", width=36, height=64)
    scene = Scene(id="sv", name="sv", canvas="v", items=[SceneItem(source="green", x=0, y=54)])
    compositor.render(vertical, scene, {"green": (10, 10)})
    img = pixels(compositor, vertical)
    assert img.shape == (64, 36, 4)
    assert tuple(img[60, 5]) == (0, 255, 0, 255)
    assert tuple(img[5, 5]) == (0, 0, 0, 255)


def test_bgra_frames_are_swizzled_on_the_gpu(compositor):
    canvas = Canvas(id="b", width=8, height=8)
    bgra_red = np.full((8, 8, 4), (0, 0, 255, 255), dtype=np.uint8)  # red, stored as B G R A
    compositor.upload("cap", bgra_red, "BGRA")
    scene = Scene(id="s", name="s", canvas="b", items=[SceneItem(source="cap")])
    compositor.render(canvas, scene, {"cap": (8, 8)})
    assert tuple(pixels(compositor, canvas)[4, 4]) == (255, 0, 0, 255)
