"""SPRIGCAST_ENGINE=native: the native engine drawing the previews (render/native_bridge.py) and how the render
thread hands over to it and takes back from it."""

import numpy as np
import pytest

from sprigcast import sprigcore
from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.render import native_bridge
from sprigcast.render.engine import Engine


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36), Canvas(id="v", width=36, height=64)],
        sources=[
            SourceConfig(id="red", kind="color", name="Red", settings={"color": "#ff0000ff", "width": 8, "height": 8}),
            SourceConfig(id="cam", kind="scene", name="Cam frame", settings={"scene": "p-cam"}),
            SourceConfig(id="inner", kind="scene", name="Badge", settings={"scene": "p-badge"}),
        ],
        scenes=[
            Scene(id="show-h", name="Gaming", canvas="h", items=[SceneItem(source="cam"), SceneItem(source="red")]),
            Scene(id="show-v", name="Gaming (v)", canvas="v", items=[SceneItem(source="cam", scale=0.5)]),
            Scene(id="p-cam", name="Cam frame", canvas="h", kind="part",
                  items=[SceneItem(source="red"), SceneItem(source="inner")]),
            Scene(id="p-badge", name="Badge", canvas="h", kind="part"),
        ],
        active_scenes={"h": "show-h", "v": "show-v"},
    )


def _plain_state(collection, layout):
    return layout, None


def test_the_snapshot_holds_what_is_on_air_and_every_sprig():
    col = _collection()
    draws, parts, shows = native_bridge.snapshot_draws(col, _plain_state)
    keys = [d.key for d in draws]
    assert keys == ["canvas:h", "canvas:v", "part:p-cam", "part:p-badge"]
    assert {d.key: d.size for d in draws}["canvas:v"] == (36, 64)
    assert {d.key: d.size for d in draws}["part:p-cam"] == (64, 36)  # a sprig is drawn at its canvas's size
    assert parts == {("cam", "canvas:h"): "part:p-cam", ("cam", "canvas:v"): "part:p-cam",
                     ("inner", "part:p-cam"): "part:p-badge"}
    assert shows == {"h": "canvas:h", "v": "canvas:v"}


def test_a_switched_off_canvas_or_an_empty_one_shows_nothing():
    col = _collection()
    col.canvases[1].enabled = False
    col.active_scenes.pop("h")
    draws, _parts, shows = native_bridge.snapshot_draws(col, _plain_state)
    assert shows == {"h": None}
    assert not [d for d in draws if d.key.startswith("canvas:")]


def test_a_sprig_is_drawn_as_its_live_state_shows_it():
    col = _collection()

    def state(collection, layout):
        return collection.hide_items(layout, {"red"}), {"inner": 0.5}

    draws, _parts, _shows = native_bridge.snapshot_draws(col, state)
    cam = next(d for d in draws if d.key == "part:p-cam")
    assert [i.source for i in cam.scene.items if i.visible] == ["inner"] and cam.fades == {"inner": 0.5}


# ---- the render thread with a stand-in native engine -----------------------------------------------------------

class FakeCompositor:
    def __init__(self):
        self.rendered, self.parts, self.uploaded = [], {}, []

    def upload(self, source_id, *a):
        self.uploaded.append(source_id)

    def render(self, canvas, scene, sizes, previous=None, progress=1.0):
        self.rendered.append(canvas.id)

    def read(self, key):
        return b"x"


class FakeNative:
    """Stands in for NativeCanvases: draws every canvas it is shown, and its pictures are always ready."""

    def __init__(self):
        self.failed = False
        self.uploads, self.watching, self.synced = [], {}, 0

    def upload(self, source_id, frame, order, size):
        self.uploads.append(source_id)

    def forget_gone(self, ids):
        pass

    def sync(self, collection, effects, state_view, fading):
        self.synced += 1

    def check(self, now):
        pass

    def draws(self, canvas_id):
        return not self.failed

    def watch(self, canvas_id, size, fps):
        self.watching[canvas_id] = size

    def take(self, canvas_id):
        size = self.watching.get(canvas_id)
        return (b"n" * (size[0] * size[1] * 4), *size) if size else None

    def close(self):
        pass


def _engine():
    col = SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36), Canvas(id="v", width=36, height=64)],
        scenes=[Scene(id="a", name="A", canvas="h"), Scene(id="b", name="B", canvas="v")],
        active_scenes={"h": "a", "v": "b"},
    )
    engine = Engine(col)
    engine._native = FakeNative()
    return engine


def _run(engine, fake, seconds=1.0, fps=60):
    for i in range(int(seconds * fps)):
        engine._tick(fake, 10.0 + i / fps)


def test_previews_come_from_the_native_engine_and_python_draws_nothing():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    _run(engine, fake)
    assert fake.rendered == [] and engine.readbacks == 0
    frame = engine.frames["h"].take()
    assert frame is not None and frame.data[:1] == b"n" and frame.canvas_size == (64, 36)
    assert engine._native.watching == {"h": (64, 36), "v": None}  # an unwatched preview isn't copied back
    assert engine._native.synced == 60


def test_outputs_are_still_drawn_by_python_while_the_preview_is_native():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    got = []
    engine.add_frame_tap("h", got.append)
    _run(engine, fake)
    assert len(got) == 60 and fake.rendered.count("h") == 60
    assert engine.frames["h"].take().data[:1] == b"n"  # the preview is the native engine's


def test_a_failed_native_engine_hands_the_previews_back_to_python():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    engine._native.failed = True
    _run(engine, fake)
    assert 29 <= fake.rendered.count("h") <= 31
    assert engine.frames["h"].take().data == b"x"


def test_preview_pictures_are_the_size_shown():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    engine.preview_sizes = {"h": (32, 18)}
    _run(engine, fake, seconds=0.1)
    assert engine._native.watching["h"] == (32, 18)
    assert engine.frames["h"].take().width == 32


# ---- the real engine (Windows) ------------------------------------------------------------------------------

needs_engine = pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")


@needs_engine
def test_the_bridge_draws_a_collection_like_the_device():
    col = SceneCollection(
        canvases=[Canvas(id="h", width=32, height=18)],
        sources=[SourceConfig(id="pic", kind="color", name="Pic", settings={"width": 8, "height": 8})],
        scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source="pic", x=4, y=2, align="top_left")])],
        active_scenes={"h": "a"},
    )
    rng = np.random.default_rng(5)
    pic = rng.integers(0, 256, (8, 8, 4), dtype=np.uint8)
    pic[..., 3] = 255
    bridge = native_bridge.NativeCanvases(col, warp=True)
    try:
        bridge.upload("pic", pic, "RGBA", None)
        bridge.sync(col, {}, _plain_state, False)
        bridge.watch("h", (32, 18), 0)
        import time

        got, deadline = None, time.monotonic() + 3
        while got is None and time.monotonic() < deadline:
            got = bridge.take("h")
            time.sleep(0.01)
        assert got is not None and not bridge.failed
        img = np.frombuffer(got[0], np.uint8).reshape(18, 32, 4)
        assert (img[2:10, 4:12] == pic).all()
        assert (img[0, 0] == 0).all()  # see-through where nothing is
    finally:
        bridge.close()
