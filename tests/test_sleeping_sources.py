"""Sources no scene on screen shows rest: no frames, no texture, no work."""

import numpy as np

from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.core.source import Source
from sprigcast.render.engine import SLEEP_AFTER, Engine


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
    assert engine._sources["cam"].asleep and engine._sources["game"].asleep
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
