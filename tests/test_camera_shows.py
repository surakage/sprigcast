"""SprigCast Camera's "Shows" picker: a whole canvas, one bloom or one source, letterboxed."""

import ctypes
import types

import numpy as np

from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.output.virtualcam import HEADER, MAGIC, VIEW_TAP, SharedBlock, VirtualCamera, fit_inside
from sprigcast.render.engine import Engine


class FakeCompositor:
    def __init__(self):
        self.rendered, self.parts = [], {}

    def upload(self, *a):
        pass

    def render(self, canvas, scene, sizes, **kw):
        items = [(i.source, round(i.x), round(i.y), round(i.scale, 3)) for i in scene.items] if scene else []
        self.rendered.append((canvas.id, scene.id if scene else None, items))

    def read(self, key):
        return key.encode()


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36), Canvas(id="v", width=36, height=64)],
        sources=[SourceConfig(id=i, kind="color", name=i) for i in ("game", "cam")],
        scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source="game")]),
                Scene(id="b", name="B", canvas="h", items=[SceneItem(source="cam")]),
                Scene(id="t", name="Tall", canvas="v", items=[SceneItem(source="cam")])],
        active_scenes={"h": "a", "v": "t"},
    )


def test_engine_draws_a_view_of_a_bloom_or_one_source_only_while_tapped():
    engine, fake = Engine(_collection()), FakeCompositor()
    engine.previews = frozenset()
    got = []
    engine.add_frame_tap(VIEW_TAP, got.append)
    engine.set_view("camera", "h", "bloom", "b")  # not the live bloom
    engine._tick(fake, 10.0)
    assert ("view:camera", "b", [("cam", 0, 0, 1.0)]) in fake.rendered and got[-1].data == b"view:camera"
    fake.rendered.clear()
    engine.set_view("camera", "v", "source", "game")
    engine._tick(fake, 10.1)
    view = [r for r in fake.rendered if r[0] == "view:camera"]
    assert len(view) == 1 and view[0][2][0][0] == "game"  # fitted into the vertical canvas
    fake.rendered.clear()
    engine.set_view("camera", None)
    engine._tick(fake, 10.2)
    assert not [r for r in fake.rendered if r[0] == "view:camera"]


def _block(w=64, h=64):
    size = HEADER + w * h * 3 // 2
    buf = ctypes.create_string_buffer(size)
    block = SharedBlock(ctypes.addressof(buf), size)
    block._buf = buf
    ctypes.c_uint32.from_address(block.address).value = MAGIC
    return block


class _Engine:
    def __init__(self):
        self.taps, self.views = [], {}

    def add_frame_tap(self, cid, cb, size=None):
        self.taps.append((cid, cb, size))

    def remove_frame_tap(self, cid, cb):
        self.taps = [t for t in self.taps if not (t[0] == cid and t[1] == cb)]

    def set_view(self, name, canvas_id=None, kind="bloom", ref=""):
        if canvas_id is None:
            self.views.pop(name, None)
        else:
            self.views[name] = (canvas_id, kind, ref)


def _watch(block, w, h, beat=100):
    ctypes.c_uint32.from_address(block.address + 8).value = w
    ctypes.c_uint32.from_address(block.address + 12).value = h
    ctypes.c_uint64.from_address(block.address + 32).value = beat


def test_choice_picks_a_canvas_bloom_or_source_and_letterboxes_it():
    col = _collection()
    choice = ["canvas:v"]
    block, engine = _block(), _Engine()
    cam = VirtualCamera(engine, lambda: col.canvases, open_block=lambda: block, clock=lambda: 100,
                        choice=lambda: choice[0], scenes=lambda: col.scenes)
    _watch(block, 32, 18)  # a landscape camera, showing the vertical canvas
    cam.poll()
    assert engine.taps[0][0] == "v" and engine.taps[0][2] == (10, 18)  # pillarboxed
    # The picture lands in the middle with black (16/128) bars around it.
    frame = types.SimpleNamespace(fmt="nv12", width=10, height=18, data=bytes([200]) * 180 + bytes([90]) * 90)
    engine.taps[0][1](frame)
    y = np.frombuffer(ctypes.string_at(block.address + HEADER, 32 * 18), np.uint8).reshape(18, 32)
    assert y[0, 0] == 16 and y[9, 16] == 200 and (y == 200).sum() == 180
    uv = np.frombuffer(ctypes.string_at(block.address + HEADER + 32 * 18, 32 * 9), np.uint8)
    assert uv[0] == 128 and (uv == 90).sum() == 90

    choice[0] = "bloom:b"
    cam.poll()
    assert engine.taps[0][0] == VIEW_TAP and engine.views == {"camera": ("h", "bloom", "b")}
    choice[0] = "source:cam"
    cam.poll()
    assert engine.views == {"camera": ("h", "source", "cam")}  # the canvas that matches the app's shape
    choice[0] = "auto"
    cam.poll()
    assert engine.taps[0][0] == "h" and engine.views == {}
    choice[0] = "bloom:gone"
    cam.poll()
    assert engine.taps == []


def test_fit_inside_keeps_the_shape_and_even_sizes():
    assert fit_inside((1920, 1080), (1280, 720)) == (1280, 720)
    assert fit_inside((1080, 1920), (1280, 720)) == (404, 720)
    assert fit_inside((1920, 1080), (720, 1280)) == (720, 404)


def test_shows_list_offers_canvases_blooms_and_sources():
    from PySide6.QtWidgets import QApplication

    from sprigcast.app import output_page

    QApplication.instance() or QApplication([])
    col = _collection()
    tab = output_page.CameraTab(col)
    items = [(tab.shows.itemText(i), tab.shows.itemData(i)) for i in range(tab.shows.count())]
    data = [d for _, d in items]
    assert data[0] == "auto"
    assert {"canvas:h", "canvas:v", "bloom:a", "bloom:b", "bloom:t", "source:game", "source:cam"} <= set(data)


def test_number_boxes_get_room_on_both_sides():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QSpinBox

    from sprigcast.app import theme

    app = QApplication.instance() or QApplication([])
    app.setStyle(theme.roomy_style())
    left, centred = QSpinBox(), QSpinBox()
    centred.setAlignment(Qt.AlignmentFlag.AlignCenter)
    for box in (left, centred):
        box.ensurePolished()
    assert left.lineEdit().textMargins().left() == theme.NUMBER_PAD
    m = centred.lineEdit().textMargins()
    assert m.left() == m.right() == theme.NUMBER_PAD // 2


def test_a_view_dropped_by_another_thread_mid_look_is_skipped():
    """PC run 2026-11-18: a slideshow failed to start with KeyError 'replay-instant_replay-1': the render thread
    listed the views, then Instant replay (on a worker) dropped its view before the render thread looked it up."""
    engine = Engine(_collection())
    engine.set_view("replay-instant_replay-1", "h", "bloom", "b")
    assert engine._view_scene("replay-instant_replay-1")[1].id == "b"
    engine.set_view("replay-instant_replay-1", None)
    assert engine._view_scene("replay-instant_replay-1") == (None, None)
    assert engine._on_air_view("replay-instant_replay-1", engine._collection.canvases[0], {"h": object()}) is None

    class Racing(dict):  # the name is listed, then gone when looked up
        def __iter__(self):
            return iter(["gone"])

    engine.views = Racing()
    assert all(scene.id != "view:gone" for scene in engine._drawn_scenes(engine._collection))
