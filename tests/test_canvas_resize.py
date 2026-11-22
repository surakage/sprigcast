"""A smaller canvas takes its blooms with it, and Optimize offers 720p canvases on a light PC (tester PC 2026-11-22)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sprigcast.core.scene import PART_KIND, Canvas, Scene, SceneCollection, SceneItem, SourceConfig  # noqa: E402


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080, fps=30),
                  Canvas(id="vertical", width=1080, height=1920, fps=30)],
        sources=[SourceConfig(id="cam", kind="camera", name="Camera"),
                 SourceConfig(id="part-src", kind=PART_KIND, name="Frame", settings={"scene": "frame"})],
        scenes=[Scene(id="main", name="Main", canvas="horizontal",
                      items=[SceneItem(source="cam", x=960, y=540, scale=0.5, scale_y=0.25),
                             SceneItem(source="cam", x=0, y=0, bounds="fit", bounds_size=(1920, 1080)),
                             SceneItem(source="part-src", x=0, y=0, crop=(30, 0, 0, 0))]),
                Scene(id="frame", name="Frame", canvas="horizontal", kind="part",
                      items=[SceneItem(source="cam", x=1800, y=900)]),
                Scene(id="tall", name="Tall", canvas="vertical",
                      items=[SceneItem(source="part-src", x=0, y=100, scale=0.5),
                             SceneItem(source="cam", x=10, y=20, scale=2.0)])],
    )


def test_items_keep_their_place_and_size_relative_to_the_canvas():
    c = _collection()
    c.resize_canvas("horizontal", 1280, 720)
    assert (c.canvases[0].width, c.canvases[0].height) == (1280, 720)
    cam, boxed, frame = c.scenes[0].items
    assert (cam.x, cam.y) == pytest.approx((640, 360))
    assert (cam.scale, cam.scale_y) == pytest.approx((1 / 3, 1 / 6))
    assert boxed.bounds_size == pytest.approx((1280, 720))
    # the sprig's picture is the canvas size, so it already shrank: its scale stays, its crop follows
    assert frame.scale == 1.0 and frame.crop == (20, 0, 0, 0)
    assert (c.scenes[1].items[0].x, c.scenes[1].items[0].y) == pytest.approx((1200, 600))
    # on the vertical canvas: the horizontal sprig drew smaller now, so it grows back; the camera is untouched
    tall_frame, tall_cam = c.scenes[2].items
    assert tall_frame.scale == pytest.approx(0.75) and (tall_frame.x, tall_frame.y) == (0, 100)
    assert (tall_cam.x, tall_cam.y, tall_cam.scale) == (10, 20, 2.0)


def test_another_shape_is_fitted_and_centred():
    c = _collection()
    c.resize_canvas("horizontal", 1080, 1080)
    cam = c.scenes[0].items[0]
    assert cam.scale == pytest.approx(0.5 * 1080 / 1920)
    assert (cam.x, cam.y) == pytest.approx((960 * 0.5625, 540 * 0.5625 + (1080 - 1080 * 0.5625) / 2))


def test_same_size_changes_nothing():
    c = _collection()
    before = c.model_dump()
    c.resize_canvas("horizontal", 1920, 1080)
    assert c.model_dump() == before


def test_optimize_offers_a_720p_canvas_on_a_light_pc_only():
    pytest.importorskip("PySide6.QtWidgets")
    from sprigcast.app import optimize
    from sprigcast.core.scene import Output

    def collection():
        c = _collection()
        c.outputs = [Output(id="tw", type="stream", service="twitch", canvas="horizontal", bitrate_kbps=3000,
                            enabled=True)]
        c.canvases[1].enabled = False
        return c

    light = optimize.PcProfile(threads=4, memory_gb=4.0, codec="h264_amf", upload_kbps=10000)
    plan = optimize.plan(light, collection())
    sized = [ch for ch in plan.changes if ch.size]
    assert [ch.size for ch in sized] == [{"horizontal": (1280, 720)}]
    assert sized[0].label == "Horizontal canvas: 1280×720, 30 fps, blooms scaled to fit (after a restart)"
    assert sized[0].now == "1920×1080, 30 fps" and not sized[0].fps
    assert not [ch for ch in plan.changes if ch.label.startswith("Horizontal canvas: 30 fps")]
    assert plan.sizes(sized) == {"horizontal": (1280, 720)}
    strong = optimize.PcProfile(threads=16, memory_gb=32.0, codec="h264_nvenc", upload_kbps=20000)
    assert not [ch for ch in optimize.plan(strong, collection()).changes if ch.size]


def test_queued_size_scales_the_blooms_at_the_next_start(monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import QCoreApplication, QSettings

    from sprigcast.app import lowend
    from sprigcast.app.settings_window import PENDING_VIDEO, apply_pending_video

    QCoreApplication.setOrganizationName("SprigCastTest")
    QCoreApplication.setApplicationName("canvas-resize-test")
    QSettings().remove(PENDING_VIDEO)
    c = _collection()
    try:
        assert lowend.store_fps({}, c, {"horizontal": (1280, 720)})
        assert apply_pending_video(c)
        assert (c.canvases[0].width, c.canvases[0].height) == (1280, 720)
        assert c.scenes[0].items[0].scale == pytest.approx(1 / 3)
    finally:
        QSettings().remove(PENDING_VIDEO)
