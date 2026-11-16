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
    assert set(fake.uploaded) == {"game", "soon", "cam"}  # just hidden: still ready
    engine._tick(fake, 100.0 + SLEEP_AFTER + 0.1)
    assert engine._sources["soon"].log == ["sleep"] and fake.forgotten == ["soon"]
    fake.uploaded.clear()
    engine._tick(fake, 110.0)
    assert set(fake.uploaded) == {"game", "cam"}  # a sleeping source isn't even asked for frames

    col.active_scenes["h"] = "b"  # switch: the scene's sources wake at once (hidden items too)
    engine._tick(fake, 111.0)
    assert engine._sources["soon"].log == ["sleep", "wake"] and "soon" in fake.uploaded
    assert not engine._sources["game"].asleep  # stays ready for a few seconds in case you switch back
    engine._tick(fake, 111.0 + SLEEP_AFTER + 0.1)
    assert engine._sources["game"].asleep
    assert not engine._sources["cam"].asleep  # cameras stay ready by default


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
