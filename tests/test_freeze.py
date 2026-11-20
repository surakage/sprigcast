"""Freeze (store plugin): a sprig or source holds still on one frame for a while, and Snap's "Freeze the screen
while it prints": the screenshot is exactly the frame that holds on stream."""

import time

import numpy as np
import pytest

from sprigcast import plugin_hooks
from sprigcast.control.native import FEATURES, ApiError, NativeApi
from sprigcast.core.filters import freeze_of, video_effects
from sprigcast.core.scene import Canvas, Filter, Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.core.source import create_source

pytest.importorskip("PySide6.QtWidgets")

from test_control_api import FakeHost, _collection, _wait  # noqa: E402
from test_store_effects import Host, _shot, added, app  # noqa: E402,F401


def test_freeze_is_a_filter_without_the_plugin():
    """User, 2026-11-20: Freeze "should be in filters". It's in every Filters list, and switching it on (by hand,
    filter.set or filter.toggle) freezes for its time; toggled after the time ran out, it freezes again."""
    from sprigcast.core.filters import filter_kinds, switch

    assert "freeze" in [k.kind for k in filter_kinds("video")]
    flt = Filter(id="f", name="Freeze", kind="freeze", enabled=False, settings={"seconds": 10.0})
    assert freeze_of([flt]) is None and not video_effects([flt]).active
    switch(flt, True)
    run, until = freeze_of([flt])
    assert run == 1 and time.time() + 9 < until < time.time() + 11
    switch(flt, True)  # already frozen: same picture
    assert freeze_of([flt])[0] == 1
    flt.settings["until"] = time.time() - 1  # ran out
    switch(flt, True)
    assert freeze_of([flt])[0] == 2
    flt.settings = {"seconds": 0}
    flt.enabled = False
    switch(flt, True)
    assert freeze_of([flt])[1] == 0  # 0 s: until it's switched off

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                          sources=[SourceConfig(id="cam", kind="color", name="Cam", filters=[
                              Filter(id="fz", name="Freeze", kind="freeze", enabled=False, settings={"seconds": 0})])],
                          scenes=[Scene(id="s", name="Main", canvas="horizontal", items=[SceneItem(source="cam")])],
                          active_scenes={"horizontal": "s"})
    api = NativeApi(FakeHost(col))
    api.run("filter.toggle", {"source": "Cam", "filter": "Freeze"})
    assert freeze_of(api.col.source("cam").filters) == (1, 0)
    api.run("filter.toggle", {"source": "Cam", "filter": "Freeze"})
    assert freeze_of(api.col.source("cam").filters) is None
    api.run("filter.set", {"source": "Cam", "filter": "freeze", "on": True, "settings": {"seconds": 3}})
    assert freeze_of(api.col.source("cam").filters)[0] == 2


def test_freeze_holds_a_sprig_everywhere_its_shown(added):
    added("freeze")
    api = NativeApi(FakeHost(_collection()))
    assert "effects.freeze" in FEATURES and "freeze.start" in api.all_actions()
    before = time.time()
    out = api.run("freeze.start", {"sprig": "Frame", "seconds": 10})
    assert sorted(out["frozen"]) == ["Frame", "Frame"] and out["seconds"] == 10.0
    for sid in ("frame-h", "frame-v"):
        run, until = freeze_of(api.col.source(sid).filters)
        assert run == 1 and before + 9 < until < time.time() + 11
    assert not video_effects(api.col.source("frame-h").filters).active  # nothing for the shader to do

    # Again while frozen: the same picture (run), a new time.
    api.run("freeze.start", {"sprig": "Frame", "seconds": 30})
    run, until = freeze_of(api.col.source("frame-h").filters)
    assert run == 1 and until > time.time() + 25
    assert api.run("freeze.stop", {"sprig": "Frame"})["was"] == ["Frame", "Frame"]
    assert freeze_of(api.col.source("frame-h").filters) is None

    # Frozen again later: a new picture. Toggled: until toggled back (no end).
    api.run("freeze.toggle", {"source": "Cam"})
    assert freeze_of(api.col.source("cam").filters) == (1, 0)
    api.run("freeze.toggle", {"source": "Cam"})
    assert freeze_of(api.col.source("cam").filters) is None
    api.run("freeze.start", {"source": "Cam"})  # its usual time
    run, until = freeze_of(api.col.source("cam").filters)
    assert run == 2 and until == pytest.approx(time.time() + 5, abs=1)

    for bad in ({"sprig": "Frame", "seconds": "lots"}, {"sprig": "Frame", "seconds": 601}, {}):
        with pytest.raises(ApiError):
            api.run("freeze.start", bad)


def test_a_freeze_ends_by_itself_and_says_so(added, app):
    added("freeze")
    api = NativeApi(FakeHost(_collection()))
    api.changes()
    api.run("freeze.start", {"source": "Cam", "seconds": 0.05})
    api.changes()
    cam = api.col.source("cam")
    _wait(app, lambda: not plugin_hooks.find_filter(cam, "freeze").enabled, 2.0)
    assert ("effect.ended", {"source": "Cam", "sourceId": "cam", "effect": "freeze.start", "reason": "ended"}) \
        in api.changes()
    assert plugin_hooks.find_filter(cam, "freeze").settings["until"] == 0  # switched on by hand later, it holds


def test_a_freeze_left_on_when_sprigcast_closed_has_run_out(added):
    added("freeze")
    stale = Filter(id="f", name="Freeze", kind="freeze", settings={"run": 3, "until": time.time() - 60})
    assert freeze_of([stale]) == (3, stale.settings["until"])  # the engine sees its end has passed
    from sprigcast.render.engine import Engine

    col = _collection()
    col.source("cam").filters.append(stale)
    engine = Engine(col)
    engine._freeze_filters = {"cam": (("filter", 3), stale.settings["until"])}
    engine._ticks = 5
    assert engine._freezes(object()) == {}


def test_freeze_hotkeys_and_right_click(added):
    from sprigcast.app import hotkeys

    added("freeze")
    col = _collection()
    actions = dict(hotkeys.actions(col))
    assert actions["plugin:freeze:sprig:p-frame"] == "Freeze Frame"
    api = NativeApi(FakeHost(col))
    assert plugin_hooks.run_hotkey(api, "plugin:freeze:sprig:p-frame")
    assert freeze_of(col.source("frame-h").filters)[0] == 1
    assert "plugin:freeze:source:cam" not in actions  # a camera's keys are per item now


def test_right_click_and_its_hotkey_freeze_only_that_item(added):
    """User 2026-11-20: Freeze holds the one you froze, like Pixelate; the same camera elsewhere keeps moving."""
    from sprigcast.app import hotkeys
    from sprigcast.plugin_hooks import Place

    added("freeze")
    col = _collection()
    col.source("cam").kind = "camera"
    api = NativeApi(FakeHost(col))
    chat, frame = col.scene("chat-h"), col.scene("p-frame")
    place = Place.of(col, chat, "cam")
    entries = {}
    plugin_hooks.fill_item_menu(api, col.source("cam"), lambda label, fn: entries.setdefault(label, fn), place)
    entries["Freeze for 5 s"]()
    mine = next(i for i in chat.items if i.source == "cam")
    run, until = freeze_of(mine.effects)
    assert run == 1 and until > time.time()
    assert freeze_of(col.source("cam").filters) is None  # not the camera everywhere
    assert freeze_of(next(i for i in frame.items if i.source == "cam").effects) is None  # nor the one in the sprig
    entries.clear()
    plugin_hooks.fill_item_menu(api, col.source("cam"), lambda label, fn: entries.setdefault(label, fn), place)
    entries["Unfreeze"]()
    assert freeze_of(mine.effects) is None
    # A sprig's placement freezes the same way, and sounds get no entry.
    entries.clear()
    plugin_hooks.fill_item_menu(api, col.source("frame-h"), lambda label, fn: entries.setdefault(label, fn),
                                Place.of(col, chat, "frame-h"))
    assert "Freeze for 5 s" in entries
    # Its own hotkey, per item.
    actions = dict(hotkeys.actions(col))
    assert actions["plugin:freeze:item:chat-h/cam"] == "Freeze Cam in Chatting"
    assert actions["plugin:freeze:item:p-frame/cam"] == "Freeze Cam in Frame"
    assert plugin_hooks.run_hotkey(api, "plugin:freeze:item:chat-h/cam")
    assert freeze_of(mine.effects)[0] == 2 and freeze_of(next(i for i in frame.items if i.source == "cam").effects) is None


def test_apps_freeze_one_item_with_in_and_it_ends_by_itself(added, app):
    added("freeze")
    api = NativeApi(FakeHost(_collection()))
    frame = api.col.scene("p-frame")
    inner = next(i for i in frame.items if i.source == "cam")
    out = api.run("freeze.start", {"source": "Cam", "in": "Frame", "seconds": 0.05})
    assert out["frozen"] == ["Cam in Frame"] and freeze_of(inner.effects)[0] == 1
    assert freeze_of(api.col.source("cam").filters) is None
    _wait(app, lambda: freeze_of(inner.effects) is None, 2.0)  # let go when its time is up
    api.run("freeze.toggle", {"sprig": "Frame", "in": "Chatting"})  # that sprig placed in the bloom
    placed = next(i for i in api.col.scene("chat-h").items if i.source == "frame-h")
    assert freeze_of(placed.effects) == (1, 0) and freeze_of(api.col.source("frame-h").filters) is None
    api.run("freeze.stop", {"sprig": "Frame", "in": "Chatting"})
    assert freeze_of(placed.effects) is None
    with pytest.raises(ApiError):
        api.run("freeze.start", {"source": "Alert", "in": "Frame"})  # not in there
    api.run("freeze.start", {"source": "Cam", "seconds": 0})  # without "in": everywhere, as before
    assert freeze_of(api.col.source("cam").filters) == (1, 0)


def test_plugins_list_shows_freeze(added):
    added("freeze")
    api = NativeApi(FakeHost(_collection()))
    listed = {p["plugin"]: p for p in api.run("plugins.list", {})["plugins"]}
    assert listed["freeze"]["name"] == "Freeze" and listed["freeze"]["enabled"]
    assert sorted(listed["freeze"]["actions"]) == ["freeze.start", "freeze.stop", "freeze.toggle"]


# Drawing ---------------------------------------------------------------------------------------------

moderngl = pytest.importorskip("moderngl")


@pytest.fixture(scope="module")
def compositor():
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    yield comp
    comp.release()


class Blinker:
    """A source whose colour is whatever it's told: red, then green..."""

    def __init__(self, source_id):
        self.id, self.kind, self.settings = source_id, "color", {}
        self.asleep, self.channel_order, self.size, self.colour = False, "RGBA", (8, 8), (255, 0, 0)
        self.in_screenshots, self.audio_chain = True, None

    def video_frame(self):
        frame = np.zeros((8, 8, 4), dtype=np.uint8)
        frame[..., :3], frame[..., 3] = self.colour, 255
        return frame

    def __getattr__(self, name):
        return lambda *a, **k: None


def _world():
    return SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36)],
        sources=[
            SourceConfig(id="game", kind="color", name="Game", settings={"color": "#ff0000ff", "width": 8, "height": 8}),
            SourceConfig(id="cam", kind="color", name="Cam", settings={"color": "#ff0000ff", "width": 8, "height": 8}),
            SourceConfig(id="main", kind="scene", name="Gameplay", settings={"scene": "p-main"}),
            SourceConfig(id="other", kind="scene", name="Gameplay", settings={"scene": "p-main"}),
        ],
        scenes=[
            Scene(id="show", name="Gaming", canvas="h", items=[SceneItem(source="main"), SceneItem(source="cam", x=40)]),
            Scene(id="show2", name="Other", canvas="h", items=[SceneItem(source="other")]),
            Scene(id="p-main", name="Gameplay", canvas="h", kind="part", items=[SceneItem(source="game")]),
        ],
        active_scenes={"h": "show"},
    )


def _engine(compositor, col):
    from sprigcast.render.engine import Engine

    engine = Engine(col)
    engine._sync_sources(compositor)
    for sid in ("game", "cam"):
        engine._sources[sid].stop()
        engine._sources[sid] = Blinker(sid)
    return engine


def _pixel(engine, compositor, x, y):
    img = np.frombuffer(compositor.read("h"), dtype=np.uint8).reshape(36, 64, 4)
    return tuple(int(v) for v in img[y, x][:3])


def test_a_held_sprig_shows_its_frozen_picture_then_live_again(compositor):
    engine = _engine(compositor, _world())
    engine._tick(compositor, 1.0)
    assert _pixel(engine, compositor, 4, 4)[0] > 200  # the game (red) inside the sprig
    token = engine.hold(["main", "cam"], 30)
    engine._tick(compositor, 1.1)  # caught: still red
    engine._sources["game"].colour = engine._sources["cam"].colour = (0, 255, 0)
    engine._tick(compositor, 1.2)
    assert _pixel(engine, compositor, 4, 4)[0] > 200 and _pixel(engine, compositor, 44, 4)[0] > 200  # held
    assert ("main", "h") in compositor.held
    # The sprig itself still draws live for anything else showing it (the other placement isn't frozen).
    assert ("other", "h") not in compositor.held
    engine.let_go(token)
    engine._tick(compositor, 1.3)
    assert _pixel(engine, compositor, 4, 4)[1] > 200 and _pixel(engine, compositor, 44, 4)[1] > 200  # live
    assert not compositor.held and not engine._caught


def test_a_hold_ends_by_itself(compositor):
    engine = _engine(compositor, _world())
    engine._tick(compositor, 1.0)
    engine.hold(["cam"], 0.05)
    engine._tick(compositor, 1.1)
    engine._sources["cam"].colour = (0, 255, 0)
    engine._tick(compositor, 1.2)
    assert _pixel(engine, compositor, 44, 4)[0] > 200
    time.sleep(0.08)
    engine._tick(compositor, 1.3)
    assert _pixel(engine, compositor, 44, 4)[1] > 200 and not engine._holds


def test_a_freeze_filter_holds_until_it_goes(compositor, added):
    added("freeze")
    col = _world()
    col.source("cam").filters.append(Filter(id="fz", kind="freeze", name="Freeze", settings={"run": 1, "until": 0}))
    engine = _engine(compositor, col)
    engine._tick(compositor, 1.0)
    engine._sources["cam"].colour = (0, 255, 0)
    engine._tick(compositor, 1.1)
    assert _pixel(engine, compositor, 44, 4)[0] > 200
    col.source("cam").filters[0].enabled = False
    engine._sync_filters(compositor, {s.id: s for s in col.sources})
    engine._tick(compositor, 1.2)
    assert _pixel(engine, compositor, 44, 4)[1] > 200


def test_one_item_freezes_while_its_source_stays_live_elsewhere(compositor, added):
    """A Freeze among one item's effects: that item holds its picture (a source's or a sprig's) and the source
    goes on live for every other item that shows it."""
    added("freeze")
    col = _world()
    col.scene("p-main").items.append(SceneItem(source="cam", x=20))  # the same camera, inside the sprig too
    main, cam = col.scene("show").items
    cam.effects.append(Filter(id="fz", kind="freeze", name="Freeze", settings={"run": 1, "until": 0}))
    engine = _engine(compositor, col)
    wanted = {s.id: s for s in col.sources}
    main, cam = engine._collection.scene("show").items  # (the engine's own copy of the layout)
    engine._sync_filters(compositor, wanted)
    engine._tick(compositor, 1.0)
    engine._sources["game"].colour = engine._sources["cam"].colour = (0, 255, 0)
    engine._tick(compositor, 1.1)
    assert _pixel(engine, compositor, 44, 4)[0] > 200  # the item holds
    assert _pixel(engine, compositor, 4, 4)[1] > 200 and _pixel(engine, compositor, 24, 4)[1] > 200  # the rest live
    assert set(compositor.item_held) == {"show/cam"} and not engine._frozen
    main.effects.append(Filter(id="fz2", kind="freeze", name="Freeze", settings={"run": 1, "until": 0}))
    engine._sync_filters(compositor, wanted)
    engine._tick(compositor, 1.2)  # the sprig placement is caught, green
    engine._sources["game"].colour = engine._sources["cam"].colour = (0, 0, 255)
    cam.effects[0].settings["run"] = 2  # the camera's item frozen again: a new picture is caught
    engine._sync_filters(compositor, wanted)
    engine._tick(compositor, 1.3)
    assert _pixel(engine, compositor, 4, 4)[1] > 200 and _pixel(engine, compositor, 24, 4)[1] > 200
    assert _pixel(engine, compositor, 44, 4)[2] > 200
    cam.effects[0].enabled = main.effects[0].enabled = False
    engine._sync_filters(compositor, wanted)
    engine._tick(compositor, 1.4)
    assert _pixel(engine, compositor, 4, 4)[2] > 200 and not compositor.item_held


def test_an_items_freeze_ends_by_itself(compositor, added):
    added("freeze")
    col = _world()
    col.scene("show").items[1].effects.append(
        Filter(id="fz", kind="freeze", name="Freeze", settings={"run": 1, "until": time.time() + 0.05}))
    engine = _engine(compositor, col)
    engine._sync_filters(compositor, {s.id: s for s in col.sources})
    engine._tick(compositor, 1.0)
    engine._sources["cam"].colour = (0, 255, 0)
    engine._tick(compositor, 1.1)
    assert _pixel(engine, compositor, 44, 4)[0] > 200
    time.sleep(0.08)
    engine._tick(compositor, 1.2)
    assert _pixel(engine, compositor, 44, 4)[1] > 200 and not compositor.item_held


def test_the_screenshot_is_the_frozen_frame(compositor):
    """Snap's freeze: a tick already being drawn when the hold was asked for doesn't count, for the hold or
    the screenshot, so the screenshot is exactly the picture that then stays on stream."""
    import threading

    from sprigcast.app import screenshot

    engine = _engine(compositor, _world())
    engine._tick(compositor, 1.0)
    engine.hold(["main", "cam"], 30)
    shots, images = [], {}
    worker = threading.Thread(target=lambda: shots.extend(screenshot.take(engine, ["h"], None, None, False, images)))
    worker.start()
    for n in range(20):
        if n == 1:
            engine._sources["game"].colour = engine._sources["cam"].colour = (0, 255, 0)  # after the catch
        if not worker.is_alive():
            break
        engine._tick(compositor, 2.0 + n / 30)
        worker.join(0.02)
    worker.join(2)
    image = images["h"]
    assert image.pixelColor(4, 4).red() > 200 and image.pixelColor(44, 4).red() > 200
    assert _pixel(engine, compositor, 4, 4)[0] > 200  # and the stream holds the same


# Snap ------------------------------------------------------------------------------------------------


class HoldHost(Host):
    def __init__(self, collection, folder=None):
        super().__init__(collection, folder)
        self.holds = []  # (ids, seconds, token)
        self.gone = []

    def hold(self, ids, seconds, token=None):
        token = token or object()
        self.holds.append((list(ids), seconds, token))
        return token

    def let_go(self, token):
        self.gone.append(token)


def _snap_world():
    col = _collection()
    col.sources.append(SourceConfig(id="snap-1", kind="snap", name="Snap"))
    col.own_sprig(col.scene("chat-h"), "snap-1", (800, 1000), "Snap")
    return col


def test_snap_freezes_the_bloom_but_not_itself(added, tmp_path):
    added("snap")
    snap = __import__("sys").modules["sprigcast_plugin_snap"]
    col = _snap_world()
    host = HoldHost(col, tmp_path)
    instance = host.instances["snap-1"] = create_source("snap", "snap-1", {"seconds": 1.0})
    api = NativeApi(host)
    afters = plugin_hooks.screenshot_starting(api, ["horizontal", "vertical"])
    ids, seconds, token = host.holds[0]
    assert set(ids) == {"cam", "frame-h", "alert", "frame-v"} and seconds == snap.FREEZE_FIRST_S
    plugin_hooks.screenshot_taken(api, _shot(tmp_path))
    plugin_hooks.screenshot_done(afters, _shot(tmp_path))
    assert host.holds[-1][2] is token and host.holds[-1][1] > 4  # same freeze, held until it leaves
    assert not host.gone
    job = None
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (job is None or job.card is None):
        instance.video_frame()
        job = instance._job
        time.sleep(0.01)
    # The polaroid starts to leave (a little past it: Windows' clock moves in 16 ms steps, so with no time gone by
    # the subtraction could come back a hair short of the mark).
    job.started -= snap.GROW[1] + instance._hold() + 0.05
    instance.video_frame()
    assert host.gone == [token]  # the screen moves again
    instance.clear()


def test_snap_freeze_leaves_death_count_moving(added, tmp_path):
    added("snap")
    added("death_count")
    col = _snap_world()
    col.sources.append(SourceConfig(id="deaths", kind="death_count", name="Death count"))
    col.own_sprig(col.scene("chat-h"), "deaths", (800, 1000), "Death count")
    host = HoldHost(col, tmp_path)
    host.instances["snap-1"] = create_source("snap", "snap-1", {"seconds": 1.0})
    afters = plugin_hooks.screenshot_starting(NativeApi(host), ["horizontal"])
    ids = host.holds[0][0]
    assert set(ids) == {"cam", "frame-h", "alert"}  # not Death count's sprig: a death rises at once
    plugin_hooks.screenshot_done(afters, [])


def test_snap_freeze_can_be_turned_off_and_a_failed_screenshot_lets_go(added, tmp_path):
    added("snap")
    col = _snap_world()
    host = HoldHost(col, tmp_path)
    host.instances["snap-1"] = create_source("snap", "snap-1", {"seconds": 1.0})
    api = NativeApi(host)
    afters = plugin_hooks.screenshot_starting(api, ["horizontal"])
    token = host.holds[0][2]
    plugin_hooks.screenshot_done(afters, [])  # nothing was drawing
    assert host.gone == [token]
    col.source("snap-1").settings["freeze_screen"] = False
    host.holds.clear()
    assert plugin_hooks.screenshot_starting(api, ["horizontal"]) == [] and not host.holds


def test_snap_freeze_goes_inside_a_sprig_that_holds_snap(added):
    snap = __import__("sys").modules.get("sprigcast_plugin_snap")
    if snap is None:
        added("snap")
        snap = __import__("sys").modules["sprigcast_plugin_snap"]
    col = _collection()
    col.sources.append(SourceConfig(id="snap-1", kind="snap", name="Snap"))
    col.scene("p-frame").items.append(SceneItem(source="snap-1"))  # Snap moved into the Frame sprig
    items = snap.still_items(col, col.scene("chat-h"), {"snap-1"}, "horizontal")
    assert "frame-h" not in items and set(items) == {"cam", "alert"}  # Frame's insides instead of Frame


def test_a_source_that_only_sends_changes_shows_its_newest_after_a_freeze(compositor):
    """A text or Death count sends a picture only when it changes. One that changed while frozen must show the
    change once it's let go, not stay on the frozen picture."""
    engine = _engine(compositor, _world())
    cam = engine._sources["cam"]
    engine._tick(compositor, 1.0)
    token = engine.hold(["cam"], 30)
    engine._tick(compositor, 1.1)
    cam.colour = (0, 255, 0)
    engine._tick(compositor, 1.2)  # it sends green once...
    cam.video_frame = lambda: None  # ...then nothing more
    engine._tick(compositor, 1.3)
    assert _pixel(engine, compositor, 44, 4)[0] > 200
    engine.let_go(token)
    engine._tick(compositor, 1.4)
    assert _pixel(engine, compositor, 44, 4)[1] > 200 and not engine._missed


def test_an_items_own_blackout_draws_through_the_engine(compositor):
    """The engine hands an item's own effects to the drawing under its item key (user 2026-11-20)."""
    from sprigcast import plugin_store
    from sprigcast.core.scene import item_key

    hide = next(p for p in plugin_store.catalog() if p.id == "hide_it")
    col = _world()
    col.scene("show").items[1].effects = [Filter(id="b", kind="blackout", name="Blackout", settings={"amount": 100})]
    import tempfile
    from pathlib import Path

    folder = Path(tempfile.mkdtemp()) / "plugins"
    assert plugin_store.install(folder, hide) is None
    try:
        engine = _engine(compositor, col)
        engine._sync_filters(compositor, {s.id: s for s in col.sources})
        assert item_key("show", "cam") in compositor.effects and "cam" not in compositor.effects
        engine._tick(compositor, 1.0)
        assert _pixel(engine, compositor, 44, 4) == (0, 0, 0) and _pixel(engine, compositor, 4, 4)[0] > 200
    finally:
        plugin_store.remove(folder, "hide_it")
