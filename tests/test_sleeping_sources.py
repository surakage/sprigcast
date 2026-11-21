"""Sources no scene on screen shows rest: no frames, no texture, no work."""

import numpy as np

from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.core.source import Source
from sprigcast.render.engine import CAMERA_SLEEP_AFTER, SLEEP_AFTER, Engine


class FakeCompositor:
    def __init__(self):
        self.parts, self.uploaded, self.forgotten, self.targets, self.effects = {}, [], [], {}, {}

    def upload(self, source_id, *a):
        self.uploaded.append(source_id)

    def forget(self, source_id):
        self.forgotten.append(source_id)

    def render(self, *a, **k):
        pass

    def read(self, key):
        return b"x"


class Sleeper(Source):
    sleeps = True
    kind = "window_capture"

    def __init__(self, source_id):
        super().__init__(source_id, {})
        self.log = []

    size = (8, 8)

    def start(self):
        self.log.append("start")

    def sleep(self):
        self.log.append("sleep")

    def wake(self):
        self.log.append("wake")

    def video_frame(self):
        return np.zeros((8, 8, 4), dtype=np.uint8)


def _engine():
    col = SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36)],
        sources=[SourceConfig(id=i, kind="color", name=i) for i in ("game", "soon", "cam")],
        scenes=[
            Scene(id="a", name="Gameplay", canvas="h", items=[SceneItem(source="game"), SceneItem(source="cam")]),
            Scene(id="b", name="Starting soon", canvas="h", items=[SceneItem(source="soon", visible=False)]),
        ],
        active_scenes={"h": "a"},
    )
    engine = Engine(col)
    engine.previews = frozenset()
    engine._sources = {i: Sleeper(i) for i in ("game", "soon", "cam")}
    engine._sources["cam"].kind = "camera"
    return engine, engine._collection


def test_only_sources_on_screen_work_and_the_rest_sleep_after_a_moment():
    engine, col = _engine()
    fake = FakeCompositor()
    engine._tick(fake, 100.0)
    assert set(fake.uploaded) == {"game", "cam"}  # off air (its bloom isn't on screen): asleep at once
    assert engine._sources["soon"].asleep and fake.forgotten == ["soon"]

    col.active_scenes["h"] = "b"  # a cut: what's on air now wakes, what left sleeps as the cut lands
    col.scenes[1].items[0].visible = True
    fake.uploaded.clear()
    engine._tick(fake, 101.0)
    assert engine._sources["soon"].log == ["sleep", "wake"] and "soon" in fake.uploaded
    assert engine._sources["game"].asleep and "game" in fake.forgotten  # no wait in case you switch back
    assert not engine._sources["cam"].asleep  # cameras stay ready by default


def test_an_eye_off_source_on_air_sleeps_after_a_moment():
    engine, col = _engine()
    fake = FakeCompositor()
    col.scenes[0].items[0].visible = False  # game's eye off, in the bloom on air
    engine._tick(fake, 100.0)
    assert not engine._sources["game"].asleep  # a quick hide and show stays instant
    engine._tick(fake, 100.0 + SLEEP_AFTER + 0.1)
    assert engine._sources["game"].asleep  # then it lets everything go
    col.scenes[0].items[0].visible = True
    engine._tick(fake, 110.0)
    assert engine._sources["game"].log == ["sleep", "wake"]


def test_policy_all_pauses_cameras_and_off_wakes_everything():
    engine, col = _engine()
    fake = FakeCompositor()
    col.active_scenes["h"] = "b"
    engine.sleep_policy = "all"
    engine._tick(fake, 1.0)
    engine._tick(fake, 1.0 + SLEEP_AFTER + 0.1)
    assert engine._sources["game"].asleep and not engine._sources["cam"].asleep  # cameras wait longer
    engine._tick(fake, 1.0 + CAMERA_SLEEP_AFTER + 0.1)
    assert engine._sources["cam"].asleep
    engine.sleep_policy = "off"
    engine._tick(fake, 20.0)
    assert not any(s.asleep for s in engine._sources.values())
    assert engine._sources["cam"].log == ["sleep", "wake"]


def test_live_cameras_stay_open_off_screen():
    """PC 2026-11-21: a camera reopened at each switch held the switch up to 2 s and came up in another format.
    Live, cameras stay open (and one paused before is opened ready for its bloom); other sources still rest."""
    engine, col = _engine()
    fake = FakeCompositor()
    col.active_scenes["h"] = "b"
    engine.sleep_policy = "all"
    engine._tick(fake, 1.0)
    engine._tick(fake, 1.0 + CAMERA_SLEEP_AFTER + 0.1)
    assert engine._sources["cam"].asleep
    engine.live = True
    engine._tick(fake, 20.0)
    assert not engine._sources["cam"].asleep and engine._sources["game"].asleep
    engine._tick(fake, 20.0 + CAMERA_SLEEP_AFTER + 0.1)
    assert not engine._sources["cam"].asleep
    engine.live = False  # off air again: it pauses after its wait (longer for a camera woken soon after a pause)
    engine._tick(fake, 1000.0)
    assert engine._sources["cam"].asleep


def test_a_source_not_on_screen_is_never_started(monkeypatch):
    from sprigcast.render import engine as module

    engine, _ = _engine()
    engine._sources = {}
    monkeypatch.setattr(module, "create_source", lambda kind, sid, settings: Sleeper(sid))
    fake = FakeCompositor()
    engine._sync_sources(fake)
    assert engine._sources["soon"].log == ["sleep"] and engine._sources["soon"].asleep
    assert engine._sources["game"].log == ["start"]


def test_image_is_decoded_only_when_asked_and_again_after_waking(tmp_path):
    from PySide6.QtGui import QColor, QImage

    from sprigcast.core.source import create_source

    path = tmp_path / "pic.png"
    image = QImage(40, 20, QImage.Format.Format_RGBA8888)
    image.fill(QColor(10, 20, 30, 255))
    assert image.save(str(path))
    src = create_source("image", "i", {"path": str(path)})
    assert src.size == (40, 20)  # from the header
    frame = src.video_frame()
    assert frame.shape == (20, 40, 4) and tuple(frame[5, 5]) == (10, 20, 30, 255)
    assert src.video_frame() is None
    src.wake()
    assert src.video_frame().shape == (20, 40, 4)


def test_a_camera_set_to_pause_off_screen_sleeps_like_the_rest():
    engine, col = _engine()
    fake = FakeCompositor()
    engine._sources["cam"].settings = {}  # cameras made before the setting existed pause too
    col.active_scenes["h"] = "b"
    engine._tick(fake, 1.0)
    engine._tick(fake, 1.0 + SLEEP_AFTER + 0.1)
    assert not engine._sources["cam"].asleep  # a camera gets 30 s: they take a second to open again
    engine._tick(fake, 1.0 + CAMERA_SLEEP_AFTER + 0.1)
    assert engine._sources["cam"].asleep
    engine._sources["cam"].settings = {"pause_off_screen": False}  # this camera is set to stay ready
    engine.sleep_policy = "not_cameras"
    engine._tick(fake, 40.0)
    assert not engine._sources["cam"].asleep
    engine.sleep_policy = "not_cameras"
    engine._sources["cam"].settings = {}
    engine.sleep_policy = "off"  # "never pause" still wins
    engine._tick(fake, 20.0)
    assert not engine._sources["cam"].asleep


def test_a_removed_plugins_sources_stop_now_not_at_restart(monkeypatch):
    from sprigcast.core import source as source_module

    engine, _col = _engine()
    compositor = FakeCompositor()
    engine._sync_sources(compositor)  # real colour sources in place of the test sleepers
    compositor.forgotten.clear()
    game = engine._sources["game"]
    stopped = []
    monkeypatch.setattr(game, "stop", lambda: stopped.append("game"))
    monkeypatch.setitem(source_module._REGISTRY, "color", None)  # as if its plugin was taken out
    monkeypatch.delitem(source_module._REGISTRY, "color")
    engine._sync_sources(compositor)
    from sprigcast.core import retire

    assert retire.RETIRER.wait(2.0)  # closed on the closing helper, not the render thread
    assert stopped and "game" not in engine._sources and "game" in compositor.forgotten
    engine._sync_sources(compositor)  # no new attempt (and no error) every time the blooms change
    assert "game" not in engine._sources and engine.events.empty()


def test_a_switch_waits_for_a_closed_camera_to_open(monkeypatch):
    """The bloom on air stays until the camera of the next one has a picture, so the cut lands on it."""
    from sprigcast.render import engine as module

    engine, col = _engine()
    fake = FakeCompositor()
    fake.textures, fake.rendered = {}, []
    fake.upload = lambda sid, *a: fake.textures.setdefault(sid, 1)
    fake.forget = lambda sid: fake.textures.pop(sid, None)
    fake.render = lambda canvas, scene, *a, **k: fake.rendered.append(scene.id if scene else None)
    engine.previews = None
    col.scenes[1].items.append(SceneItem(source="cam"))  # "Starting soon" shows the camera too
    col.scenes[0].items = [SceneItem(source="game")]
    engine._tick(fake, 1.0)
    engine._tick(fake, 1.0 + CAMERA_SLEEP_AFTER + 0.1)
    cam = engine._sources["cam"]
    assert cam.asleep and "cam" not in fake.textures
    cam.video_frame = lambda: None  # opening: no picture yet
    new = col.model_copy(deep=True)
    new.active_scenes["h"] = "b"
    engine._start_transitions(col, new)
    engine._collection = new
    clock = [module.time.perf_counter()]
    monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0])
    engine._tick(fake, 40.0)
    assert fake.rendered[-1] == "a" and not cam.asleep  # still the old bloom, camera opening
    cam.video_frame = lambda: np.zeros((8, 8, 4), dtype=np.uint8)
    engine._tick(fake, 40.1)
    assert fake.rendered[-1] == "b"  # its first picture is in: switch


class caplog_at:
    """Collect one logger's lines (INFO and up) inside a with block."""

    def __init__(self, logger):
        import logging

        self.logger, self.lines = logger, []
        outer = self

        class Keep(logging.Handler):
            def emit(self, record):
                outer.lines.append(record.getMessage())
        self.handler = Keep(logging.INFO)

    def __enter__(self):
        import logging

        self.level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)
        return self.lines

    def __exit__(self, *exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.level)


def test_one_long_frame_never_makes_a_source_drawn_less_often(monkeypatch):
    """PC 2026-11-18: one 237 ms media frame (Python's lock held elsewhere) had it drawn every 4th frame for 0.8 s.
    One frame counts at most CHARGE_CAP_MS."""
    from sprigcast.render import engine as module

    engine, _col = _engine()
    source = engine._sources["game"]
    for _ in range(30):
        engine._charge("game", source, 0.001)
    engine._charge("game", source, 0.237)
    assert engine._source_every.get("game", 1) == 1
    for _ in range(60):  # steadily slow: still drawn less often
        engine._charge("game", source, 0.03)
    assert engine._source_every["game"] == module.math.ceil(module.CHARGE_CAP_MS / module.SOURCE_BUDGET_MS)


def test_a_slow_source_is_drawn_less_often(monkeypatch):
    """A plugin that takes ~20 ms a frame is asked every 4th tick, so the canvas stays on time."""
    from sprigcast.render import engine as module

    engine, col = _engine()
    fake = FakeCompositor()
    calls = []
    clock = [0.0]
    slow = engine._sources["game"]

    def frame():
        calls.append(1)
        clock[0] += 0.02  # 20 ms of painting
        return np.zeros((8, 8, 4), dtype=np.uint8)
    slow.video_frame = frame
    real = module.time.perf_counter
    monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0] + real() * 0)
    for i in range(200):
        engine._tick(fake, 1.0 + i / 60)
    assert engine._source_every["game"] == 4
    assert 50 <= len(calls) <= 80  # about a quarter of 200 once the average settled
    assert engine._source_every.get("cam", 1) == 1  # quick sources every tick
    def quick():
        clock[0] += 0.001  # its animation ended: 1 ms a frame
        return np.zeros((8, 8, 4), dtype=np.uint8)
    slow.video_frame = quick
    with caplog_at(module.log) as lines:
        for i in range(400):
            engine._tick(fake, 5.0 + i / 60)
    assert engine._source_every["game"] == 1
    assert any("back to every frame" in line for line in lines)


def test_a_camera_needed_again_soon_stays_open_longer_each_time():
    from sprigcast.render.engine import CAMERA_BACK_SOON_S, CAMERA_SLEEP_MAX

    engine, col = _engine()
    fake = FakeCompositor()
    engine.sleep_policy = "all"
    t = 1.0

    def away_until_closed():
        nonlocal t
        col.active_scenes["h"] = "b"  # a bloom without the camera
        engine._tick(fake, t)
        wait = engine._camera_wait.get("cam", CAMERA_SLEEP_AFTER)
        engine._tick(fake, t + wait - 1)
        assert not engine._sources["cam"].asleep
        t += wait + 0.1
        engine._tick(fake, t)
        assert engine._sources["cam"].asleep
        return wait

    def back():
        nonlocal t
        t += 20
        col.active_scenes["h"] = "a"
        engine._tick(fake, t)
        assert not engine._sources["cam"].asleep

    col.active_scenes["h"] = "a"
    engine._tick(fake, t)
    waits = []
    for _ in range(6):
        waits.append(away_until_closed())
        back()
    assert waits == [30.0, 60.0, 120.0, 240.0, CAMERA_SLEEP_MAX, CAMERA_SLEEP_MAX]
    away_until_closed()
    t += CAMERA_BACK_SOON_S + 1  # left closed a long while: back to the short wait
    col.active_scenes["h"] = "a"
    engine._tick(fake, t)
    assert "cam" not in engine._camera_wait


def test_a_switch_waits_for_a_slow_waking_source_too(monkeypatch):
    """A PNGtuber lets its pictures go asleep; a switch back waits for them like it does for a camera."""
    from sprigcast.render import engine as module

    engine, col = _engine()
    fake = FakeCompositor()
    fake.textures, fake.rendered = {}, []
    fake.upload = lambda sid, *a: fake.textures.setdefault(sid, 1)
    fake.forget = lambda sid: fake.textures.pop(sid, None)
    fake.render = lambda canvas, scene, *a, **k: fake.rendered.append(scene.id if scene else None)
    engine.previews = None
    col.scenes[1].items = [SceneItem(source="soon")]
    tuber = engine._sources["soon"]
    tuber.slow_wake = True
    engine._tick(fake, 1.0)
    engine._tick(fake, 1.0 + SLEEP_AFTER + 0.1)
    assert tuber.asleep
    tuber.video_frame = lambda: None  # loading its pictures again
    new = col.model_copy(deep=True)
    new.active_scenes["h"] = "b"
    engine._start_transitions(col, new)
    engine._collection = new
    clock = [module.time.perf_counter()]
    monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0])
    engine._tick(fake, 40.0)
    assert fake.rendered[-1] == "a" and not tuber.asleep
    tuber.video_frame = lambda: np.zeros((8, 8, 4), dtype=np.uint8)
    engine._tick(fake, 40.1)
    assert fake.rendered[-1] == "b"


def test_a_source_near_its_budget_settles_instead_of_flapping(monkeypatch):
    """Instant replay at 5.1-5.2 ms switched between every frame and every 2nd one 6 times in 6 s (heavy run,
    2026-11-17). Back to every frame only once it's well under the budget."""
    from sprigcast.render import engine as module

    engine, col = _engine()
    fake = FakeCompositor()
    clock = [0.0]
    slow = engine._sources["game"]
    cost = [0.0053]

    def frame():
        clock[0] += cost[0]
        return np.zeros((8, 8, 4), dtype=np.uint8)
    slow.video_frame = frame
    real = module.time.perf_counter
    monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0] + real() * 0)
    changes, last = 0, 1
    for phase in range(12):
        cost[0] = 0.0053 if phase % 2 == 0 else 0.0047
        for i in range(120):
            engine._tick(fake, 1.0 + phase * 2 + i / 60)
            now = engine._source_every.get("game", 1)
            changes += now != last
            last = now
    assert changes == 1 and last == 2
    cost[0] = 0.002  # well under: back to every frame
    for i in range(300):
        engine._tick(fake, 40.0 + i / 60)
    assert engine._source_every.get("game", 1) == 1


def test_a_bloom_coming_back_spreads_its_first_pictures_over_a_few_ticks(caplog):
    """PC 2026-11-18: switching back to a bloom of ~17 sources copied every first picture (GIFs, videos, cards) to the
    card in one tick, 130-220 ms. At most FIRST_UPLOAD_BYTES of first pictures go each tick, the rest the next; the
    switch waits for them (they had a picture when they went to sleep), and a log line names the slow ones."""
    import logging

    from sprigcast.render.engine import FIRST_UPLOAD_BYTES

    class Big(Sleeper):
        def video_frame(self):
            return np.zeros((1600, 1600, 4), dtype=np.uint8)  # ~10 MB, like a 1080p picture

    ids = ["g1", "g2", "g3", "g4"]
    col = SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36)],
        sources=[SourceConfig(id=i, kind="color", name=i) for i in ids + ["other"]],
        scenes=[Scene(id="heavy", name="Heavy", canvas="h", items=[SceneItem(source=i) for i in ids]),
                Scene(id="calm", name="Calm", canvas="h", items=[SceneItem(source="other")])],
        active_scenes={"h": "heavy"},
    )
    engine = Engine(col)
    engine.previews = frozenset()
    engine._sources = {i: Big(i) for i in ids} | {"other": Sleeper("other")}
    col = engine._collection

    class Comp(FakeCompositor):
        def __init__(self):
            super().__init__()
            self.textures = {}

        def upload(self, source_id, *a):
            super().upload(source_id)
            self.textures[source_id] = object()

        def forget(self, source_id):
            super().forget(source_id)
            self.textures.pop(source_id, None)

    fake = Comp()
    engine._tick(fake, 1.0)
    assert set(ids) <= set(fake.uploaded)  # starting up: everything at once, as before
    col.active_scenes["h"] = "calm"
    engine._tick(fake, 2.0)
    assert all(engine._sources[i].asleep for i in ids) and engine._had_picture >= set(ids)
    back = col.model_copy(deep=True)
    back.active_scenes["h"] = "heavy"
    engine._wait_for_waking("h", col.scenes[1], back, "heavy")
    assert engine._waking["h"][2] == set(ids)  # the switch waits for their pictures
    engine._collection = back
    fake.uploaded.clear()
    with caplog.at_level(logging.INFO, logger="sprigcast.render.engine"):
        engine._tick(fake, 3.0)
    first = [i for i in fake.uploaded if i in ids]
    assert 0 < len(first) < len(ids) and len(first) * 1600 * 1600 * 4 <= FIRST_UPLOAD_BYTES + 1600 * 1600 * 4
    engine._tick(fake, 3.02)
    assert set(ids) <= set(fake.uploaded)  # the rest a tick later
    assert engine._still_waking(fake, "h", 3.03) is None  # all there: the switch lands


def test_a_plugins_settings_reach_the_engine_without_a_whole_garden(monkeypatch):
    """PC 2026-11-18: a stream timer hotkey copied and checked the whole garden on the window's thread (544 ms) and
    the stream skipped ~440 frames. Only the changed settings travel now, applied on top of any garden sent before."""
    from types import SimpleNamespace

    from sprigcast.control.native import commit_settings
    from sprigcast.core.events import ReplaceCollection, UpdateSourceSettings

    engine, col = _engine()
    updated = []
    engine._sources["game"].update = lambda settings: updated.append(dict(settings))
    engine.send(UpdateSourceSettings({"game": {"seconds": 5}}))
    assert engine._drain_commands(FakeCompositor())
    assert next(s for s in engine._collection.sources if s.id == "game").settings == {"seconds": 5}
    assert updated == [{"seconds": 5}]
    # A whole garden queued before newer settings: the settings still win.
    older = engine._collection.model_copy(deep=True)
    next(s for s in older.sources if s.id == "game").settings = {"seconds": 1}
    monkeypatch.setattr(engine, "_sync_sources", lambda comp: None)
    engine.send(ReplaceCollection(older))
    engine.send(UpdateSourceSettings({"game": {"seconds": 9}}))
    engine._drain_commands(FakeCompositor())
    assert next(s for s in engine._collection.sources if s.id == "game").settings == {"seconds": 9}

    light, full = [], []
    commit_settings(SimpleNamespace(commit_settings=light.append, commit=full.append), [1])
    commit_settings(SimpleNamespace(commit=full.append), [1], "timer")
    assert light == [[1]] and full == ["timer"]  # hosts without it (apps' test hosts) get the whole commit


def test_a_held_switch_draws_the_new_blooms_sprigs_only_once_it_shows():
    """While the old bloom is held for a waking source, the new bloom's sources wake but its sprigs aren't
    drawn (both blooms' sprigs each held tick: "sprigs 51.9 ms" on the PC, 2026-11-18)."""
    engine, col = _engine()
    old = col.scenes[0]
    col.active_scenes["h"] = "b"
    engine._waking["h"] = (old, 1e9, {"soon"})
    assert {s.id for s in engine._drawn_scenes(col)} == {"a", "b"}  # its sources wake
    assert {s.id for s in engine._drawn_scenes(col, frozenset({"h"}))} == {"a"}  # its sprigs wait
    fake = FakeCompositor()
    fake.textures = {"soon": 1}
    engine._sprigs_held = frozenset({"h"})  # left out this tick: it shows from the next one, sprigs drawn
    assert engine._still_waking(fake, "h", 0.0) is old
    engine._sprigs_held = frozenset()
    assert engine._still_waking(fake, "h", 0.0) is None


def test_slow_wakes_are_spread_over_ticks(monkeypatch):
    """A bloom's sources waking all in one tick held that frame for every canvas (PC soak 2026-11-19): once waking
    has taken WAKE_BUDGET_S of a tick, the rest wake on the next ticks."""
    from sprigcast.render import engine as module

    engine, col = _engine()
    fake = FakeCompositor()
    col.scenes[1].items = [SceneItem(source=i) for i in ("game", "soon", "cam")]
    col.scenes[0].items = []
    col.active_scenes["h"] = "a"
    engine._tick(fake, 1.0)
    engine._tick(fake, 1.0 + CAMERA_SLEEP_AFTER + SLEEP_AFTER + 0.1)
    assert all(s.asleep for s in engine._sources.values())
    clock = [100.0]
    monkeypatch.setattr(module.time, "perf_counter", lambda: clock[0])

    def slow_wake(source):
        def wake():
            source.log.append("wake")
            clock[0] += module.WAKE_BUDGET_S * 1.5
        return wake

    for source in engine._sources.values():
        source.wake = slow_wake(source)
    col.active_scenes["h"] = "b"
    awake = []
    for step in range(3):
        engine._tick(fake, 50.0 + step / 60)
        awake.append(sum(not s.asleep for s in engine._sources.values()))
    assert awake == [1, 2, 3]
    assert all(s.log.count("wake") == 1 for s in engine._sources.values())
