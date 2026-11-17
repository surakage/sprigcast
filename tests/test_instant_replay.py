"""Instant replay (store plugin): what it records, the wipe and badge, playing a saved replay, and the
whole trip from frames on a tap to a replay on screen and back, with the events apps hear."""

import time

import av
import numpy as np
import pytest

from sprigcast import plugin_store
from sprigcast.core.scene import SourceConfig
from sprigcast.render.engine import Frame

pytest.importorskip("PySide6.QtWidgets")

from test_control_api import _collection  # noqa: E402
from test_store_effects import Host  # noqa: E402

needs_x264 = pytest.mark.skipif("libx264" not in av.codecs_available, reason="needs libx264 in PyAV")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def ir(tmp_path, app):
    plugins = tmp_path / "plugins"
    store = next(p for p in plugin_store.catalog() if p.id == "instant_replay")
    assert plugin_store.install(plugins, store) is None
    import sys

    yield sys.modules["sprigcast_plugin_instant_replay"]
    plugin_store.remove(plugins, "instant_replay")


def test_it_is_in_the_store_with_its_actions():
    store = {p.id: p for p in plugin_store.catalog()}
    assert store["instant_replay"].name == "Instant replay"
    from sprigcast.control.native import _store_action_plugins

    found = _store_action_plugins()
    assert found["replay.play"] == "Instant replay" and found["replay.stop"] == "Instant replay"


# What it records -----------------------------------------------------------------------------------


def test_choices_list_placed_sprigs_then_picture_sources_a_to_z(ir):
    col = _collection()
    options = ir.capture_options(col)
    assert options[0] == ("sprig:p-frame", "Frame (sprig)")
    names = [label for _, label in options[1:]]
    assert names == ["Alert", "Cam"]  # no sound sources, no sprig placements


def test_sprigs_holding_a_replay_are_left_out(ir):
    col = _collection()
    col.sources.append(SourceConfig(id="rp", kind=ir.KIND, name="Instant replay"))
    col.scene("p-frame").items.append(col.scene("chat-h").items[0].model_copy(update={"source": "rp"}))
    assert all(not v.startswith("sprig:") for v, _ in ir.capture_options(col))


def test_a_sprig_is_recorded_through_one_of_its_placements_on_a_landscape_canvas(ir):
    col = _collection()
    assert ir.resolve(col, "sprig:p-frame") == ("frame-h", "horizontal")
    assert ir.resolve(col, "source:cam") == ("cam", "horizontal")
    assert ir.resolve(col, "sprig:gone") is None and ir.resolve(col, "") is None


def test_the_view_fills_the_canvas_and_puts_the_corner_pick_in_its_corner(ir):
    col = _collection()
    canvas, scene = ir.view_scene(col, {"capture": "sprig:p-frame", "corner": "source:cam",
                                        "corner_at": "top_left", "corner_size": 25}, "replay-x")
    assert canvas.id == "horizontal" and scene.canvas == "horizontal"
    main, corner = scene.items
    assert main.source == "frame-h" and main.bounds == "fit" and main.bounds_size == (64, 36)
    assert corner.source == "cam" and corner.bounds_size == (16, 9) and corner.bounds_align == "top_left"
    margin = round(36 * 0.035)
    assert (corner.x, corner.y) == (margin, margin)
    with pytest.raises(ValueError, match="pick what to replay"):
        ir.view_scene(col, {"capture": ""}, "replay-x")


def test_the_engine_draws_a_plugins_scene_view(ir):
    from sprigcast.render.engine import Engine

    col = _collection()
    engine = Engine(col)
    canvas, scene = ir.view_scene(col, {"capture": "source:cam"}, "replay-x")
    engine.set_view("replay-x", canvas.id, "scene", scene)
    assert engine._view_scene("replay-x") == (col.canvas("horizontal"), scene)
    assert scene in engine._drawn_scenes(engine._collection)  # what it records stays awake
    engine.set_view("replay-x", None)
    assert "replay-x" not in engine.views


# The look ------------------------------------------------------------------------------------------


def test_the_wipe_covers_the_whole_picture_in_the_middle_and_nothing_at_the_ends(ir):
    size = (320, 180)
    for k in (0.0, 1.0):
        assert ir.wipe_frame(None, size, k, False)[..., 3].max() == 0
    for leaving in (False, True):
        covered = ir.wipe_frame(None, size, ir.COVER_AT, leaving)
        assert covered[..., 3].min() == 255
    assert ir.covered(ir.COVER_AT) and not ir.covered(0.1)
    title = ir.wipe_frame(None, size, ir.COVER_AT, False)
    assert title[..., :3].max() > 200  # REPLAY is drawn while covered (bright text)
    out = ir.wipe_frame(None, size, ir.COVER_AT, True)
    assert out[..., :3].max() < 200  # not on the way out


def test_the_bands_come_in_then_hold_then_leave(ir):
    w, slant = 1000, 100
    leads = [ir.band_lead(k / 100, w, slant) for k in range(101)]
    assert leads == sorted(leads)
    assert ir.band_lead(ir.COVER_AT, w, slant) == w + slant


def test_badge_blends_onto_an_opaque_frame_and_is_cut_to_fit(ir):
    frame = np.zeros((50, 80, 4), dtype=np.uint8)
    frame[..., 3] = 255
    badge = np.zeros((10, 30, 4), dtype=np.uint8)
    badge[..., 1], badge[..., 3] = 128, 128  # half see-through green, premultiplied
    ir.blend(frame, badge, 60, 45)  # hangs off the bottom right
    assert frame[46, 70, 1] == 128 and frame[46, 70, 3] == 255
    assert frame[40, 70, 1] == 0
    ir.blend(frame, badge, -100, -100)  # wholly off: nothing happens


def test_badge_says_the_speed_only_when_slowed(ir):
    normal, slow = ir.badge_image(720, 1.0), ir.badge_image(720, 0.5)
    assert slow.width() > normal.width() and normal.height() == slow.height()


def test_progress_line_fills_from_the_left(ir):
    frame = np.full((100, 200, 4), 200, dtype=np.uint8)
    ir.progress_line(frame, 0.5)
    assert tuple(frame[-1, 10, :3]) == (ir.LEAF[2], ir.LEAF[1], ir.LEAF[0])
    assert frame[-1, 150, 0] == 120  # the track: darker
    assert frame[50, 150, 0] == 200


# Playing a replay ----------------------------------------------------------------------------------


def _clip(path, seconds=2.0, fps=30, size=(160, 90)):
    from sprigcast.output.replay import ReplayBuffer

    start = 100.0
    buf = ReplayBuffer("replay test", size[0], size[1], fps, 800, "libx264", start, seconds=seconds + 5)
    buf.start()
    for i in range(int(seconds * fps)):
        rgba = np.zeros((size[1], size[0], 4), dtype=np.uint8)
        rgba[..., 0], rgba[..., 3] = (i * 3) % 256, 255
        buf.push_frame(Frame("x", size[0], size[1], rgba.tobytes(), start + i / fps))
        buf.push_audio(np.full((48000 // fps, 2), 0.1, dtype=np.float32))
        while buf._video.qsize() > 10:
            time.sleep(0.001)
    time.sleep(0.3)
    buf.save(path)
    buf.stop()
    return path


@needs_x264
def test_player_decodes_letterboxed_frames_and_slowed_sound(ir, tmp_path):
    path = _clip(tmp_path / "r.mkv")
    with av.open(str(path)) as f:  # the encoder may still hold the newest frames: go by what was saved
        times = [float(fr.pts * fr.time_base) for fr in f.decode(video=0)]
    whole = times[-1] - times[0]
    player = ir.Player(path, (200, 200), speed=0.5, skip_to=1.0).start()
    assert player.wait_first(5)
    pictures = []
    while not player.finished():
        picture = player.take(1e9)
        if picture is not None:
            pictures.append(picture)
        time.sleep(0.001)
    assert player.error is None
    assert whole - 1.1 <= player.length <= whole - 0.9  # all but the first second
    assert pictures and pictures[0].shape == (200, 200, 4)
    assert tuple(pictures[0][0, 0]) == (ir.DEEP[2], ir.DEEP[1], ir.DEEP[0], 255)  # letterbox
    sound = sum(len(a) for a in player.audio)
    assert sound > 48000 * 2 * player.length * 0.8  # sound at half speed lasts about twice as long


class FakeEngine:
    def __init__(self, collection):
        self._collection = collection
        self.taps, self.views, self.mixer = {}, {}, self
        self.audio = []

    def set_view(self, name, canvas_id=None, kind="bloom", ref=None):
        if canvas_id is None:
            self.views.pop(name, None)
        else:
            self.views[name] = (canvas_id, kind, ref)

    def add_frame_tap(self, cid, cb, nv12_size=None):
        self.taps.setdefault(cid, []).append(cb)

    def remove_frame_tap(self, cid, cb):
        self.taps[cid].remove(cb)

    def add_tap(self, cb, tracks=("stream",)):
        self.audio.append(cb)

    def remove_tap(self, cb):
        self.audio.remove(cb)


def _wait(test, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if test():
            return True
        time.sleep(0.01)
    return False


@needs_x264
def test_from_tapped_frames_to_a_replay_on_screen_and_back(ir, tmp_path, monkeypatch):
    import tempfile

    from sprigcast.output import recorder

    monkeypatch.setattr(recorder, "resolve_codec", lambda enc=None, streaming=False: "libx264")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(ir, "IN_S", 0.2)
    monkeypatch.setattr(ir, "OUT_S", 0.2)
    col = _collection()
    for canvas in col.canvases:
        canvas.width, canvas.height = 320, 180
    col.sources.append(SourceConfig(id="rp", kind=ir.KIND, name="Instant replay"))
    engine = FakeEngine(col)
    source = ir.InstantReplaySource("rp", {**ir.InstantReplaySource.defaults, "capture": "source:cam", "seconds": 5})
    source.engine = engine
    assert source.play() == "Instant replay isn't on air."
    source.start()
    assert _wait(lambda: source._buffer is not None)
    assert engine.views["replay-rp"][1] == "scene"
    buf = source._buffer
    tap = engine.taps["view:replay-rp"][0]
    start = buf.start_time
    for i in range(90):
        tap(Frame("view:replay-rp", 320, 180, np.full((180, 320, 4), 255, np.uint8).tobytes(), start + i / 30))
        engine.audio[0](np.full((1600, 2), 0.1, dtype=np.float32))
        while buf._video.qsize() > 10:
            time.sleep(0.001)
    assert _wait(lambda: source.ready_seconds >= 1.0)
    assert source.play(seconds=2) is None
    assert source.play() == "A replay is already playing."
    assert _wait(lambda: source._show is not None)
    first = source.video_frame()  # the wipe starts
    assert first is not None and first.shape == (720, 1280, 4)
    phases = set()
    sound = 0.0
    while source._show is not None:
        frame = source.video_frame()
        if source._show is not None:
            phases.add(source._show.phase)
        sound += float(np.abs(source.read_block(256)).sum())
        if frame is not None:
            assert frame.shape == (720, 1280, 4)
        time.sleep(0.004)
        assert len(phases) < 5
    assert {"in", "play", "out"} <= phases
    assert sound > 0
    assert [what for what, _ in source.outbox] == ["started", "ended"]
    assert source.outbox[1][1]["reason"] == "ended"
    assert not source.busy
    assert _wait(lambda: not list((tmp_path / "SprigCast").glob("replay-*.mkv")))  # tidied away
    source.sleep()  # off air: the encoder stays a while (KEEP_WARM_S), then stops keeping replays
    assert source.play() == "Instant replay isn't on air."
    source.wake()  # back on air within that time: the same encoder goes on, nothing new is opened
    assert _wait(lambda: source._want) and source._buffer is buf
    time.sleep(0.05)
    assert source._buffer is buf and engine.views["replay-rp"][1] == "scene"
    source.KEEP_WARM_S = 0.05
    source.sleep()
    assert _wait(lambda: source._buffer is None)
    assert "replay-rp" not in engine.views and not engine.taps["view:replay-rp"] and not engine.audio


def test_actions_and_hotkey_say_why_when_it_cannot_play(ir, app):
    from sprigcast.control.native import ApiError, NativeApi

    col = _collection()
    api = NativeApi(Host(col))
    with pytest.raises(ApiError, match="Add Instant replay"):
        api.run("replay.play", {})
    col.sources.append(SourceConfig(id="rp", kind=ir.KIND, name="Instant replay"))
    with pytest.raises(ApiError, match="isn't on air"):
        api.run("replay.play", {})
    with pytest.raises(ApiError):
        api.run("replay.play", {"speed": 3})
    assert api.run("replay.stop", {}) == {"stopped": []}
    from sprigcast import plugin_hooks

    assert ("plugin:replay:rp", "Instant replay") in plugin_hooks.hotkey_actions(col)
    said = []
    plugin_hooks.run_hotkey(api, "plugin:replay:rp", said.append)
    assert said == ["Instant replay isn't on air."]


def test_names_and_ids_from_apps_become_the_choice_and_unknown_ones_are_refused(ir):
    from sprigcast.control.native import ApiError, NativeApi

    col = _collection()
    assert ir.pick_ref(col, "Cam") == "source:cam"
    assert ir.pick_ref(col, "cam") == "source:cam"  # an id, or the name in any case
    assert ir.pick_ref(col, "frame") == "sprig:p-frame"  # a sprig's name wins
    assert ir.pick_ref(col, "source:Alert") == "source:alert"
    assert ir.pick_ref(col, "sprig:p-frame") == "sprig:p-frame" and ir.pick_ref(col, "") == ""
    with pytest.raises(ValueError, match="no sprig or source called 'Camera'"):
        ir.pick_ref(col, "Camera")
    with pytest.raises(ValueError, match="no picture"):
        ir.pick_ref(col, "Mic")
    assert ir.resolve(col, "Cam") == ("cam", "horizontal")  # settings saved as a name still work
    col.sources.append(SourceConfig(id="rp", kind=ir.KIND, name="Instant replay"))
    api = NativeApi(Host(col))
    with pytest.raises(ApiError, match="no sprig or source called 'Camera'"):
        api.run("source.set", {"source": "rp", "settings": {"capture": "Camera"}})
    assert col.source("rp").settings.get("capture") is None  # nothing was kept
    out = api.run("source.set", {"source": "rp", "settings": {"capture": "Frame", "corner": "Cam"}})
    assert out["settings"]["capture"] == "sprig:p-frame" and out["settings"]["corner"] == "source:cam"
    with pytest.raises(ApiError, match="no sprig or source"):
        api.run("source.new", {"name": "Replay 2", "kind": ir.KIND, "settings": {"capture": "Nope"}})


def test_a_bad_pick_says_what_it_was_when_it_tries_to_play(ir):
    col = _collection()
    with pytest.raises(ValueError, match="'Camera'"):
        ir.view_scene(col, {"capture": "Camera"}, "replay-x")
    engine = FakeEngine(col)
    source = ir.InstantReplaySource("rp", {**ir.InstantReplaySource.defaults, "capture": "Camera"})
    source.engine = engine
    source._want, source._gen = True, 1
    source._arm_now(1)
    assert source._buffer is None and "Camera" in source.status
    assert source.play() == "Instant replay: There's no sprig or source called 'Camera'."
