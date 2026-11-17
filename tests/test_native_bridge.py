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


def test_a_transitions_outgoing_bloom_is_in_the_snapshot():
    col = _collection()
    old = Scene(id="old", name="Old", canvas="h", items=[SceneItem(source="cam")])
    draws, parts, shows = native_bridge.snapshot_draws(col, _plain_state, {"h": old})
    assert "from:h" in [d.key for d in draws]
    assert parts[("cam", "from:h")] == "part:p-cam"
    assert shows["h"] == "canvas:h"


def test_which_transitions_the_native_engine_draws():
    from sprigcast.core.scene import Transition
    from sprigcast.render.engine import Running

    def running(kind, began=1.0, stinger=None):
        return Running(None, began, 0.3, Transition(kind=kind), stinger=stinger)

    for kind in ("fade", "slide", "swipe", "wipe", "leaf", "bloom"):
        assert native_bridge.native_transition(running(kind))
    assert not native_bridge.native_transition(running("move"))
    assert not native_bridge.native_transition(running("stinger", stinger=object()))
    assert not native_bridge.native_transition(running("fade", began=None))  # waiting for a camera
    spec = sprigcore.transition_spec(Transition(kind="wipe", direction="up", softness=2.0, duration_ms=500), "w")
    assert (spec.mode, round(spec.seconds, 3), tuple(spec.direction), spec.softness) == (4, 0.5, (0.0, -1.0), 1.0)
    assert spec.wipe_map == sprigcore.key("w")
    assert sprigcore.transition_spec(Transition(kind="cut")) is None


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

    def render(self, canvas, scene, sizes, **extra):
        self.rendered.append(canvas.id)

    def read(self, key):
        return b"x"


class FakeNative:
    """Stands in for NativeCanvases: draws every canvas it is shown, and its pictures are always ready."""

    def __init__(self):
        self.failed = False
        self.uploads, self.watching, self.synced = [], {}, 0
        self.outputs, self.python_served, self.clock = {}, [], 0.0

    def upload(self, source_id, frame, order, size):
        self.uploads.append(source_id)

    def forget_gone(self, ids):
        pass

    def sync(self, collection, effects, state_view, fading, transitions=None, wipe_picture=None):
        self.synced += 1
        self.transitions = dict(transitions or {})

    def handles(self, extra):
        return native_bridge.NativeCanvases.handles(self, extra)

    def check(self, now):
        pass

    def draws(self, canvas_id):
        return not self.failed

    def watch(self, canvas_id, size, fps):
        self.watching[canvas_id] = size

    def take(self, canvas_id):
        size = self.watching.get(canvas_id)
        return (b"n" * (size[0] * size[1] * 4), *size) if size else None

    can_share = True

    def output_key(self, size, on_card):
        return native_bridge.NativeCanvases.output_key(self, size, on_card)

    def serves(self, canvas_id, keys):
        return native_bridge.NativeCanvases.serves(self, canvas_id, keys)

    def set_outputs(self, canvas_id, keys, scale="bilinear"):
        self.outputs[canvas_id] = (sorted(set(keys)), scale)

    def take_outputs(self, canvas_id, key):
        self.clock += 1.0  # one frame a tick, each drawn later than the last
        size, on_card = key
        if on_card:
            return [native_bridge.CardFrame(canvas_id, size, self.clock, native_bridge.CardPool(1), 0xABC, 0,
                                            lambda *a: None)]
        return [(b"o" * (size[0] * size[1] * 3 // 2), self.clock)]

    def served(self, canvas_id, size, drawn):
        self.python_served.append((canvas_id, size))

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


def test_a_full_size_rgba_output_is_still_drawn_by_python_while_the_preview_is_native():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    got = []
    engine.add_frame_tap("h", got.append)
    _run(engine, fake)
    assert len(got) == 60 and fake.rendered.count("h") == 60
    assert engine.frames["h"].take().data[:1] == b"n"  # the preview is the native engine's
    assert engine._native.outputs["h"] == ([], "bicubic")


def test_recordings_and_streams_take_the_native_engines_frames():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    big, small = [], []
    engine.add_frame_tap("h", big.append, nv12_size=(64, 36))
    engine.add_frame_tap("h", small.append, nv12_size=(32, 18))
    _run(engine, fake)
    assert fake.rendered == [] and engine.readbacks == 0  # Python draws and copies nothing
    assert len(big) == 60 and len(small) == 60
    assert (big[0].fmt, big[0].width, big[0].height, len(big[0].data)) == ("nv12", 64, 36, 64 * 36 * 3 // 2)
    assert [f.time for f in big] == sorted(f.time for f in big) and big[0].time < big[-1].time
    assert engine._native.outputs["h"] == ([((32, 18), False), ((64, 36), False)], "bicubic")
    assert engine._native.outputs["v"] == ([], "bicubic")


class CardRecorder:
    """Stands in for a Recorder whose NVENC takes frames on the card."""

    def __init__(self):
        self.frames = []

    def wants_gpu_frames(self):
        return True

    def push_frame(self, frame):
        self.frames.append(frame)


def test_an_encoder_on_the_card_gets_the_native_engines_textures():
    engine, fake = _engine(), FakeCompositor()
    card, memory = CardRecorder(), []
    engine.add_frame_tap("h", card.push_frame, nv12_size=(64, 36))
    engine.add_frame_tap("h", memory.append, nv12_size=(64, 36))  # the same size through memory: its own output
    _run(engine, fake, seconds=0.25)
    assert fake.rendered == [] and engine.readbacks == 0
    assert card.frames and all(f.fmt == "gpu" and f.data is None and f.texture == 0xABC for f in card.frames)
    assert (card.frames[0].width, card.frames[0].height, card.frames[0].key) == (64, 36, ("native", 1, 0))
    assert memory and all(f.fmt == "nv12" for f in memory)
    assert engine._native.outputs["h"] == ([((64, 36), False), ((64, 36), True)], "bicubic")
    engine._native.can_share = False  # the card can't: the same recorder gets bytes from then on
    card.frames.clear()
    _run(engine, fake, seconds=0.1)
    assert card.frames and all(f.fmt == "nv12" for f in card.frames)


def test_outputs_go_back_to_python_while_it_draws_the_canvas():
    engine, fake = _engine(), FakeCompositor()
    got = []
    engine.add_frame_tap("h", got.append, nv12_size=(64, 36))
    _switch(engine, "move")  # a transition only Python draws
    engine._transitions["h"].began = 10.0
    fake.read_nv12_later = lambda canvas_id, size, tag, now: (b"p", now)
    fake.read_nv12 = lambda canvas_id, size: b"p"
    _run(engine, fake, seconds=0.25)
    assert got and all(f.data == b"p" for f in got)
    assert engine._native.outputs["h"] == ([], "bicubic") and ("h", (64, 36)) in engine._native.python_served


def test_more_output_sizes_than_the_native_engine_has_room_for_stay_with_python():
    keys = [((2 * n, 2 * n), False) for n in range(1, sprigcore.ENGINE_OUTPUTS + 2)]
    fake = FakeNative()
    assert not fake.serves("h", keys) and fake.serves("h", keys[:-1])
    assert not fake.serves("h", [None]) and not fake.serves("h", [])
    assert fake.output_key(None, True) is None and fake.output_key((4, 4), True) == ((4, 4), True)
    fake.can_share = False
    assert fake.output_key((4, 4), True) == ((4, 4), False)


def test_a_failed_native_engine_hands_the_previews_back_to_python():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    engine._native.failed = True
    _run(engine, fake)
    assert 29 <= fake.rendered.count("h") <= 31
    assert engine.frames["h"].take().data == b"x"


def _switch(engine, kind):
    from sprigcast.core.scene import Transition

    new = engine._collection.model_copy(deep=True)
    new.scenes.append(Scene(id="c", name="C", canvas="h"))
    new.active_scenes["h"] = "c"
    new.transition = Transition(kind=kind, duration_ms=500)
    old, engine._collection = engine._collection, new
    engine._start_transitions(old, new)


def test_a_fade_stays_in_the_native_engine():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    _switch(engine, "fade")
    engine._transitions["h"].began = 10.0
    _run(engine, fake, seconds=0.25)
    assert fake.rendered == []  # Python draws nothing: the native engine runs the fade
    assert "h" in engine._native.transitions


def test_a_move_transition_is_drawn_by_python():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    _switch(engine, "move")
    engine._transitions["h"].began = 10.0
    _run(engine, fake, seconds=0.25)
    assert fake.rendered.count("h") >= 7
    assert engine.frames["h"].take().data == b"x"


def test_preview_pictures_are_the_size_shown():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    engine.preview_sizes = {"h": (32, 18)}
    _run(engine, fake, seconds=0.1)
    assert engine._native.watching["h"] == (32, 18)
    assert engine.frames["h"].take().width == 32


class FakeEngine:
    """Stands in for sprigcore.Engine: remembers the outputs it was set and hands back queued frames."""

    def __init__(self, **_):
        self.set, self.queue, self.back = [], [], []

    def set_output(self, index, slot, width, height, scale="bilinear", share=False):
        self.set.append((index, slot, width, height) + (("card",) if share else ()))

    def take_output(self, index, slot, width, height):
        return self.queue.pop(0) if self.queue else None

    def take_texture(self, index, slot):
        return self.queue.pop(0) if self.queue else (0, None, 0)

    def give_back(self, index, slot, pool, entry):
        self.back.append((index, slot, pool, entry))

    def device(self):
        return 0x1234, 0

    def close(self):
        pass


def test_an_output_keeps_its_slot_and_its_frames_never_go_backwards(monkeypatch):
    monkeypatch.setattr(sprigcore, "Engine", FakeEngine)
    bridge = native_bridge.NativeCanvases(_collection())
    bridge._slots["h"] = 0
    big, small, tiny = ((64, 36), False), ((32, 18), False), ((16, 8), False)
    bridge.set_outputs("h", [big, small, big], "area")
    assert bridge.engine.set == [(0, 0, 32, 18), (0, 1, 64, 36)]
    bridge.set_outputs("h", [big, tiny], "area")  # one gone, one new: the kept one isn't touched
    assert bridge.engine.set[2:] == [(0, 0, 0, 0), (0, 0, 16, 8)]
    bridge.engine.queue = [(b"a", 5.0, 0), (b"b", 6.0, 0), (b"c", 7.0, 3)]
    bridge.served("h", (64, 36), 5.5)  # Python handed on a frame drawn at 5.5: the one from 5.0 is let go
    assert bridge.take_outputs("h", big) == [(b"b", 6.0), (b"c", 7.0)]
    assert bridge.output_drops == 3 and bridge.take_outputs("h", big) == []
    assert bridge.take_outputs("h", ((2, 2), False)) == []  # not an output it makes
    bridge.set_outputs("h", [big], "lanczos")  # scaled another way: every output starts over
    assert bridge.engine.set[-3:] == [(0, 1, 0, 0), (0, 0, 0, 0), (0, 0, 64, 36)]
    bridge.set_outputs("h", ())
    assert bridge.engine.set[-1] == (0, 0, 0, 0) and not bridge.failed


def test_frames_on_the_card_go_back_to_the_engine_when_let_go(monkeypatch):
    monkeypatch.setattr(sprigcore, "Engine", FakeEngine)
    released = []
    monkeypatch.setattr(sprigcore, "com_release", released.append)
    bridge = native_bridge.NativeCanvases(_collection())
    bridge._slots["h"] = 0
    card = bridge.output_key((64, 36), True)
    assert bridge.encoder_device() == 0x1234
    bridge.set_outputs("h", [card])
    assert bridge.engine.set == [(0, 0, 64, 36, "card")]
    bridge.engine.queue = [(1, (0xA, 7, 2, 5.0), 0), (1, (0xB, 7, 3, 6.0), 1)]
    first, second = bridge.take_outputs("h", card)
    assert (first.fmt, first.texture, first.key, first.time) == ("gpu", 0xA, ("native", 7, 2), 5.0)
    assert first.pool is second.pool and bridge.output_drops == 1  # one pool: an encoder registers each texture once
    del first
    assert bridge.engine.back == [(0, 0, 7, 2)] and released == [0xA]
    bridge.engine.queue = [(-1, None, 0)]  # the card can't draw NV12 textures: outputs go through memory
    assert bridge.take_outputs("h", card) == [] and not bridge.can_share and not bridge.failed
    assert bridge.output_key((64, 36), True) == ((64, 36), False) and bridge.encoder_device() is None
    del second
    assert released == [0xA, 0xB]


def test_an_encoder_opens_on_the_native_engines_device_while_it_draws(monkeypatch):
    from sprigcast.render import gpu_share

    monkeypatch.setattr(sprigcore, "Engine", FakeEngine)
    bridge = native_bridge.NativeCanvases(_collection())
    assert gpu_share.native_device() is None
    gpu_share.set_native(bridge)
    try:
        assert gpu_share.native_device().value == 0x1234
        gpu_share.native_give_up("a check")
        assert gpu_share.native_device() is None and not bridge.can_share
    finally:
        gpu_share.set_native(None)


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
        bridge.set_outputs("h", [((32, 18), False)])
        frames, deadline = [], time.monotonic() + 3
        while not frames and time.monotonic() < deadline:
            frames = bridge.take_outputs("h", ((32, 18), False))
            time.sleep(0.01)
        assert frames and not bridge.failed
        data, drawn = frames[0]
        assert abs(time.perf_counter() - drawn) < 3  # timed by the clock recorders use
        y = np.frombuffer(bytes(data), np.uint8)[:32 * 18].reshape(18, 32)
        luma = 16 + 219 * (pic[..., :3].astype(float) @ [0.2126, 0.7152, 0.0722]) / 255
        assert np.abs(y[2:10, 4:12] - luma).max() <= 1.5 and (y[0] == 16).all()
        # On the card: textures come out (where the device can draw NV12) and go back when let go.
        card = bridge.output_key((32, 18), True)
        bridge.set_outputs("h", [card])
        frames, deadline = [], time.monotonic() + 3
        while not frames and bridge.can_share and time.monotonic() < deadline:
            frames = bridge.take_outputs("h", card)
            time.sleep(0.01)
        assert not bridge.failed and (frames or not bridge.can_share)
        if frames:
            assert frames[0].fmt == "gpu" and frames[0].texture and frames[0].key[0] == "native"
            device = bridge.encoder_device()
            assert device
            sprigcore.com_release(device)
        del frames
    finally:
        bridge.close()
