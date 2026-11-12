from sprigcast.app.resources import describe
from sprigcast.core.scene import Canvas, Scene, SceneCollection
from sprigcast.render.engine import Engine


class FakeCompositor:
    def __init__(self):
        self.rendered, self.parts = [], {}

    def upload(self, *a):
        pass

    def render(self, canvas, scene, sizes, previous=None, progress=1.0):
        self.rendered.append(canvas.id)

    def read(self, key):
        return b"x"


def _engine():
    col = SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36), Canvas(id="v", width=36, height=64)],
        scenes=[Scene(id="a", name="A", canvas="h"), Scene(id="b", name="B", canvas="v")],
        active_scenes={"h": "a", "v": "b"},
    )
    return Engine(col)


def _run(engine, fake, seconds=1.0, fps=60):
    for i in range(int(seconds * fps)):
        engine._tick(fake, 10.0 + i / fps)


def test_unwatched_canvases_are_not_drawn_or_copied():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset()  # minimised
    _run(engine, fake)
    assert fake.rendered == [] and engine.readbacks == 0


def test_previews_are_copied_at_most_30_times_a_second():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset({"h"})
    _run(engine, fake)
    assert 29 <= fake.rendered.count("h") <= 31 and "v" not in fake.rendered
    assert engine.frames["h"].take() is not None and engine.frames["v"].take() is None


def test_outputs_get_every_frame_whether_watched_or_not():
    engine, fake = _engine(), FakeCompositor()
    engine.previews = frozenset()
    got = []
    engine.add_frame_tap("v", got.append)
    _run(engine, fake)
    assert len(got) == 60 and fake.rendered.count("v") == 60
    assert engine.frames["v"].take() is None  # not shown, so not handed to the UI


def test_meter_text():
    assert describe({}) == ""
    assert describe({"cpu": 3.6, "ram_mb": 311.8, "gpu": None}) == "CPU 4% · RAM 312 MB"
    assert describe({"cpu": 3.6, "ram_mb": 311.8, "gpu": 18.2}).endswith("· GPU 18%")


def test_outputs_of_one_size_share_one_nv12_conversion():
    class Nv12Fake(FakeCompositor):
        def __init__(self):
            super().__init__()
            self.converted = []

        def read_nv12(self, key, size):
            self.converted.append(size)
            return b"y" * (size[0] * size[1] * 3 // 2)

    engine, fake = _engine(), Nv12Fake()
    engine.previews = frozenset()
    a, b, c = [], [], []
    engine.add_frame_tap("h", a.append, (32, 18))
    engine.add_frame_tap("h", b.append, (32, 18))
    engine.add_frame_tap("h", c.append, (16, 8))
    engine._tick(fake, 10.0)
    assert fake.converted == [(32, 18), (16, 8)] and a[0] is b[0] and a[0].fmt == "nv12"
    engine.remove_frame_tap("h", b.append)
    assert len(engine._taps["h"]) == 2
