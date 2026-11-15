"""Store plugins that change how things look or add a show: Hide it, Barrel Roll and Snap."""

import numpy as np
import pytest

from sprigcast import plugin_hooks, plugin_store
from sprigcast.control.native import ApiError, NativeApi
from sprigcast.core.filters import filter_kind, video_effects
from sprigcast.core.scene import Filter, SourceConfig
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")

from test_control_api import FakeHost, _collection  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _store(pid):
    return next(p for p in plugin_store.catalog() if p.id == pid)


@pytest.fixture
def added(tmp_path, app):
    """Add store plugins by id for one test; they're removed afterwards."""
    plugins = tmp_path / "plugins"
    ids = []

    def add(pid):
        assert plugin_store.install(plugins, _store(pid)) is None
        ids.append(pid)

    yield add
    for pid in ids:
        plugin_store.remove(plugins, pid)


class Host(FakeHost):
    """FakeHost plus what Snap uses: running sources and pictures of them."""

    def __init__(self, collection, folder=None):
        super().__init__(collection)
        self.instances = {}
        self.grabs = []
        self.folder = folder

    def source_instance(self, source_id):
        return self.instances.get(source_id)

    def grab_picture(self, source_id, callback):
        self.grabs.append(source_id)
        picture = np.zeros((90, 160, 4), dtype=np.uint8)
        picture[..., 0], picture[..., 3] = 220, 255
        callback(picture)

    def stats(self):
        return {"recordDirectory": str(self.folder)}


def test_the_store_offers_all_three_and_none_run_until_added():
    ids = {p.id for p in plugin_store.catalog()}
    assert {"hide_it", "barrel_roll", "snap"} <= ids
    assert filter_kind("pixelate") is None and filter_kind("barrel_roll") is None and "snap" not in known_kinds()
    # Without the plugin, a saved Pixelate filter does nothing.
    assert not video_effects([Filter(id="f", name="x", kind="pixelate", settings={"size": 20})]).active


def test_hide_it_pixelates_for_a_while_then_shows_it_again(added):
    added("hide_it")
    api = NativeApi(FakeHost(_collection()))  # made before the plugin? Its actions still show up
    assert "hide.pixelate" in api.all_actions()
    out = api.run("hide.pixelate", {"source": "Cam", "seconds": 15})
    assert out == {"source": "Cam", "hidden": "pixelate", "seconds": 15.0}
    cam = api.col.source("cam")
    assert video_effects(cam.filters).pixelate == 24
    api.run("hide.blackout", {"source": "Cam"})  # one at a time
    assert video_effects(cam.filters).blackout == 1.0 and video_effects(cam.filters).pixelate == 0
    assert api.run("hide.show", {"source": "Cam"})["was"] == "blackout"
    assert not video_effects(cam.filters).active
    assert api.run("hide.toggle", {"source": "Cam"})["hidden"] == "pixelate"
    assert api.run("hide.toggle", {"source": "Cam"})["hidden"] is None


def test_timed_filters_switch_off_on_their_own(added, app):
    from test_control_api import _wait

    added("hide_it")
    api = NativeApi(FakeHost(_collection()))
    api.run("hide.blackout", {"source": "Cam", "seconds": 0.05})
    cam = api.col.source("cam")
    assert plugin_hooks.find_filter(cam, "blackout").enabled
    _wait(app, lambda: not plugin_hooks.find_filter(cam, "blackout").enabled, 2.0)
    # A second trigger while it's on restarts the time instead of being cut short by the first.
    api.run("hide.blackout", {"source": "Cam", "seconds": 0.05})
    api.run("hide.blackout", {"source": "Cam", "seconds": 30})
    _wait(app, lambda: False, 0.2)
    assert plugin_hooks.find_filter(cam, "blackout").enabled


def test_barrel_roll_spins_every_place_a_sprig_is_shown(added):
    added("barrel_roll")
    api = NativeApi(FakeHost(_collection()))
    out = api.run("roll.spin", {"sprig": "Frame", "seconds": 15})
    assert sorted(out["spinning"]) == ["Frame", "Frame"] and out["turns"] == 15
    fx = video_effects(api.col.source("frame-h").filters)
    assert fx.spin == (15.0, 15.0, 1)
    api.run("roll.spin", {"source": "Cam", "turns": 2, "seconds": 1, "direction": "anticlockwise"})
    assert video_effects(api.col.source("cam").filters).spin == (-2.0, 1.0, 1)
    with pytest.raises(ApiError):
        api.run("roll.spin", {"sprig": "Frame", "seconds": "lots"})


def test_plugin_hotkeys_and_right_click_entries(added):
    from sprigcast.app import hotkeys

    added("hide_it")
    added("barrel_roll")
    col = _collection()
    actions = dict(hotkeys.actions(col))
    assert "plugin:roll:p-frame" in actions
    api = NativeApi(FakeHost(col))
    assert plugin_hooks.run_hotkey(api, "plugin:roll:p-frame")
    assert video_effects(col.source("frame-h").filters).spin[2] == 1
    col.sources.append(SourceConfig(id="fc", kind="camera", name="Facecam"))
    entries = []
    plugin_hooks.fill_item_menu(api, col.source("fc"), lambda label, fn: entries.append(label))
    assert any(e.startswith("Pixelate") for e in entries) and any(e.startswith("Barrel roll") for e in entries)


def test_removing_a_plugin_takes_its_filters_actions_and_hotkeys(added, tmp_path):
    from sprigcast.app import hotkeys

    added("barrel_roll")
    api = NativeApi(FakeHost(_collection()))
    assert filter_kind("barrel_roll") is not None
    plugin_store.remove(tmp_path / "plugins", "barrel_roll")
    assert filter_kind("barrel_roll") is None and "roll.spin" not in api.all_actions()
    assert not any(a.startswith("plugin:roll:") for a, _ in hotkeys.actions(_collection()))
    with pytest.raises(ApiError):
        api.run("roll.spin", {"sprig": "Frame"})


def _shot(tmp_path, colour=(220, 30, 30)):
    from PySide6.QtGui import QColor, QImage

    image = QImage(160, 90, QImage.Format.Format_RGBA8888_Premultiplied)
    image.fill(QColor(*colour))
    folder = tmp_path / "Screenshots"
    folder.mkdir(exist_ok=True)
    path = folder / "screenshot horizontal 2026-11-14 20-12-43.png"
    image.save(str(path))
    return [{"shape": "horizontal", "path": str(path), "image": image},
            {"shape": "vertical", "path": str(folder / "v.png"), "image": None}]


def test_a_screenshot_prints_as_a_polaroid(added, tmp_path):
    import sys
    import time

    added("snap")
    to_array = sys.modules["sprigcast_plugin_snap"].to_array
    col = _collection()
    col.sources.append(SourceConfig(id="snap-1", kind="snap", name="Snap", settings={"save_polaroid": True}))
    host = Host(col, tmp_path)
    snap = host.instances["snap-1"] = create_source("snap", "snap-1", {"seconds": 1.0})
    api = NativeApi(host)
    assert snap.size == (800, 1000)
    first = snap.video_frame()
    assert first.shape == (1000, 800, 4) and not first[..., 3].any()  # see-through until a screenshot
    assert snap.video_frame() is None

    plugin_hooks.screenshot_taken(api, _shot(tmp_path))
    frame = snap.video_frame()
    assert frame.shape == (1000, 800, 4) and snap.busy
    job = snap._job
    assert to_array(snap.paint(job, 0.5))[..., 3].any()  # the camera
    held = to_array(snap.paint(job, 3.2))
    assert held[500, 400, 3] == 255 and held[500, 400, 0] > 180  # the red screenshot in the middle
    job.started -= 100
    assert not snap.video_frame()[..., 3].any() and not snap.busy  # gone again

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not list((tmp_path / "Screenshots").glob("snap *.png")):
        time.sleep(0.05)
    assert list((tmp_path / "Screenshots").glob("snap *.png"))  # the polaroid copy, next to the screenshot

    # A burst of screenshots: a few wait their turn, the rest are let go.
    for _ in range(6):
        plugin_hooks.screenshot_taken(api, _shot(tmp_path))
    assert len(snap._queue) == 3
    snap.clear()
    assert not snap.busy


def test_snap_reads_nothing_but_the_screenshot(added):
    added("snap")
    import sys

    module = sys.modules["sprigcast_plugin_snap"]
    assert not any(name.startswith("snap.") for name in NativeApi(FakeHost(_collection())).all_actions())
    assert set(module.SnapSource.defaults) == {"frame", "seconds", "flash", "save_polaroid"}


def test_apps_can_list_plugins_and_hear_their_events(added):
    from sprigcast.control.native import NativeSession, event_group, send_event

    added("hide_it")
    added("barrel_roll")
    api = NativeApi(FakeHost(_collection()))
    listed = {p["plugin"]: p for p in api.run("plugins.list", {})["plugins"]}
    assert listed["barrel_roll"]["name"] == "Barrel Roll" and listed["barrel_roll"]["actions"] == ["roll.spin"]
    assert "hide.pixelate" in listed["hide_it"]["actions"]
    sent = []
    api.plugin_event = lambda name, data: sent.append((name, data))
    send_event(api, "confetti.thrown", {"amount": 3})
    assert sent == [("confetti.thrown", {"amount": 3})] and event_group("confetti.thrown") == "plugin"
    session = NativeSession(api)
    session.welcomed, session.events = True, {"plugin"}
    assert '"event": "confetti.thrown"' in session.event("confetti.thrown", {"amount": 3})


def test_snap_gets_a_sprig_of_its_own_on_top_of_both_shapes():
    col = _collection()
    col.sources.append(SourceConfig(id="snap-1", kind="snap", name="Snap"))
    bloom = col.scene("chat-h")
    part = col.own_sprig(bloom, "snap-1", (800, 1000), "Snap")
    shapes = col.shapes(part)
    assert [s.canvas for s in shapes] == ["horizontal", "vertical"]
    for shape in shapes:
        canvas = col.canvas(shape.canvas)
        item = shape.items[0]
        w, h = 800 * item.scale, 1000 * item.scale
        assert min(canvas.width - w, canvas.height - h) == pytest.approx(0)  # fills the canvas one way
        assert item.x == pytest.approx((canvas.width - w) / 2)
    for scene_id in ("chat-h", "chat-v"):
        top = col.source(col.scene(scene_id).items[-1].source)
        assert col.part_of(top) is part  # on top in both shapes


def test_snap_frames_hold_a_big_picture(added):
    added("snap")
    snap = __import__("sys").modules["sprigcast_plugin_snap"]
    picture = np.full((720, 1280, 4), 255, dtype=np.uint8)
    for frame in ("vintage", "white"):
        card, window = snap.compose(picture, frame)
        assert window.width() >= 999 and card.width() > window.width() and card.height() > window.height()
    assert snap.snap_name(__import__("pathlib").Path("x")).name.startswith("snap ")


# GPU ----------------------------------------------------------------------------------------------


@pytest.fixture
def compositor():
    pytest.importorskip("moderngl")
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    yield comp
    comp.release()


def _draw(comp, picture, filters):
    from sprigcast.core.scene import Canvas, Scene, SceneItem

    h, w = picture.shape[:2]
    comp.upload("src", picture)
    fx = video_effects(filters)
    comp.effects = {"src": fx} if fx.active else {}
    comp.render(Canvas(id="f", width=w, height=h), Scene(id="s", name="s", canvas="f",
                                                         items=[SceneItem(source="src")]), {"src": (w, h)})
    return np.frombuffer(comp.read("f"), dtype=np.uint8).reshape(h, w, 4)


def test_gpu_pixelate_and_blackout(added, compositor):
    added("hide_it")
    picture = np.zeros((16, 16, 4), dtype=np.uint8)
    picture[..., 0] = np.arange(16, dtype=np.uint8)[None, :] * 16
    picture[..., 3] = 255
    out = _draw(compositor, picture, [Filter(id="p", name="x", kind="pixelate", settings={"size": 8})])
    assert len(set(out[2, 0:8, 0])) == 1 and out[2, 0, 0] != out[2, 8, 0]  # blocks of one colour
    out = _draw(compositor, picture, [Filter(id="b", name="x", kind="blackout", settings={"amount": 100})])
    assert not out[..., :3].any() and (out[..., 3] == 255).all()


def test_gpu_barrel_roll_lands_where_it_began(added, compositor):
    added("barrel_roll")
    quad = [(0, 0, 0, 0), (10, 0, 1, 0), (0, 4, 0, 1), (10, 4, 1, 1)]

    def spun():
        return np.array(compositor._spun("a", quad, (1.0, 2.0, 7)))

    compositor.time = 100.0
    assert spun() == pytest.approx(np.array(quad))
    compositor.time = 101.0  # halfway through one turn: upside down
    half = spun()
    assert half[0, :2] == pytest.approx((10, 4)) and half[3, :2] == pytest.approx((0, 0), abs=1e-9)
    compositor.time = 105.0
    assert spun() == pytest.approx(np.array(quad))


def test_gpu_grabs_one_source_with_its_filters(added, compositor):
    added("hide_it")
    picture = np.full((8, 12, 4), (30, 200, 90, 255), dtype=np.uint8)
    compositor.upload("cam", picture)
    compositor.effects = {}
    out = compositor.render_source("cam", {"cam": (12, 8)}, "horizontal")
    assert out.shape == (8, 12, 4) and tuple(out[4, 6]) == (30, 200, 90, 255)
    compositor.effects = {"cam": video_effects([Filter(id="b", name="x", kind="blackout", settings={"amount": 100})])}
    assert not compositor.render_source("cam", {"cam": (12, 8)}, "horizontal")[..., :3].any()
    assert compositor.render_source("nothing", {}, "horizontal") is None
