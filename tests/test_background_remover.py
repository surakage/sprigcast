"""Background remover store plugin: the filter, its watcher thread, the engine feeding it, and the GPU mix."""

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from sprigcast import plugin_hooks, plugin_store
from sprigcast.core.filters import filter_kind, video_effects
from sprigcast.core.scene import Canvas, Filter, Scene, SceneCollection, SceneItem, SourceConfig

pytest.importorskip("PySide6.QtWidgets")

MODELS_DIR = Path(__file__).resolve().parent.parent / "models" / "background"


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def plugin(tmp_path, app):
    """The plugin added for one test (removed afterwards); its module."""
    import sys

    plugins = tmp_path / "plugins"
    store = next(p for p in plugin_store.catalog() if p.id == "background_remover")
    assert plugin_store.install(plugins, store) is None
    yield sys.modules["sprigcast_plugin_background_remover"]
    plugin_store.remove(plugins, "background_remover")


def _bg(**settings):
    return Filter(id="f", name="Background remover", kind="background", settings=settings)


def test_does_nothing_until_added_then_maps_its_modes(plugin):
    info = filter_kind("background")
    assert info is not None and info.status is not None
    assert [f.key for f in info.fields] == ["mode", "blur", "picture_file", "better_edges"]
    assert video_effects([_bg(mode="remove")]).cutout == 1
    fx = video_effects([_bg(mode="blur", blur=100)])
    assert fx.cutout == 2 and fx.cutout_blur > 0
    assert video_effects([_bg(mode="replace", picture_file="C:/a.png")]).cutout == 3
    assert video_effects([_bg(mode="replace")]).cutout == 1  # no picture yet: see-through
    assert not video_effects([_bg(mode="blur")][:0]).active
    assert plugin.status("cam") == "Starts when it's on screen"


def test_removed_plugin_leaves_the_camera_alone(tmp_path, app):
    assert filter_kind("background") is None
    assert video_effects([_bg(mode="remove")]).cutout == 0
    assert "background" not in plugin_hooks.frame_watchers()


def test_shrink_reads_every_frame_layout(plugin):
    bgr = np.zeros((720, 1280, 3), np.uint8)
    bgr[..., 2] = 200  # red, stored B G R
    small = plugin.shrink(bgr, "BGR", 320)
    assert small.shape == (180, 320, 3) and tuple(small[0, 0]) == (200, 0, 0)
    rgba = np.zeros((720, 1280, 4), np.uint8)
    rgba[..., 1] = 150
    assert tuple(plugin.shrink(rgba, "RGBA", 320)[5, 5]) == (0, 150, 0)
    # Packed 4:2:0, full range grey: Y 128, U = V = 128.
    w, h = 1280, 720
    planes = np.full((h + h // 2, w), 128, np.uint8)
    small = plugin.shrink(planes, "I420/709/pc", 320)
    assert small.shape[1] <= 320 and small.shape[0] >= 170
    assert np.allclose(small[3, 3], 128, atol=2)


def test_box_blur_and_resize_are_right(plugin):
    a = np.random.default_rng(1).random((30, 40)).astype(np.float32)
    padded = np.pad(a, 2, mode="edge")
    want = np.array([[padded[i:i + 5, j:j + 5].mean() for j in range(40)] for i in range(30)])
    assert np.abs(plugin.box(a, 2) - want).max() < 1e-5
    assert plugin.resize(a, (40, 30)) is a
    up = plugin.resize(np.array([[0.0, 1.0]], np.float32), (4, 1))
    assert up[0, 0] == 0 and up[0, -1] == 1 and 0 < up[0, 1] < up[0, 2] < 1


class FakeModel:
    """A 'model' that sees a person in the middle third."""
    where = "graphics card"

    def __init__(self, which):
        self.which = which

    def person(self, rgb):
        h, w = rgb.shape[:2]
        out = np.zeros((h, w), np.float32)
        out[:, w // 3:2 * w // 3] = 1
        return out


def test_watcher_masks_the_middle_and_blurs_the_room(plugin):
    loads = []
    cut = plugin.Cutout("cam", loader=lambda which, sid, cancel: loads.append(which) or FakeModel(which))
    small = np.full((180, 320, 3), 90, np.uint8)
    out = cut.process(small, {"mode": "blur", "blur": 60, "better_edges": False})
    assert loads == ["wide"]
    mask = out["mask"]
    assert mask.shape == (180, 320) and mask[90, 160] > 240 and mask[90, 10] < 10
    assert out["backdrop"].shape == (90, 160, 3) and abs(int(out["backdrop"][40, 10, 0]) - 90) <= 2
    assert plugin.status("cam") == "On, using the graphics card"
    # Square cameras use the square model; Better edges its own.
    cut.process(np.zeros((240, 240, 3), np.uint8), {"mode": "remove"})
    cut.process(small, {"mode": "remove", "better_edges": True})
    assert loads == ["wide", "square", "better"]
    assert cut.process(small, {"mode": "remove"})["backdrop"] is None
    cut.close()
    assert plugin.status("cam") == "Starts when it's on screen"


def test_mask_settles_over_time_instead_of_flickering(plugin):
    cut = plugin.Cutout("cam", loader=lambda *a: FakeModel(a[0]))
    small = np.zeros((180, 320, 3), np.uint8)
    cut.process(small, {"mode": "remove"})
    cut.process(small, {"mode": "remove"})

    class Flicker(FakeModel):
        def person(self, rgb):
            out = super().person(rgb)
            out[:, :20] = 0.45  # a faint wrong guess for one frame
            return out

    cut.model = Flicker("wide")
    mask = cut.process(small, {"mode": "remove"})["mask"]
    assert mask[90, 5] < 60  # damped, not jumping halfway on


def test_failed_download_is_said_and_retried_later(plugin):
    from sprigcast.models import ModelError

    calls = []

    def fail(which, sid, cancel):
        calls.append(which)
        raise ModelError("Couldn't download it. Check your internet and try again.")

    cut = plugin.Cutout("cam", loader=fail)
    small = np.zeros((180, 320, 3), np.uint8)
    assert cut.process(small, {"mode": "remove"}) is None
    assert cut.process(small, {"mode": "remove"}) is None
    assert calls == ["wide"]  # not hammering the network every frame
    assert plugin.status("cam").startswith("Couldn't download it")
    cut.close()


def test_watcher_thread_takes_frames_and_lets_go(plugin):
    done = threading.Event()

    class Model(FakeModel):
        def person(self, rgb):
            done.set()
            return super().person(rgb)

    cut = plugin.Cutout("cam", loader=lambda *a: Model(a[0]))
    frame = np.zeros((720, 1280, 3), np.uint8)
    cut.offer(frame, "BGR", {"mode": "remove"})
    assert done.wait(5)
    for _ in range(100):
        result = cut.take()
        if result is not None:
            break
        time.sleep(0.02)
    assert result is not None and result["mask"].shape == (180, 320)
    assert cut.take() is None  # handed over once
    cut.close()
    cut._thread.join(2)
    assert not cut._thread.is_alive() and cut.model is None


# The engine: feeds a watcher only while the source shows, uploads results, lets go when parked ----------

class FakeCompositor:
    def __init__(self):
        self.parts, self.targets, self.cutouts, self.forgot = {}, {}, {}, []

    def upload(self, *a):
        pass

    def upload_cutout(self, source_id, mask, backdrop=None):
        self.cutouts[source_id] = mask

    def forget_cutout(self, source_id):
        self.forgot.append(source_id)

    def forget(self, source_id):
        pass

    def render(self, *a, **k):
        pass

    def read(self, key):
        return b""


class Watcher(plugin_hooks.FrameWatcher):
    made = []

    def __init__(self, source_id):
        self.offers, self.closed = 0, False
        Watcher.made.append(self)

    def offer(self, frame, order, settings):
        self.offers += 1
        self.settings = settings

    def take(self):
        return {"mask": np.full((2, 2), 255, np.uint8)} if self.offers else None

    def close(self):
        self.closed = True


def _collection(visible=True, on=True):
    flt = Filter(id="f", name="Bg", kind="test_watch", enabled=on, settings={"mode": "blur"})
    return SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36)],
        sources=[SourceConfig(id="cam", kind="color", name="Cam", filters=[flt]),
                 SourceConfig(id="other", kind="color", name="Other")],
        scenes=[Scene(id="a", name="A", canvas="h",
                      items=[SceneItem(source="cam", visible=visible), SceneItem(source="other")])],
        active_scenes={"h": "a"},
    )


@pytest.fixture
def watched():
    from sprigcast.core.filters import _KINDS, FilterKind

    _KINDS["test_watch"] = FilterKind("test_watch", "Test", "video", [], builtin=False)
    plugin_hooks._FRAME_WATCHERS["test_watch"] = Watcher
    Watcher.made.clear()
    yield
    _KINDS.pop("test_watch", None)
    plugin_hooks._FRAME_WATCHERS.pop("test_watch", None)


def _engine(collection):
    from sprigcast.render.engine import Engine

    engine, fake = Engine(collection), FakeCompositor()
    engine.previews = frozenset()
    engine._sync_sources(fake)
    for source in engine._sources.values():  # a colour source only sends its picture once: send one every tick
        source.video_frame = lambda: np.zeros((4, 4, 4), np.uint8)
    return engine, fake


def test_engine_feeds_a_shown_source_and_uploads_its_mask(watched):
    engine, fake = _engine(_collection())
    engine._tick(fake, 10.0)
    engine._tick(fake, 10.1)
    assert len(Watcher.made) == 1 and Watcher.made[0].offers >= 1
    assert Watcher.made[0].settings == {}  # its kind has no fields: settings_for drops unknown keys
    assert "cam" in fake.cutouts and "other" not in fake.cutouts


def test_engine_skips_hidden_sources_and_closes_when_the_filter_goes(watched):
    engine, fake = _engine(_collection(visible=False))
    engine._tick(fake, 10.0)
    engine._tick(fake, 10.1)
    assert not Watcher.made  # eye off: the model never runs
    engine, fake = _engine(_collection())
    engine._tick(fake, 10.0)
    engine._tick(fake, 10.1)
    watcher = Watcher.made[-1]
    engine._collection = _collection(on=False)
    engine._sync_sources(fake)
    assert watcher.closed and "cam" in fake.forgot


def test_engine_parks_a_watcher_nothing_feeds(watched):
    from sprigcast.render import engine as engine_mod

    engine, fake = _engine(_collection())
    engine._tick(fake, 10.0)
    engine._tick(fake, 10.1)
    watcher = Watcher.made[-1]
    engine._visible = set()
    engine._visible_sources = lambda: set()  # keep it "not showing"
    engine._show_hidden = lambda: None
    engine._tick(fake, 10.1 + engine_mod.WATCHER_PARK_S + 1)
    assert watcher.closed and not engine._watchers


# The GPU mix ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def compositor():
    pytest.importorskip("moderngl")
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    yield comp
    comp.release()


def _pixels(comp, canvas):
    return np.frombuffer(comp.read(canvas.id), dtype=np.uint8).reshape(canvas.height, canvas.width, 4)


def _draw(comp, fx, mask=None, backdrop=None):
    from sprigcast.core.filters import VideoEffects

    canvas = Canvas(id="bg", width=32, height=16)
    comp.upload("cam", np.full((16, 32, 3), (0, 0, 255), np.uint8), "BGR")  # red camera
    comp.forget_cutout("cam")
    if mask is not None:
        comp.upload_cutout("cam", mask, backdrop)
    comp.effects = {"cam": fx}
    comp.render(canvas, Scene(id="s", name="s", canvas="bg", items=[SceneItem(source="cam")]), {"cam": (32, 16)})
    comp.effects = {}
    return _pixels(comp, canvas)


def _half_mask():
    mask = np.zeros((16, 32), np.uint8)
    mask[:, 16:] = 255  # you are the right half
    return mask


def test_gpu_removes_the_room_and_hides_it_until_the_first_mask(compositor):
    from sprigcast.core.filters import VideoEffects

    fx = VideoEffects(cutout=1, active=True)
    img = _draw(compositor, fx, _half_mask())
    assert img[8, 28, 3] == 255 and tuple(img[8, 28, :3]) == (255, 0, 0)
    assert img[8, 3, 3] == 0  # the room: see-through
    assert _draw(compositor, fx)[8, 28, 3] == 0  # no mask yet: nothing shows, never the room


def test_gpu_blurs_or_replaces_the_room(compositor, tmp_path):
    from PIL import Image

    from sprigcast.core.filters import VideoEffects

    back = np.zeros((8, 16, 3), np.uint8)
    back[...] = (0, 0, 200)  # the plugin's blurred room: blue here
    img = _draw(compositor, VideoEffects(cutout=2, cutout_blur=8, active=True), _half_mask(), back)
    assert tuple(img[8, 3, :3]) == (0, 0, 200) and tuple(img[8, 28, :3]) == (255, 0, 0)
    path = tmp_path / "beach.png"
    Image.new("RGB", (64, 64), (0, 255, 0)).save(path)
    img = _draw(compositor, VideoEffects(cutout=3, cutout_picture=str(path), active=True), _half_mask())
    assert tuple(img[8, 3, :3]) == (0, 255, 0) and img[8, 3, 3] == 255 and tuple(img[8, 28, :3]) == (255, 0, 0)


# The real models (CPU), when onnxruntime is installed ------------------------------------------------------

def test_real_models_find_a_person_shaped_blob(plugin):
    pytest.importorskip("onnxruntime")
    for which in ("wide", "better"):
        file = MODELS_DIR / plugin.MODELS[which][0]
        import hashlib

        assert hashlib.sha256(file.read_bytes()).hexdigest() == plugin.MODELS[which][1]
        assert file.stat().st_size == plugin.MODELS[which][2]
        model = plugin.Model(file, which)
        out = model.person(np.full((180, 320, 3), 128, np.uint8))
        w, h = plugin.MODELS[which][3]
        assert out.shape == (h, w) and 0 <= out.min() and out.max() <= 1
