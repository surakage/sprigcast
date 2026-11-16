"""Store plugins that change how things look or add a show: Hide it, Barrel Roll, Snap and Death count."""

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


def _next_frame(source, timeout=3.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = source.video_frame()
        if frame is not None:
            return frame
        time.sleep(0.01)
    return None


def test_a_screenshot_prints_as_a_polaroid(added, tmp_path):
    import sys
    import time

    added("snap")
    to_array = sys.modules["sprigcast_plugin_snap"].to_array
    col = _collection()
    # save_polaroid: a setting older Snaps had (a framed copy next to the screenshot); it's ignored now.
    col.sources.append(SourceConfig(id="snap-1", kind="snap", name="Snap", settings={"save_polaroid": True}))
    host = Host(col, tmp_path)
    snap = host.instances["snap-1"] = create_source("snap", "snap-1", {"seconds": 1.0})
    api = NativeApi(host)
    assert snap.size == (800, 1000)
    first = snap.video_frame()
    assert first.shape == (1000, 800, 4) and not first[..., 3].any()  # see-through until a screenshot
    assert snap.video_frame() is None

    plugin_hooks.screenshot_taken(api, _shot(tmp_path))
    assert snap.video_frame() is None  # the card is made off the render thread first
    frame = _next_frame(snap)
    assert frame.shape == (1000, 800, 4) and snap.busy
    job = snap._job
    assert to_array(snap.paint(job, 0.5))[..., 3].any()  # the camera
    held = to_array(snap.paint(job, 3.6))  # developed, not yet leaving
    assert held[500, 400, 3] == 255 and held[500, 400, 0] > 180  # the red screenshot in the middle
    job.started -= 100
    assert not snap.video_frame()[..., 3].any() and not snap.busy  # gone again

    # The polaroid is only for show: the one file is the screenshot SprigCast saved, nothing framed.
    time.sleep(0.3)
    assert [p.name for p in (tmp_path / "Screenshots").iterdir()] == ["screenshot horizontal 2026-11-14 20-12-43.png"]
    assert type(snap).in_screenshots is False  # and a screenshot taken while it's up leaves it out

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
    assert set(module.SnapSource.defaults) == {"seconds", "flash"}


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
    card, window = snap.compose(picture)
    assert window.width() >= 999 and card.width() > window.width() and card.height() > window.height()


@pytest.mark.parametrize("shape", [(1080, 1920), (1920, 1080), (900, 900), (300, 2400), (2400, 300)])
def test_the_polaroid_shows_the_whole_screenshot(added, shape):
    """The user's report: the polaroid cut off the screenshot's edges (a 16:9 shot cropped to a 4:3-ish window).
    The window now has the screenshot's own shape, so every edge of it is in the print."""
    added("snap")
    snap = __import__("sys").modules["sprigcast_plugin_snap"]
    h, w = shape
    picture = np.zeros((h, w, 4), dtype=np.uint8)
    picture[..., 3] = 255
    picture[:, : w // 20, 0] = 255  # a red stripe down the left edge
    picture[:, -(w // 20):, 1] = 255  # a green one down the right
    picture[: h // 10, :, 2] = 255  # a blue one along the top
    card, win = snap.compose(picture, 600)
    assert win.width() / win.height() == pytest.approx(w / h, rel=0.02)
    image = snap.to_array(card)
    x0, y0, x1, y1 = round(win.left()), round(win.top()), round(win.right()) - 1, round(win.bottom()) - 1
    mid_y = (y0 + y1) // 2
    assert image[mid_y, x0 + 1, 0] > 200 and image[mid_y, x1 - 1, 1] > 200  # both side edges made it
    assert image[y0 + 1, (x0 + x1) // 2, 2] > 200  # and the top


@pytest.mark.parametrize("shape", [(1080, 1920), (1920, 1080), (900, 900), (300, 2400), (2400, 300)])
def test_the_polaroid_stays_inside_the_canvas(added, shape):
    """Nothing Snap draws, at any moment of the show (camera, flash, print, held up tilted, sliding away),
    reaches the edge of its box (which fills the canvas): it's never cut off."""
    added("snap")
    module = __import__("sys").modules["sprigcast_plugin_snap"]
    source = create_source("snap", "snap-1", {"seconds": 1.0})
    picture = np.full((*shape, 4), 200, dtype=np.uint8)
    job = module._Job(picture)
    source._picture_in(job)
    t = 0.0
    while t < source._length():
        alpha = module.to_array(source.paint(job, t))[..., 3]
        edge = max(alpha[:2].max(), alpha[-2:].max(), alpha[:, :2].max(), alpha[:, -2:].max())
        assert edge == 0, f"cut off at {t:.2f} s"
        t += 0.1


# Death count -------------------------------------------------------------------------------------


def _dc_setup(added, tmp_path, count=0):
    added("death_count")
    col = _collection()
    col.sources.append(SourceConfig(id="dc-1", kind="death_count", name="Death count", settings={"count": count}))
    host = Host(col, tmp_path)
    instance = host.instances["dc-1"] = create_source("death_count", "dc-1", {"count": count, "seconds": 1.0})
    api = NativeApi(host)
    return col, host, instance, api


def test_its_only_there_once_added():
    assert "death_count" not in known_kinds()
    assert "deaths.add" not in NativeApi(Host(_collection())).all_actions()


def test_apps_change_the_count_and_it_is_kept(added, tmp_path):
    col, host, _dc, api = _dc_setup(added, tmp_path, 4)
    assert api.run("deaths.get", {}) == {"count": 4}
    assert api.run("deaths.add", {}) == {"count": 5}
    assert col.source("dc-1").settings["count"] == 5 and "sources" in host.commits  # saved with the blooms
    assert api.run("deaths.add", {"amount": "3"}) == {"count": 8}  # web links send text
    assert api.run("deaths.remove", {}) == {"count": 7}
    assert api.run("deaths.remove", {"amount": 50}) == {"count": 0}  # never below 0
    assert api.run("deaths.set", {"count": 12}) == {"count": 12}
    assert api.run("deaths.show", {}) == {"count": 12}
    assert api.run("deaths.reset", {"source": "Death count"}) == {"count": 0}
    with pytest.raises(ApiError):
        api.run("deaths.set", {"count": -1})
    with pytest.raises(ApiError):
        api.run("deaths.add", {"amount": "lots"})
    with pytest.raises(ApiError):
        api.run("deaths.add", {"source": "Cam"})  # not a death count
    # A restart: a new source made from the saved settings starts at the saved count.
    assert create_source("death_count", "dc-1", col.source("dc-1").settings)._count == 0


def test_no_death_count_added_yet(added):
    added("death_count")
    with pytest.raises(ApiError) as err:
        NativeApi(Host(_collection())).run("deaths.add", {})
    assert err.value.code == "not_found"


def test_the_gravestone_rises_flips_the_number_and_sinks(added, tmp_path):
    _col, _host, dc, api = _dc_setup(added, tmp_path, 6)
    to_array = __import__("sys").modules["sprigcast_plugin_death_count"].to_array
    first = dc.video_frame()
    assert first.shape == (1000, 800, 4) and not first[..., 3].any()  # see-through until it changes
    assert dc.video_frame() is None

    api.run("deaths.add", {})
    assert dc.video_frame() is not None and dc.busy
    job = dc._job
    assert (job.old, job.new) == (6, 7)
    low = to_array(dc.paint(job, 0.05))
    up = to_array(dc.paint(job, 2.0))
    assert up[..., 3].sum() > low[..., 3].sum()  # the stone is out of the ground
    assert up[600, 400, 3] == 255  # the stone's face, middle of the box
    assert not up[:300, :, 3].any()  # it only takes the lower part of the box
    assert dc._showing(job, 1.0) == 6 and dc._showing(job, 2.0) == 7

    # Another death while it's up: the number changes again and it stays up longer.
    sink_before = dc._sink_at(job)
    job.started -= 2.0
    api.run("deaths.add", {})
    assert dc._job is job and job.new == 8 and not dc._queue and dc._sink_at(job) > sink_before

    job.started -= 100
    assert not dc.video_frame()[..., 3].any() and not dc.busy  # back in the ground


def test_changing_the_count_in_its_settings_pops_it_up(added, tmp_path):
    _col, _host, dc, _api = _dc_setup(added, tmp_path, 2)
    dc.update({"count": 2, "seconds": 1.0})
    assert not dc.busy  # nothing changed
    dc.update({"count": 9, "seconds": 1.0})
    assert dc.busy and dc._queue[0].new == 9


def test_hotkeys_and_right_click(added, tmp_path):
    from sprigcast.app import hotkeys

    col, _host, _dc, api = _dc_setup(added, tmp_path, 1)
    actions = dict(hotkeys.actions(col))
    assert {"plugin:deaths:add", "plugin:deaths:remove", "plugin:deaths:reset", "plugin:deaths:show"} <= set(actions)
    assert plugin_hooks.run_hotkey(api, "plugin:deaths:add")
    assert col.source("dc-1").settings["count"] == 2
    entries = {}
    plugin_hooks.fill_item_menu(api, col.source("dc-1"), lambda label, fn: entries.setdefault(label, fn))
    entries["Reset to 0"]()
    assert col.source("dc-1").settings["count"] == 0
    assert not any(a.startswith("plugin:deaths:") for a, _ in hotkeys.actions(_collection()))  # none until added


def test_apps_hear_when_the_count_changes(added, tmp_path):
    _col, _host, _dc, api = _dc_setup(added, tmp_path, 0)
    sent = []
    api.plugin_event = lambda name, data: sent.append((name, data))
    api.run("deaths.add", {"amount": 2})
    assert sent == [("deaths.changed", {"count": 2})]
    listed = {p["plugin"]: p for p in api.run("plugins.list", {})["plugins"]}
    assert "deaths.add" in listed["death_count"]["actions"]


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


def test_barrel_roll_keys_only_for_placed_sprigs_and_no_crash_when_unplaced(added, caplog):
    """A sprig in no bloom got a Barrel roll key that logged a traceback each press (seen in a 60-minute soak)."""
    import logging

    from sprigcast.app import hotkeys

    added("barrel_roll")
    col = _collection()
    assert "plugin:roll:p-frame" in dict(hotkeys.actions(col))
    api = NativeApi(FakeHost(col))
    placed = {s.id for s in col.sources if s.kind == "scene" and s.id == "frame-h" or s.settings.get("scene") == "p-frame"}
    col.sources[:] = [s for s in col.sources if s.id not in placed]
    assert "plugin:roll:p-frame" not in dict(hotkeys.actions(col))  # nothing to spin: no key offered
    with caplog.at_level(logging.INFO):
        assert plugin_hooks.run_hotkey(api, "plugin:roll:p-frame") in (True, False)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_a_still_show_is_not_painted_again_and_reads_as_working(added, tmp_path):
    """Soak 2026-11-15: painting every frame on the render thread missed ticks; idle ones said "problem"."""
    from sprigcast.app.names import short_status

    _col, _host, dc, api = _dc_setup(added, tmp_path, 1)
    assert dc.channel_order == "pBGRA"  # painted pictures go up as they are, no conversion
    dc.video_frame()
    api.run("deaths.add", {})
    assert dc.video_frame() is not None
    job = dc._job
    job.started = __import__("time").monotonic() - (dc._still_from(job) + 0.1)  # up and standing still
    assert dc.video_frame() is not None  # the still frame, once
    assert dc.video_frame() is None and dc.video_frame() is None  # then nothing to paint or upload
    job.started -= 100
    assert not dc.video_frame()[..., 3].any()
    assert short_status(dc.status) == ""  # Leaves shows no problem

    import sys

    added("snap")
    snap = create_source("snap", "snap-1", {"seconds": 2.0})
    snap.video_frame()
    sys.modules["sprigcast_plugin_snap"].on_screenshot(NativeApi(Host(_collection(), tmp_path)), [])  # nothing shot
    snap.show(None)
    assert _next_frame(snap) is not None
    still = sys.modules["sprigcast_plugin_snap"].STILL_FROM
    snap._job.started = __import__("time").monotonic() - (still + 0.1)
    assert snap.video_frame() is not None and snap.video_frame() is None
    assert short_status(snap.status) == ""


def test_the_engraved_stone_is_drawn_once_per_number(added, tmp_path):
    """Text is the slowest thing QPainter draws: a standing stone reuses its engraving."""
    _col, _host, dc, api = _dc_setup(added, tmp_path, 1)
    job = __import__("sprigcast.store.death_count", fromlist=["_Job"])._Job(1, 2)
    dc.paint(job, 0.8)
    first = dc._stone_cache
    dc.paint(job, 0.9)
    assert dc._stone_cache is first  # same number: the same picture
    dc.paint(job, job.flip_at + 1.0)
    assert dc._stone_cache is not first and dc._stone_cache[0][1] == 2  # the new number, engraved once


def test_the_flip_moves_pictures_of_the_numbers_instead_of_cutting_text(added, tmp_path, monkeypatch):
    """The flip cut its numbers as text every frame: 15-24 ms a frame on a busy PC (soak 2026-11-15)."""
    from PySide6.QtGui import QPainter

    _col, _host, dc, _api = _dc_setup(added, tmp_path, 1)
    job = __import__("sprigcast.store.death_count", fromlist=["_Job"])._Job(1, 2)
    dc.paint(job, job.flip_at + 0.05)  # cuts 1 and 2 once
    texts = []
    real = QPainter.drawText
    monkeypatch.setattr(QPainter, "drawText", lambda self, *a: texts.append(a) or real(self, *a))
    for i in range(10):
        image = dc.paint(job, job.flip_at + 0.03 * i)
    assert texts == []  # every flip frame is pictures only
    assert image.pixelColor(400, 600).alpha() == 255


def test_plugin_hotkeys_explain_themselves(added):
    """A hotkey for something not in the bloom on air says so, instead of doing nothing silently."""
    added("hide_it")
    added("barrel_roll")
    col = _collection()
    col.sources.append(SourceConfig(id="fc", kind="camera", name="Facecam"))
    col.scene("brb-h").items.append(__import__("sprigcast.core.scene", fromlist=["SceneItem"]).SceneItem(source="fc"))
    api = NativeApi(FakeHost(col))
    said = []
    assert plugin_hooks.on_air(col, "frame-h") and plugin_hooks.on_air(col, "cam")  # cam is inside the Frame sprig too
    assert not plugin_hooks.on_air(col, "fc")  # only in BRB, which isn't on air
    assert plugin_hooks.run_hotkey(api, "plugin:hide:pixelate:fc", said.append)
    assert said == ["Not in this bloom: Facecam"] and not plugin_hooks.find_filter(col.source("fc"), "pixelate")
    col.active_scenes["horizontal"] = "brb-h"
    said.clear()
    assert plugin_hooks.run_hotkey(api, "plugin:hide:pixelate:fc", said.append) and said == []
    assert plugin_hooks.find_filter(col.source("fc"), "pixelate").enabled
    # The Frame sprig is placed (so its key is listed), but not in BRB: say so rather than spin unseen.
    col.active_scenes["vertical"] = "brb-v"
    assert plugin_hooks.run_hotkey(api, "plugin:roll:p-frame", said.append)
    assert said == ["Not in this bloom: Frame"] and not plugin_hooks.find_filter(col.source("frame-h"), "barrel_roll")
    said.clear()
    assert not plugin_hooks.run_hotkey(api, "plugin:gone:thing", said.append)
    assert said == ["That hotkey's plugin isn't added any more."]


def test_death_count_key_counts_off_air_but_says_so(added):
    added("death_count")
    col = _collection()
    col.sources.append(SourceConfig(id="dc", kind="death_count", name="Deaths"))
    api = NativeApi(Host(col))
    said = []
    assert plugin_hooks.run_hotkey(api, "plugin:deaths:add", said.append)
    assert col.source("dc").settings["count"] == 1 and said == ["Deaths: 1. Not in this bloom: Deaths"]


def test_hotkey_run_tells_apps_why_it_did_nothing():
    """Over the API (Streamer.bot), a hotkey that couldn't do anything answers with its note."""
    from sprigcast.app.control_server import WindowHost

    class Window:
        _last_hotkey_note = None
        streaming = object()  # live: Go live's key ends it (going live from an app is tested with the doctor)

        def _on_hotkey(self, action):
            self._last_hotkey_note = "Not in this bloom: Facecam" if action == "plugin:hide:pixelate:fc" else None

    host = Host(_collection())
    host.hotkeys = lambda: [("plugin:hide:pixelate:fc", "Pixelate Facecam"), ("toggle_stream", "Go live")]
    window_host = WindowHost.__new__(WindowHost)
    window_host.w = Window()
    host.trigger_hotkey = window_host.trigger_hotkey
    api = NativeApi(host)
    assert api.run("hotkey.run", {"hotkey": "plugin:hide:pixelate:fc"}) == {
        "hotkey": "plugin:hide:pixelate:fc", "note": "Not in this bloom: Facecam"}
    assert api.run("hotkey.run", {"hotkey": "toggle_stream"}) == {"hotkey": "toggle_stream", "note": None}


def test_apps_hear_which_plugin_to_add_and_which_are_on(added):
    """THSV StreamBridge: an effect whose plugin isn't added says which plugin; plugins.list says what's on."""
    api = NativeApi(FakeHost(_collection()))
    with pytest.raises(ApiError) as info:
        api.run("roll.spin", {"source": "Cam", "seconds": 5})
    assert info.value.code == "plugin_missing" and "Barrel Roll" in info.value.message
    with pytest.raises(ApiError) as info:
        api.run("hide.blackout", {"source": "Cam"})
    assert info.value.code == "plugin_missing" and "Hide it" in info.value.message
    with pytest.raises(ApiError) as info:
        api.run("nope.never", {})
    assert info.value.code == "unknown_action"
    listed = {p["id"]: p for p in api.run("plugins.list", {})["plugins"]}
    assert listed["barrel_roll"]["enabled"] is False and listed["barrel_roll"]["actions"] == ["roll.spin"]
    added("barrel_roll")
    listed = {p["id"]: p for p in api.run("plugins.list", {})["plugins"]}
    assert listed["barrel_roll"]["enabled"] is True and listed["barrel_roll"]["version"] >= 3
    assert {"id", "name", "version", "enabled"} <= set(listed["barrel_roll"])
    assert listed["hide_it"]["enabled"] is False


def test_effects_take_up_to_ten_minutes_and_name_what_is_missing(added):
    added("hide_it")
    added("barrel_roll")
    api = NativeApi(FakeHost(_collection()))
    for action in ("hide.blackout", "hide.pixelate", "roll.spin"):
        with pytest.raises(ApiError) as info:
            api.run(action, {"source": "Cam", "seconds": 601})
        assert info.value.code == "bad_request"
        with pytest.raises(ApiError) as info:
            api.run(action, {"source": "Nobody", "seconds": 5})
        assert info.value.code == "not_found" and "Nobody" in info.value.message
    with pytest.raises(ApiError) as info:
        api.run("roll.spin", {"sprig": "Nobody", "seconds": 5})
    assert info.value.code == "not_found" and "Nobody" in info.value.message
    assert api.run("hide.blackout", {"source": "Cam", "seconds": 600})["seconds"] == 600.0


def test_apps_hear_when_a_plugin_is_added(added):
    api = NativeApi(FakeHost(_collection()))
    api.changes()
    added("barrel_roll")
    changed = dict(api.changes())["plugins.changed"]
    assert changed["added"] == ["barrel_roll"] and "barrel_roll" in changed["plugins"]
