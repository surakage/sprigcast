"""SPRIGCAST_ENGINE=native: the native engine drawing the previews (render/native_bridge.py) and how the render
thread hands over to it and takes back from it."""

import sys
from types import SimpleNamespace

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


def test_which_transitions_the_native_engine_draws(monkeypatch):
    from sprigcast.core.scene import Transition
    from sprigcast.render.engine import Running

    def running(kind, began=1.0, stinger=None):
        return Running(None, began, 0.3, Transition(kind=kind), stinger=stinger)

    for kind in ("fade", "slide", "swipe", "wipe", "leaf", "bloom", "move"):
        assert native_bridge.native_transition(running(kind))
    monkeypatch.setenv("SPRIGCAST_NO_NATIVE_MOVE", "1")
    assert not native_bridge.native_transition(running("move"))  # left to Python, as before
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
        self.attached = {}
        self.lost = 0

    def upload(self, source_id, frame, order, size):
        self.uploads.append(source_id)

    def attach_media(self, source_id, player, held=False):
        if player is not None:
            self.attached[source_id] = player
        else:
            self.attached.pop(source_id, None)
        return player is not None

    def forget_gone(self, ids):
        pass

    def sync(self, collection, effects, state_view, fading, transitions=None, wipe_picture=None, views=None,
             held=None, move_pieces=None):
        self.synced += 1
        self.move_pieces = move_pieces
        self.transitions = dict(transitions or {})
        self.views = dict(views or {})
        self.held = dict(held or {})

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

    def pause_outputs(self, canvas_id, keys, scale="bilinear"):
        self.set_outputs(canvas_id, keys, scale)
        self.paused = getattr(self, "paused", 0) + 1

    def take_outputs(self, canvas_id, key):
        self.clock += 1.0  # one frame a tick, each drawn later than the last
        size, on_card = key
        if on_card:
            return [native_bridge.CardFrame(canvas_id, size, self.clock, native_bridge.CardPool(1), 0xABC, 0,
                                            lambda *a: None)]
        return [(b"o" * (size[0] * size[1] * 3 // 2), self.clock)]

    def served(self, canvas_id, size, drawn):
        self.python_served.append((canvas_id, size))

    resets = 0

    stuck = False

    def reset_outputs(self):
        if self.stuck:
            return False
        self.resets += 1
        self.outputs = {}
        return True

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


def test_a_switch_waiting_for_a_camera_keeps_the_engines_outputs(monkeypatch):
    """While a switch waits for a camera the engine draws the bloom held and its outputs go on. Dropped and made
    again each switch, every switch made new texture pools, and the writer on the card keeps each texture it was
    handed: graphics memory grew ~10 MB a minute with a switch every 15 s (PC soak 2026-11-18)."""
    engine, fake = _engine(), FakeCompositor()
    engine.add_frame_tap("h", lambda f: None, nv12_size=(64, 36))
    _run(engine, fake, seconds=0.1)
    kept = engine._native.outputs["h"]
    assert kept[0]
    old = engine._collection.scenes[0]
    engine._waking["h"] = (old, 1e9, set())
    monkeypatch.setattr(engine, "_switch_waits", lambda compositor, canvas_id, now: True)
    seen = []
    for i in range(30):
        engine._tick(fake, 20.0 + i / 60)
        seen.append(engine._native.outputs["h"])
    assert all(o == kept for o in seen)  # never dropped while it waits
    assert engine._native.held == {"h": old}  # the engine draws the bloom held
    monkeypatch.setattr(engine, "_switch_waits", lambda compositor, canvas_id, now: False)
    _run(engine, fake, seconds=0.1)
    assert engine._native.outputs["h"] == kept and engine._native.held == {}


def test_the_bloom_held_is_drawn_in_place_of_the_one_on_air():
    col = SceneCollection(canvases=[Canvas(id="h", width=64, height=36)],
                          scenes=[Scene(id="a", name="A", canvas="h"), Scene(id="b", name="B", canvas="h")],
                          active_scenes={"h": "b"})
    draws, _parts, shows = native_bridge.snapshot_draws(col, _plain_state, held={"h": col.scenes[0]})
    assert [d.scene.id for d in draws] == ["a"] and shows["h"] == native_bridge.canvas_key("h")


def test_outputs_go_back_to_python_while_it_draws_the_canvas(monkeypatch):
    monkeypatch.setenv("SPRIGCAST_NO_NATIVE_MOVE", "1")
    engine, fake = _engine(), FakeCompositor()
    got = []
    engine.add_frame_tap("h", got.append, nv12_size=(64, 36))
    _switch(engine, "move")  # a transition only Python draws
    engine._transitions["h"].began = 10.0
    fake.read_nv12_later = lambda canvas_id, size, tag, now: (b"p", now)
    fake.read_nv12 = lambda canvas_id, size: b"p"
    _run(engine, fake, seconds=0.25)
    assert got and all(f.data == b"p" for f in got)
    # kept (not stopped and made again) while Python draws it: the engine's frames are let go meanwhile
    assert engine._native.outputs["h"] == ([((64, 36), False)], "bicubic") and engine._native.paused > 0
    assert ("h", (64, 36)) in engine._native.python_served


def test_more_output_sizes_than_the_native_engine_has_room_for_stay_with_python():
    keys = [((2 * n, 2 * n), False) for n in range(1, sprigcore.ENGINE_OUTPUTS + 2)]
    fake = FakeNative()
    assert not fake.serves("h", keys) and fake.serves("h", keys[:-1])
    assert not fake.serves("h", [None]) and not fake.serves("h", [])
    assert fake.output_key(None, True) is None and fake.output_key((4, 4), True) == ((4, 4), True)
    fake.can_share = False
    assert fake.output_key((4, 4), True) == ((4, 4), False)


def test_the_virtual_camera_showing_the_bloom_on_air_rides_on_its_canvas():
    engine, fake = _engine(), FakeCompositor()
    engine.set_view("camera", "h", "bloom", "a")
    got = []
    engine.add_frame_tap("view:camera", got.append, nv12_size=(32, 18))
    _run(engine, fake)
    assert fake.rendered == [] and engine.readbacks == 0  # Python draws and copies nothing
    assert len(got) == 60 and got[0].fmt == "nv12" and (got[0].width, got[0].height) == (32, 18)
    assert engine._native.outputs["h"] == ([((32, 18), False)], "bicubic")
    assert engine._native.views == {}  # no slot of its own: the same picture as the canvas


def test_instant_replay_keeping_a_camera_is_drawn_by_the_engine_in_a_slot_of_its_own():
    engine, fake = _engine(), FakeCompositor()
    engine._collection.sources.append(SourceConfig(id="cam", kind="color", name="Cam",
                                                   settings={"width": 8, "height": 8}))
    engine.set_view("replay", "h", "source", "cam")
    got = []
    engine.add_frame_tap("view:replay", got.append, nv12_size=(64, 36))
    _run(engine, fake)
    assert "view:replay" not in fake.rendered and engine.readbacks == 0
    canvas, scene = engine._native.views["replay"]
    assert canvas.id == "h" and [item.source for item in scene.items] == ["cam"]
    assert engine._native.outputs["view:replay"] == ([((64, 36), False)], "bicubic")
    assert len(got) == 60 and all(f.canvas_id == "view:replay" and f.fmt == "nv12" for f in got)
    engine.set_view("replay", None)  # replays off: the engine lets the view go
    engine.remove_frame_tap("view:replay", got.append)
    _run(engine, fake, seconds=0.1)
    assert engine._native.views == {}


def test_a_projector_watching_a_view_is_still_drawn_by_python():
    engine, fake = _engine(), FakeCompositor()
    engine.set_view("screen", "h", "bloom", "a")
    got = []
    engine.add_frame_tap("view:screen", got.append)  # full-size RGBA: a screen
    _run(engine, fake, seconds=0.25)
    assert got and fake.rendered and engine._native.views == {}


def test_frames_the_engine_let_go_of_count_as_the_recordings_drops():
    from sprigcast.output.recorder import Recorder

    engine, fake = _engine(), FakeCompositor()

    class Counting:
        def __init__(self):
            self.dropped, self.frames = 0, []

        def push_frame(self, frame):
            self.frames.append(frame)

        frames_lost = Recorder.frames_lost
        error, paused_since, _opening = None, None, False
        _stop = SimpleNamespace(is_set=lambda: False)

        def _counts_drops(self):
            return True

    rec = Counting()
    engine.add_frame_tap("h", rec.push_frame, nv12_size=(64, 36))
    engine._native.lost = 4  # every take, the engine let 4 go
    _run(engine, fake, seconds=0.1)
    assert rec.frames and rec.dropped == 4 * len(rec.frames)


def test_pictures_go_only_to_the_engine_while_python_draws_nothing():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    camera = engine._sources["cam"] = EngineVideo()
    camera.engine_player = None  # a camera: Python takes its pictures and hands them on
    engine._collection.scenes[0].items.append(SceneItem(source="cam"))
    _run(engine, fake, seconds=0.5)
    assert engine._native.uploads.count("cam") == 30
    assert fake.uploaded.count("cam") == 1  # the first tick only, before Python knew it draws nothing
    got = []
    engine.add_frame_tap("h", got.append)  # a full-size RGBA tap: Python draws again, with the newest picture
    engine._tick(fake, 20.0)
    assert fake.uploaded.count("cam") == 2 and fake.rendered == ["h"] and engine._held_back == {}
    engine._tick(fake, 20.1)
    assert fake.uploaded.count("cam") == 3  # while it draws, every picture goes to both


def test_a_held_back_picture_that_fails_to_copy_is_let_go_and_the_frame_goes_on():
    """Native simple run (2026-11-18): a held-back Local time picture failed to copy on every tick, the graphics
    restarted once a minute for 5 minutes, and the stream got 0 frames: the same picture was copied again each
    time. Now it's let go; the tick (and the outputs after it) go on, and a sleeping source's picture isn't kept."""
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    camera = engine._sources["cam"] = EngineVideo()
    camera.engine_player = None
    engine._collection.scenes[0].items.append(SceneItem(source="cam"))
    _run(engine, fake, seconds=0.2)
    assert "cam" in engine._held_back
    resting = engine._rest_sources
    engine._rest_sources = lambda compositor, now: None  # it stays asleep for this tick (its bloom went off air)
    camera.asleep = True
    engine._tick(fake, 20.0)
    assert "cam" not in engine._held_back  # asleep: nothing kept for it
    engine._rest_sources = resting
    camera.asleep = False
    _run(engine, fake, seconds=0.1)
    assert "cam" in engine._held_back

    failures = []

    def broken(source_id, *a):  # the held-back picture is broken; the camera's next ones are fine
        if source_id == "cam" and not failures:
            failures.append(source_id)
            raise OSError("exception: access violation reading 0x000002662D259040")

    fake.upload = broken
    got = []
    engine.add_frame_tap("h", got.append)  # Python draws again: the held-back picture is copied, and fails
    engine._tick(fake, 30.0)
    assert failures and engine._held_back == {} and got  # let go, and the frame still reached its tap
    engine._tick(fake, 30.1)
    assert len(got) == 2


def test_an_output_that_gets_no_frames_is_made_again():
    engine, fake = _engine(), FakeCompositor()
    got = []
    engine.add_frame_tap("h", got.append, nv12_size=(64, 36))
    _run(engine, fake, seconds=0.1)
    fake.prune = lambda idle_s=3.0: 2
    engine.refresh_outputs()  # Recorder.check_flow found no frames arriving
    engine._tick(fake, 20.0)
    assert engine._native.resets == 1 and engine._native.outputs["h"] == ([((64, 36), False)], "bicubic")
    engine._tick(fake, 20.1)
    assert engine._native.resets == 1  # once


def test_outputs_are_made_again_only_once_a_stuck_engine_frame_is_going_again():
    """Stuck-frame crash run (2026-11-17): outputs made again under a stuck frame froze the stream for good."""
    engine, fake = _engine(), FakeCompositor()
    engine.add_frame_tap("h", [].append, nv12_size=(64, 36))
    _run(engine, fake, seconds=0.1)
    pruned = []
    fake.prune = lambda idle_s=3.0: pruned.append(idle_s) or 0
    engine._native.stuck = True
    engine.refresh_outputs()
    engine._tick(fake, 20.0)
    engine._tick(fake, 20.1)
    assert engine._native.resets == 0 and 0.0 not in pruned  # waits, and keeps asking
    engine._native.stuck = False
    engine._tick(fake, 20.2)
    assert engine._native.resets == 1 and 0.0 in pruned
    engine._tick(fake, 20.3)
    assert engine._native.resets == 1


class EngineVideo:
    """Stands in for a MediaSource the engine's own player decodes."""

    kind, asleep, channel_order, size, sleeps = "media", False, "RGBA", (8, 8), True

    def __init__(self):
        self.engine_player, self.asked, self.ticked = object(), 0, 0

    def video_frame(self):
        self.asked += 1
        return np.zeros((8, 8, 4), np.uint8)

    def engine_tick(self):
        self.ticked += 1

    def __getattr__(self, name):  # whatever else the render thread asks a source
        if name.startswith("__"):
            raise AttributeError(name)
        return lambda *a, **k: None


def test_a_video_the_engine_plays_never_passes_through_python():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    video = engine._sources["clip"] = EngineVideo()
    engine._collection.scenes[0].items.append(SceneItem(source="clip"))
    _run(engine, fake, seconds=0.5)
    native = engine._native
    assert native.attached == {"clip": video.engine_player}
    assert video.asked <= 1 and video.ticked >= 29  # only the first tick, before Python knew it draws nothing
    assert "clip" not in native.uploads  # the engine takes them itself
    # Python's compositor draws again (a screenshot): it takes the picture too, the engine keeps taking its own.
    before = video.asked
    engine._python_drew = True
    engine._tick(fake, 20.0)
    assert video.asked == before + 1 and "clip" in fake.uploaded and "clip" not in native.uploads
    # Off screen it sleeps: the engine keeps the picture it has.
    engine._collection.scenes[0].items.clear()
    fake.forget = lambda source_id: None
    engine._tick(fake, 20.1)
    assert video.asleep and native.attached == {}
    engine._collection.scenes[0].items.append(SceneItem(source="clip"))
    # The native engine failed: every picture is Python's again.
    native.failed, before = True, video.asked
    engine._tick(fake, 20.2)
    assert not video.asleep and video.asked == before + 1


def test_the_bridge_tells_the_engine_once_which_player_a_source_shows(monkeypatch):
    calls = []

    class Attaching(FakeEngine):
        def attach_media(self, source_id, player):
            calls.append(("attach", source_id, player))

        def forget(self, source_id):
            calls.append(("forget", source_id))

    monkeypatch.setattr(sprigcore, "Engine", Attaching)
    bridge = native_bridge.NativeCanvases(_collection())
    player = object()
    assert bridge.attach_media("clip", player) and bridge.attach_media("clip", player)
    assert not bridge.attach_media("other", None)
    assert calls == [("attach", "clip", player)]
    assert not bridge.attach_media("clip", None)
    assert bridge.attach_media("clip", player)
    bridge.forget_gone([])
    assert calls[1:] == [("attach", "clip", None), ("attach", "clip", player), ("forget", "clip")]
    bridge.failed = True
    assert not bridge.attach_media("clip", player)


def test_a_browser_page_is_attached_by_its_shared_block_and_let_go_the_right_way(monkeypatch):
    calls = []

    class Attaching(FakeEngine):
        def attach_media(self, source_id, player):
            calls.append(("media", source_id, player))

        def attach_shared(self, source_id, name):
            calls.append(("shared", source_id, name))

    monkeypatch.setattr(sprigcore, "Engine", Attaching)
    bridge = native_bridge.NativeCanvases(_collection())
    assert bridge.attach_media("page", "sprigcast-1-1") and bridge.attach_media("page", "sprigcast-1-1")
    assert calls == [("shared", "page", "sprigcast-1-1")]  # an equal name is the same block: told once
    assert bridge.attach_media("page", "sprigcast-2-1")  # the helper restarted: its new block
    player = object()
    assert bridge.attach_media("page", player)  # from a block to a player: the block is let go first
    assert bridge.attach_media("page", "sprigcast-2-1")  # and back: the player is let go first
    assert not bridge.attach_media("page", None)
    assert calls[1:] == [("shared", "page", "sprigcast-2-1"), ("shared", "page", None), ("media", "page", player),
                         ("media", "page", None), ("shared", "page", "sprigcast-2-1"), ("shared", "page", None)]


def test_a_frozen_page_is_held_not_let_go(monkeypatch):
    """Snap's freeze (PC, 2026-11-18): letting go of a page's block left the page waiting for the engine, and it
    made its texture again every 3 s. Frozen, the block stays and is held; thawed, it shows again."""
    calls = []

    class Holding(FakeEngine):
        def attach_shared(self, source_id, name):
            calls.append(("shared", source_id, name))

        def hold_shared(self, source_id, held):
            calls.append(("hold", source_id, held))

        def forget(self, source_id):
            calls.append(("forget", source_id))

    monkeypatch.setattr(sprigcore, "Engine", Holding)
    bridge = native_bridge.NativeCanvases(_collection())
    assert bridge.attach_media("page", "sprigcast-1-1")
    assert bridge.attach_media("page", "sprigcast-1-1", held=True) and bridge.attach_media("page", "sprigcast-1-1",
                                                                                           held=True)
    assert bridge.attach_media("page", "sprigcast-1-1")
    assert calls == [("shared", "page", "sprigcast-1-1"), ("hold", "page", True), ("hold", "page", False)]
    calls.clear()
    assert bridge.attach_media("page", "sprigcast-1-1", held=True)
    bridge.forget_gone([])
    assert "page" not in bridge._held
    calls.clear()
    assert not bridge.attach_media("clip", None, held=True)  # nothing to hold: a player is simply let go
    assert calls == []


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


def test_a_move_transition_is_drawn_by_the_native_engine():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    _switch(engine, "move")
    engine._transitions["h"].began = 10.0
    _run(engine, fake, seconds=0.25)
    assert fake.rendered == []  # Python draws nothing: the engine works out every frame itself
    assert "h" in engine._native.transitions
    canvas = engine._collection.canvases[0]
    old, new = engine._native.move_pieces(engine._native.transitions["h"], canvas)
    assert isinstance(old, list) and isinstance(new, list)


def test_a_move_transition_is_drawn_by_python_when_asked(monkeypatch):
    monkeypatch.setenv("SPRIGCAST_NO_NATIVE_MOVE", "1")
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    _switch(engine, "move")
    engine._transitions["h"].began = 10.0
    _run(engine, fake, seconds=0.25)
    assert fake.rendered.count("h") >= 7
    assert engine.frames["h"].take().data == b"x"


def test_a_move_hands_the_engine_its_pieces_once(monkeypatch):
    """The bridge lays a Move out once, when it starts: every piece with its sprig's drawing or its filters, and
    which old piece each new one moves from (render/move.py pair)."""
    from sprigcast.core.filters import VideoEffects
    from sprigcast.core.scene import Transition
    from sprigcast.render import move
    from sprigcast.render.engine import Running

    calls = []

    class Moving(FakeEngine):
        def move(self, index, previous, seconds, spec):
            calls.append((index, previous, seconds, spec))

        def transition(self, index, previous, spec):
            calls.append((index, previous, spec))

        def __getattr__(self, name):  # the rest of the engine: nothing to check here
            return lambda *a, **k: None

    monkeypatch.setattr(sprigcore, "Engine", Moving)
    monkeypatch.setattr(native_bridge.native_snapshot, "hand_over", lambda *a, **k: None)
    col = _collection()
    bridge = native_bridge.NativeCanvases(col)
    quad = [(0, 0, 0, 0), (8, 0, 1, 0), (0, 8, 0, 1), (8, 8, 1, 1)]
    old = [move.Piece("cam", "cam frame", "cam", "h", quad), move.Piece("red", "red", "red", "h", quad, 0.5)]
    new = [move.Piece("red", "red", "red", "h", quad)]
    running = Running(Scene(id="old", name="Old", canvas="h"), 1.0, 0.4,
                      Transition(kind="move", easing="spring", unmatched="slide"))
    asked = []

    def pieces(r, canvas):
        asked.append((r, canvas.id))
        return old, new

    red = VideoEffects(brightness=0.25)
    for _ in range(3):
        bridge.sync(col, {"red": red}, _plain_state, False, {"h": running}, move_pieces=pieces)
    assert asked == [(running, "h")]  # laid out once
    moves = [c for c in calls if len(c) == 4]
    assert len(moves) == 1
    _index, previous, seconds, spec = moves[0]
    assert previous == "from:h" and seconds == 0.4
    assert (spec.old_count, spec.new_count, spec.effects_count) == (2, 1, 1)
    assert sprigcore.MOVE_EASINGS[spec.easing] == "spring" and sprigcore.MOVE_UNMATCHED[spec.unmatched] == "slide"
    cam, red_old, red_new = spec.pieces[0], spec.pieces[1], spec.pieces[2]
    assert cam.part == sprigcore.key("part:p-cam") and cam.effects == -1  # a sprig drawn whole
    assert red_old.part == 0 and red_old.source == sprigcore.key("red") and abs(red_old.opacity - 0.5) < 1e-6
    assert red_old.effects == red_new.effects == 0 and abs(spec.effects[0].brightness - 0.25) < 1e-6
    assert red_new.paired == 1 and cam.paired == -1
    assert [tuple(red_new.quad[i]) for i in range(4)] == [tuple(map(float, q)) for q in quad]
    bridge.sync(col, {"red": red}, _plain_state, False, {}, move_pieces=pieces)
    assert calls[-1][1:] == (None, None)  # over: stopped


def test_preview_pictures_are_the_size_shown():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    engine.preview_sizes = {"h": (32, 18)}
    _run(engine, fake, seconds=0.1)
    assert engine._native.watching["h"] == (32, 18)
    assert engine.frames["h"].take().width == 32


class FakeEngine:
    """Stands in for sprigcore.Engine: remembers the outputs it was set and hands back queued frames."""

    hung = 0

    def __init__(self, **_):
        self.set, self.queue, self.back = [], [], []

    def stats(self):
        return {"rebuilds": 0, "faulted": 0, "hung": self.hung}

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
    assert bridge.output_drops == 3 and bridge.lost == 3 and bridge.take_outputs("h", big) == []
    assert bridge.lost == 0
    assert bridge.take_outputs("h", ((2, 2), False)) == []  # not an output it makes
    bridge.set_outputs("h", [big], "lanczos")  # scaled another way: every output starts over
    assert bridge.engine.set[-3:] == [(0, 1, 0, 0), (0, 0, 0, 0), (0, 0, 64, 36)]
    bridge.set_outputs("h", ())
    assert bridge.engine.set[-1] == (0, 0, 0, 0) and not bridge.failed
    bridge.set_outputs("h", [big])
    bridge.served("h", (64, 36), 9.0)
    bridge.engine.hung, before = 1, len(bridge.engine.set)
    assert bridge.reset_outputs() is False and len(bridge.engine.set) == before  # never under a stuck frame
    bridge.engine.hung = 0
    assert bridge.reset_outputs()  # a feed that stopped: every output starts over, nothing remembered
    assert bridge.engine.set[-1] == (0, 0, 0, 0) and bridge._outputs[0] == {} and bridge._last_out == {}


def test_a_python_drawn_transition_keeps_the_outputs_and_their_textures(monkeypatch):
    """A Move or stinger transition: the recording is unlinked and its frames come from Python meanwhile, but the
    engine's output stays (its texture pool too) and its frames are let go. Dropped and made again each time,
    each transition made a new pool the writer on the card kept (PC soak 2026-11-18)."""
    monkeypatch.setattr(sprigcore, "Engine", FakeEngine)
    monkeypatch.setattr(sprigcore, "com_release", lambda pointer: None)
    bridge = native_bridge.NativeCanvases(_collection())
    bridge._slots["h"] = 0
    card = bridge.output_key((64, 36), True)
    bridge.set_outputs("h", [card])
    unlinked = []
    owner = SimpleNamespace(unlinked=unlinked.append)
    bridge.engine.unlink_output = lambda slot, out: (41, True)
    bridge._links[(0, 0)] = (owner, object(), ("settings",))
    bridge.engine.queue = [(1, (0xA, 7, 2, 5.0), 0), (1, (0xB, 7, 3, 6.0), 0)]
    bridge.pause_outputs("h", [card])
    assert bridge.engine.set == [(0, 0, 64, 36, "card")]  # never stopped and made again: the same textures
    assert unlinked == [41] and (0, 0) not in bridge._links  # the recording takes Python's frames meanwhile
    assert bridge._paused[(0, 0)][0] is owner  # linked again afterwards without saying so again in the log
    assert sorted(bridge.engine.back) == [(0, 0, 7, 2), (0, 0, 7, 3)] and bridge.lost == 0  # let go
    bridge.set_outputs("h", [card])  # the engine draws it again: nothing changes
    assert bridge.engine.set == [(0, 0, 64, 36, "card")]


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


def test_deliberate_faults_for_the_pc_crash_test(monkeypatch):
    plan = native_bridge.planned_faults
    assert plan(None) == [] and plan("") == [] and plan("nonsense, 9@5, 1@-2, 2@x") == []
    assert plan("3@60") == [(60.0, 3)]
    assert plan("1@30x3, 4@10") == [(10.0, 4), (30.0, 1), (31.0, 1), (32.0, 1)]

    class Faulting(FakeEngine):
        def __init__(self, **_):
            super().__init__()
            self.faults = []

        def debug_fault(self, kind):
            self.faults.append(kind)

        def stats(self):
            return {"rebuilds": 0, "faulted": 0, "hung": 0}

    monkeypatch.setattr(sprigcore, "Engine", Faulting)
    monkeypatch.setenv("SPRIGCAST_ENGINE_FAULT", "3@5x2")
    bridge = native_bridge.NativeCanvases(_collection())
    for second in range(100, 110):
        bridge.check(float(second))
    assert bridge.engine.faults == [3, 3] and not bridge.failed  # at 5 s and 6 s after the first check
    monkeypatch.delenv("SPRIGCAST_ENGINE_FAULT")
    quiet = native_bridge.NativeCanvases(_collection())
    quiet.check(100.0)
    quiet.check(200.0)
    assert quiet.engine.faults == []


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


def test_views_take_the_free_canvas_slots_and_give_them_back(monkeypatch):
    class Slots(FakeEngine):
        def __init__(self, **_):
            super().__init__()
            self.canvases, self.shows = {}, {}

        def set_canvas(self, slot, width, height):
            self.canvases[slot] = (width, height)

        def show(self, slot, key):
            self.shows[slot] = key

        def transition(self, *a):
            pass

    monkeypatch.setattr(sprigcore, "Engine", Slots)
    monkeypatch.setattr(native_bridge.native_snapshot, "hand_over", lambda *a: None)
    col = _collection()
    bridge = native_bridge.NativeCanvases(col)
    h = col.canvases[0]
    camera = Scene(id="view:replay", name="replay", canvas="h", items=[SceneItem(source="red")])
    bridge.sync(col, {}, _plain_state, False, views={"replay": (h, camera)})
    assert bridge._slots == {"h": 0, "v": 1, "view:replay": 2}  # the canvases first, whatever comes in
    assert bridge.engine.canvases[2] == (64, 36) and bridge.engine.shows[2] == "view:replay"
    assert bridge.draws("view:replay")
    bridge.set_outputs("view:replay", [((64, 36), False)])
    bridge.sync(col, {}, _plain_state, False, views={})  # gone: its output stops and its slot is free
    assert "view:replay" not in bridge._slots and bridge.engine.shows[2] is None
    assert bridge.engine.canvases[2] == (0, 0) and bridge.engine.set[-1] == (2, 0, 0, 0)
    assert not bridge.draws("view:replay") and 2 not in bridge._outputs
    many = {f"v{n}": (h, camera) for n in range(sprigcore.ENGINE_CANVASES)}
    bridge.sync(col, {}, _plain_state, False, views=many)  # more than there are slots: the rest stay with Python
    assert sum(key.startswith("view:") for key in bridge._slots) == sprigcore.ENGINE_CANVASES - 2


def test_a_screenshot_of_a_canvas_the_engine_draws_is_read_from_the_engine():
    """PC heavy run (2026-11-17): each Control API screenshot woke Python's compositor to draw the whole canvas
    (573-602 ms held, then the graphics restarted). Now it's read from the engine; Python draws only what the
    engine doesn't."""
    from types import SimpleNamespace

    from PySide6.QtGui import QImage

    from sprigcast.app import screenshot

    engine = _engine()
    native = engine._native
    native._slots = {"h": 0}
    native.draws = lambda canvas_id: canvas_id == "h"
    reads = []

    def read_bgra(slot):
        reads.append(slot)
        return bytes([10, 20, 30, 255]) * (64 * 36)

    native.engine = SimpleNamespace(_sizes={0: (64, 36)}, read_bgra=read_bgra)
    assert engine.native_shot("h")[1:] == (64, 36) and engine.native_shot("v") is None
    taps = []
    engine.add_frame_tap = lambda key, tap: taps.append(key)
    engine.remove_frame_tap = lambda key, tap: None
    images = screenshot.grab(engine, ["h"], timeout=0.1)
    assert taps == [] and reads == [0, 0]  # never asked Python to draw it
    image = images["h"]
    assert (image.width(), image.height()) == (64, 36)
    assert image.pixelColor(3, 3).getRgb() == (30, 20, 10, 255)  # BGRA in, the right colours out
    assert isinstance(image, QImage)
    screenshot.grab(engine, ["h", "v"], timeout=0.05)
    assert taps == ["shot:v"]  # the one the engine doesn't draw comes from Python, as before
    engine._sources["snap"] = SimpleNamespace(in_screenshots=False)
    # PC run 2026-11-18: a Snap source anywhere sent every screenshot to Python's compositor. Only one on air in
    # the canvas does now (Python draws the canvas without it).
    assert engine.native_shot("h") is not None
    engine._on_air_sources = lambda collection, canvas_id=None: {"snap"} if canvas_id == "h" else set()
    assert engine.native_shot("h") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own thread list (runs in CI with the engine checks)")
def test_thread_times_come_without_psutil_on_windows():
    """CPU by thread: psutil held Python's lock 434 ms on the PC (2026-11-17); these come through ctypes."""
    import threading

    from sprigcast.tools.cpuwhere import CpuWatch, _windows_thread_times

    times = _windows_thread_times()
    assert threading.get_native_id() in times and all(t >= 0 for t in times.values())
    whole, threads = CpuWatch().busiest()
    assert whole >= 0 and isinstance(threads, list)


def test_a_frame_stuck_for_good_hands_drawing_back_to_python(monkeypatch):
    """A frame that never ends (a call into Windows that never came back, PC test 2026-11-18): after the engine's
    own 2 s and 3 s more, Python draws everything again rather than showing nothing."""

    class Stuck(FakeEngine):
        ticks = 100

        def stats(self):
            return {"rebuilds": 0, "faulted": 0, "hung": self.hung, "ticks": self.ticks}

    monkeypatch.setattr(sprigcore, "Engine", Stuck)
    bridge = native_bridge.NativeCanvases(_collection())
    bridge.check(10.0)  # running
    bridge.engine.hung = 1
    bridge.engine.ticks = 101  # a slow frame that then finished: never given up on
    for second in range(11, 16):
        bridge.engine.ticks += 1
        bridge.check(float(second))
    assert not bridge.failed
    for second in range(16, 19):  # no frame finishes any more
        bridge.check(float(second))
    assert not bridge.failed
    bridge.check(19.0)
    assert bridge.failed
