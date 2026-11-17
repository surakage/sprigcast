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


def test_painted_pictures_go_up_premultiplied_without_a_conversion(compositor):
    """Death count and Snap hand over QPainter's own pixels (colours already times alpha): no copy per frame."""
    canvas = Canvas(id="pm", width=8, height=8)
    half_red = np.full((8, 8, 4), (0, 0, 128, 128), dtype=np.uint8)  # 50% red, premultiplied, B G R A
    compositor.upload("dc", half_red, "pBGRA")
    scene = Scene(id="s", name="s", canvas="pm", items=[SceneItem(source="dc")])
    compositor.render(canvas, scene, {"dc": (8, 8)})
    r, g, b, a = pixels(compositor, canvas)[4, 4]
    assert abs(int(r) - 128) <= 3 and g == 0 and b == 0 and abs(int(a) - 128) <= 3  # not darkened twice
    compositor.upload("dc", np.full((8, 8, 4), (0, 0, 255, 255), dtype=np.uint8), "BGRA")
    assert "dc" not in compositor.premultiplied  # back to a straight source
    compositor.forget("dc")


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


def test_downscale_filters_keep_flat_colour_and_differ_on_detail():
    import numpy as np
    import pytest

    from sprigcast.core.scene import Canvas, Scene, SceneItem
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no OpenGL context: {exc}")
    try:
        canvas = Canvas(id="c", width=64, height=36)
        img = np.zeros((36, 64, 4), np.uint8)
        img[..., 3] = 255
        img[:, ::2, :3] = 255  # fine stripes: each filter blends them differently
        comp.upload("s", img)
        scene = Scene(id="s", name="s", canvas="c", items=[SceneItem(source="s")])
        comp.render(canvas, scene, {"s": (64, 36)})
        lumas = {}
        for mode in ("bilinear", "area", "bicubic", "lanczos"):
            comp.downscale = {"c": mode}
            y = np.frombuffer(bytes(comp.read_nv12("c", (32, 18))), np.uint8)[: 32 * 18].reshape(18, 32)
            lumas[mode] = y
        assert abs(int(lumas["area"].mean()) - 126) < 12  # half white, half black
        assert len({m.tobytes() for m in lumas.values()}) >= 2
        flat = np.full((36, 64, 4), (120, 120, 120, 255), np.uint8)
        comp.upload("s", flat)
        comp.render(canvas, scene, {"s": (64, 36)})
        for mode in ("bicubic", "lanczos"):
            comp.downscale = {"c": mode}
            y = np.frombuffer(bytes(comp.read_nv12("c", (32, 18))), np.uint8)[: 32 * 18]
            assert abs(int(y.mean()) - int(y[0])) <= 1 and y.std() < 1.0
    finally:
        comp.release()


def test_canvas_output_size():
    from sprigcast.core.scene import Canvas

    assert Canvas(id="h", width=1920, height=1080).output_size() == (1920, 1080)
    assert Canvas(id="h", width=1920, height=1080, output_short_side=720).output_size() == (1280, 720)
    assert Canvas(id="v", width=1080, height=1920, output_short_side=720).output_size() == (720, 1280)
    assert Canvas(id="h", width=1280, height=720, output_short_side=1080).output_size() == (1280, 720)


def test_targets_an_ended_recording_used_are_let_go(compositor):
    # A recording's NV12 copy (with two read-back buffers) and source record's clean canvas used to stay
    # until SprigCast closed; the canvas itself stays.
    import time

    compositor.ctx.__enter__()  # earlier tests may have made their own GL context current
    compositor.prune(now=time.monotonic() + 100)  # this compositor is shared: drop what earlier tests left
    canvas = Canvas(id="pr", width=64, height=36)
    clean = canvas.model_copy(update={"id": "clean:pr"})
    compositor.render(canvas, None, {})
    compositor.render(clean, None, {})
    compositor.read_nv12_later("pr", (32, 18), "t", time.monotonic())
    compositor.read_nv12_later("clean:pr", (32, 18), "t", time.monotonic())
    compositor.read_scaled("pr", (16, 8))
    compositor.prune(idle_s=60)  # just used: kept (a long idle_s, so a slow machine can't age them meanwhile)
    assert ("pr", (32, 18)) in compositor.nv12_targets and ("pr", (16, 8)) in compositor.scaled_targets
    assert "clean:pr" in compositor.targets
    # Far past both the idle time and the transition keep time, whatever the clock's resolution.
    assert compositor.prune(now=time.monotonic() + 100) >= 4
    assert ("pr", (32, 18)) not in compositor.nv12_targets and ("clean:pr", (32, 18)) not in compositor.nv12_targets
    assert ("pr", (16, 8)) not in compositor.scaled_targets and "clean:pr" not in compositor.targets
    assert "pr" in compositor.targets
    compositor.read_nv12_later("pr", (32, 18), "t", time.monotonic())  # and comes back when wanted again
    assert ("pr", (32, 18)) in compositor.nv12_targets


@pytest.mark.parametrize("fmt,colorspace,color_range", [("yuv420p", 2, 0), ("yuva420p", 1, 0), ("yuv420p", 1, 2)])
def test_video_planes_convert_on_the_gpu_like_ffmpeg(compositor, fmt, colorspace, color_range):
    """Video files hand over packed YUV planes (sources.media.pack_i420); the shader must match FFmpeg's RGBA."""
    av = pytest.importorskip("av")
    from sprigcast.sources.media import pack_i420

    compositor.ctx.__enter__()  # earlier tests made their own GL context current
    w, h = 64, 36
    frame = av.VideoFrame(w, h, fmt)
    for i, plane in enumerate(frame.planes):
        rows = h if i in (0, 3) else h // 2
        ramp = np.linspace(40 + 30 * i, 200 - 20 * i, plane.line_size)  # smooth, like real pictures
        data = np.tile(ramp, (rows, 1)) + np.linspace(0, 20, rows)[:, None]
        if i == 3:
            data[:] = 255  # opaque: the readback is then the colour itself
        plane.update(np.clip(data, 0, 255).astype(np.uint8).tobytes())
    frame.colorspace, frame.color_range = colorspace, color_range
    packed, order = pack_i420(frame, (w, h))
    assert packed.shape == ((h + h // 2 + (h if fmt == "yuva420p" else 0)), w)
    want = frame.reformat(format="rgba").to_ndarray().astype(int)
    canvas = Canvas(id="yuv", width=w, height=h)
    compositor.upload("vid", packed, order)
    compositor.render(canvas, Scene(id="s", name="s", canvas="yuv", items=[SceneItem(source="vid")]), {"vid": (w, h)})
    got = pixels(compositor, canvas).astype(int)
    diff = np.abs(got - want)
    bad = np.argwhere(diff > 8)
    assert diff.max() <= 8, (order, diff.max(), bad[:3].tolist(), got[tuple(bad[0][:2])].tolist(), want[tuple(bad[0][:2])].tolist())
    compositor.forget("vid")
    assert "vid" not in compositor.planar


def test_a_sprig_nothing_shows_lets_go_of_its_texture(compositor):
    """Soak 2026-11-15: every sprig ever shown kept a full-canvas texture until SprigCast closed."""
    import time

    from sprigcast.core.scene import Scene

    compositor.ctx.__enter__()
    compositor.render_part("gone", (64, 36), Scene(id="gone", name="Gone", canvas="pr", kind="part"), {})
    assert "part:gone" in compositor.targets
    compositor.prune(now=time.monotonic() + 100)
    assert "part:gone" not in compositor.targets
    compositor.render_part("gone", (64, 36), Scene(id="gone", name="Gone", canvas="pr", kind="part"), {})
    assert "part:gone" in compositor.targets  # back as soon as it's shown again


def test_a_sharp_source_with_more_pixels_fills_the_same_box(compositor):
    canvas = Canvas(id="d", width=40, height=20)
    big = np.zeros((40, 80, 4), dtype=np.uint8)  # drawn at 2x its 40 x 20 box: left half red, right half green
    big[:, :40] = (255, 0, 0, 255)
    big[:, 40:] = (0, 255, 0, 255)
    compositor.upload("sharp", big, "RGBA", (40, 20))
    assert compositor.detail["sharp"] == (2.0, (40, 20))
    scene = Scene(id="s", name="s", canvas="d", items=[SceneItem(source="sharp")])
    compositor.render(canvas, scene, {"sharp": (40, 20)})
    img = pixels(compositor, canvas)
    assert tuple(img[10, 5]) == (255, 0, 0, 255) and tuple(img[10, 35]) == (0, 255, 0, 255)
    compositor.upload("sharp", big[::2, ::2].copy(), "RGBA", (40, 20))  # back to its own size
    assert "sharp" not in compositor.detail


def test_filters_in_source_pixels_look_the_same_at_any_detail(compositor):
    from sprigcast.core.filters import VideoEffects

    fx = VideoEffects(blur=3.0, pixelate=4.0, cutout_blur=2.0)
    doubled = compositor._in_detail(fx, 2.0)
    assert (doubled.blur, doubled.pixelate, doubled.cutout_blur) == (6.0, 8.0, 4.0)
    assert compositor._in_detail(fx, 2.0) is doubled  # made once, not every frame
    plain = VideoEffects()
    assert compositor._in_detail(plain, 2.0) is plain


def test_a_preview_copy_waits_for_the_window_to_come_back(compositor):
    """Minimized, the window asks for no preview; made again on its return, the first copy into fresh read-back
    buffers held the renderer 330 ms on the PC (2026-11-16, at rest). A preview's copy stays a while; an
    output's goes after a few seconds as before."""
    import time

    compositor.ctx.__enter__()
    compositor.prune(now=time.monotonic() + 10_000)
    canvas = Canvas(id="pv", width=64, height=36)
    compositor.render(canvas, None, {})
    now = time.monotonic()
    compositor.read_scaled_later("pv", (32, 18), "p", now, optional=True)  # the window's preview
    kept = compositor.scaled_targets[("pv", (32, 18))]
    compositor.prune(now=now + 120)  # two minutes minimized
    assert compositor.scaled_targets.get(("pv", (32, 18))) is kept
    compositor.prune(now=now + compositor.PREVIEW_KEEP_S + 1)  # gone for good after a long while
    assert ("pv", (32, 18)) not in compositor.scaled_targets
    compositor.read_scaled_later("pv", (32, 18), "p", time.monotonic())  # not a preview: let go soon as before
    assert compositor.prune(now=time.monotonic() + 100) >= 1 and ("pv", (32, 18)) not in compositor.scaled_targets
