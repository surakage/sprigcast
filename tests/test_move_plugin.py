"""The Move store plugin: something in a bloom moves when a key, a Stream Deck button or an app says, eased,
drawn frame by frame by the engines (Python and native do the same sums)."""

import json
import math
import time

import pytest

from sprigcast import plugin_store
from sprigcast.control.native import ApiError, NativeApi
from sprigcast.core.filters import MOVE_EASINGS, ease, move_of, moved_quad, video_effects
from sprigcast.core.scene import Filter

pytest.importorskip("PySide6.QtWidgets")

from test_control_api import FakeHost, _collection  # noqa: E402


@pytest.fixture
def move_added(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    plugins = tmp_path / "plugins"
    store = next(p for p in plugin_store.catalog() if p.id == "move")
    assert plugin_store.install(plugins, store) is None
    yield
    plugin_store.remove(plugins, "move")


class Host(FakeHost):
    def source_sizes(self):
        return {"cam": (10, 10), "alert": (10, 10)}


QUAD = [(0.0, 0.0, 0, 0), (10.0, 0.0, 1, 0), (0.0, 10.0, 0, 1), (10.0, 10.0, 1, 1)]


def test_every_easing_starts_at_0_and_lands_at_1():
    for i, name in enumerate(MOVE_EASINGS):
        assert ease(0.0, i) == pytest.approx(0.0, abs=1e-9), name
        assert ease(1.0, i) == pytest.approx(1.0, abs=1e-9), name
    assert ease(0.5, 1) == 0.5 and ease(0.5, 2) < 0.5 < ease(0.5, 3)
    assert max(ease(t / 100, 6) for t in range(101)) > 1.0  # back goes a little past


def test_a_move_starts_where_the_item_was_and_ends_where_it_is():
    move = (30.0, -20.0, 0.5, math.pi / 2, 1.0, 1, 1)  # it was 30 right, 20 up, half the size, a quarter turn
    start = moved_quad(QUAD, move, 0.0)
    assert sum(p[0] for p in start) / 4 == pytest.approx(35.0) and sum(p[1] for p in start) / 4 == pytest.approx(-15.0)
    assert math.dist(start[0][:2], start[1][:2]) == pytest.approx(5.0)
    flat = [v for p in QUAD for v in p]
    assert [v for p in moved_quad(QUAD, move, 1.0) for v in p] == pytest.approx(flat)
    assert [v for p in moved_quad(QUAD, move, 5.0) for v in p] == pytest.approx(flat)
    half = moved_quad(QUAD, move, 0.5)
    assert sum(p[0] for p in half) / 4 == pytest.approx(20.0)


def test_a_move_filter_before_it_played_draws_nothing_special():
    assert move_of({}) == (0.0, 0.0, 1.0, 0.0, 0.0, 0, 0)
    assert move_of({"from": [1, 2, 0.5, 0.1], "seconds": 2, "run": 3, "easing": "bounce"}) == \
        (1.0, 2.0, 0.5, 0.1, 2.0, 3, 4)


def test_playing_puts_it_there_and_has_the_engines_draw_the_trip(move_added):
    col = _collection()
    col.source("cam").filters.append(Filter(id="f-move", kind="move", name="Slide in",
                                            settings={"x": 20.0, "y": 0.0, "size": 200, "seconds": 0.5,
                                                      "easing": "ease_out"}))
    api = NativeApi(Host(col))
    assert "move.play" in api.all_actions()
    out = api.run("move.play", {"source": "Cam", "in": "Chatting"})
    assert out == {"moving": ["Cam in Chatting", "Cam in Chatting"]}  # both shapes of the bloom
    scene = next(s for s in col.scenes if s.id == "chat-h")
    item = next(i for i in scene.items if i.source == "cam")
    assert (item.x, item.scale) == (20.0, 2.0)
    own = next(f for f in item.effects if f.kind == "move")
    assert own.enabled and own.settings["home"] == [0.0, 0.0, 1.0, None, 0.0]
    fx = video_effects(item.effects)
    dx, dy, scale, turn, seconds, run, easing = fx.move
    assert (scale, seconds, run, MOVE_EASINGS[easing]) == (0.5, 0.5, 1, "ease_out")
    assert dx == pytest.approx(-25.0) and dy == pytest.approx(-5.0)  # its middle was at 5, now at 30 (moved 20, twice as big)
    api.run("move.play", {"source": "Cam", "in": "Chatting", "back": True})
    assert (item.x, item.scale) == (0.0, 1.0) and own.settings["home"] is None
    with pytest.raises(ApiError):
        api.run("move.play", {"source": "Cam", "back": "yes"})


def test_each_item_with_a_move_gets_a_key_and_a_key_back(move_added):
    from sprigcast import plugin_hooks

    col = _collection()
    col.source("alert").filters.append(Filter(id="f-move", kind="move", name="Move", settings={}))
    keys = dict(plugin_hooks.hotkey_actions(col))
    assert "Move Alert in Chatting" in keys.values() and "Move back Alert in Chatting" in keys.values()


def test_an_obs_move_source_filter_comes_over_as_a_move():
    from sprigcast.core.convert import from_obs
    from test_convert import _like, obs_file

    data = obs_file()
    scene = next(s for s in data["sources"] if s.get("id") == "scene" and (s.get("settings") or {}).get("items"))
    target = scene["settings"]["items"][-1]
    target["scale"] = {"x": 1.0, "y": 1.0}
    name = next(s["name"] for s in data["sources"] if s.get("uuid") == target["source_uuid"])
    scene["filters"] = [{"id": "move_source_filter", "name": "Pop out", "settings": {
        "source": name, "pos": {"x": target["pos"]["x"] + 200, "y": target["pos"]["y"]},
        "scale": {"x": target["scale"]["x"] * 1.5, "y": target["scale"]["y"] * 1.5}, "duration": 800,
        "easing_match": 2, "easing_function_match": 9}}]
    garden, notes = from_obs(data, _like())
    moves = [f for s in garden.scenes for i in s.items for f in i.effects if f.kind == "move"]
    assert len(moves) == 1 and not moves[0].enabled
    s = moves[0].settings
    assert s["relative"] and s["x"] > 0 and s["y"] == 0 and s["size"] == 150
    assert (s["seconds"], s["easing"]) == (0.8, "bounce")
    assert any("now a Move" in n for n in notes)
    json.dumps(s)  # saved with the garden


def _mod():
    """The Move plugin as SprigCast loaded it (its actions and hotkeys are this copy's)."""
    import sys

    return sys.modules["sprigcast_plugin_move"]


def _wait(ms):
    from PySide6.QtTest import QTest

    QTest.qWait(ms)


def _item(col, scene_id, source_id):
    scene = next(s for s in col.scenes if s.id == scene_id)
    return next(i for i in scene.items if i.source == source_id)


def _watch(api, levels=None, times=None):
    move = _mod()

    for _ in range(times or move.LOOK_EVERY):
        move._watch(api, levels or {})


def test_a_move_can_start_when_its_bloom_shows_and_comes_home_when_it_goes(move_added):
    move = _mod()

    move._WATCH.clear()
    move._WATCH["tick"] = 0
    col = _collection()
    col.active_scenes = {"horizontal": "brb-h", "vertical": "brb-v"}
    col.source("alert").filters.append(Filter(id="f-move", kind="move", name="Drop in",
                                              settings={"y": 30.0, "when": "shown", "seconds": 0.05}))
    api = NativeApi(Host(col))
    _watch(api)  # the first look only learns what's on air
    assert _item(col, "chat-h", "alert").y == 0.0
    col.active_scenes = {"horizontal": "chat-h", "vertical": "chat-v"}
    _watch(api)
    assert _item(col, "chat-h", "alert").y == 30.0
    _watch(api)
    assert _item(col, "chat-h", "alert").y == 30.0  # once per showing
    col.active_scenes = {"horizontal": "brb-h", "vertical": "brb-v"}
    _watch(api)
    assert _item(col, "chat-h", "alert").y == 0.0  # off air: back home, ready for next time
    col.active_scenes = {"horizontal": "chat-h", "vertical": "chat-v"}
    _watch(api)
    assert _item(col, "chat-h", "alert").y == 30.0


def test_going_live_and_another_move_ending_start_moves(move_added):
    from sprigcast import plugin_hooks
    move = _mod()

    move._WATCH.clear()
    move._WATCH["tick"] = 0
    col = _collection()
    col.source("cam").filters.append(Filter(id="f-move", kind="move", name="Live",
                                            settings={"x": 10.0, "when": "live", "seconds": 0.05}))
    col.source("alert").filters.append(Filter(id="f-move2", kind="move", name="Next",
                                              settings={"x": 5.0, "when": "after", "after": "cam", "seconds": 0.05}))
    api = NativeApi(Host(col))
    try:
        _watch(api)
        plugin_hooks.output_changed("stream", 1.0)
        _watch(api)
        assert _item(col, "chat-h", "cam").x == 10.0
        _wait(250)  # the cam's trip ends: the alert's move starts
        assert _item(col, "chat-h", "alert").x == 5.0
    finally:
        plugin_hooks.output_changed("stream", None)


def test_two_items_trade_places_and_also_comes_along(move_added):
    col = _collection()
    _item(col, "chat-h", "alert").x = 40.0
    col.source("cam").filters.append(Filter(id="f-move", kind="move", name="Swap",
                                            settings={"swap": "Alert", "seconds": 0.05, "also": "front"}))
    api = NativeApi(Host(col))
    api.run("move.play", {"source": "Cam", "in": "Chatting"})
    cam, alert = _item(col, "chat-h", "cam"), _item(col, "chat-h", "alert")
    assert (cam.x, alert.x) == (40.0, 0.0)
    scene = next(s for s in col.scenes if s.id == "chat-h")
    assert scene.items[-1] is cam  # brought to the front
    partner = next(f for f in alert.effects if f.kind == "move")
    assert partner.settings["swap_partner"] and partner.settings["from"][0] == pytest.approx(40.0)
    from sprigcast import plugin_hooks

    keys = dict(plugin_hooks.hotkey_actions(col)).values()
    assert "Move Cam in Chatting" in keys and "Move Alert in Chatting" not in keys  # the partner has no key of its own


def test_a_value_move_slides_a_setting_there_and_back(move_added):
    col = _collection()
    col.source("cam").settings["opacity"] = 100
    col.source("cam").filters.append(Filter(id="f-val", kind="move_value", name="Fade",
                                            settings={"setting": "opacity", "to": 20.0, "seconds": 0.1,
                                                      "easing": "linear"}))
    col.source("cam").filters.append(Filter(id="f-blur", kind="blur", name="Blur", settings={"size": 0}))
    api = NativeApi(Host(col))
    api.run("move.value", {"source": "Cam", "in": "Chatting"})
    _wait(300)
    assert col.source("cam").settings["opacity"] == 20  # whole numbers stay whole
    api.run("move.value", {"source": "Cam", "in": "Chatting", "back": True})
    _wait(300)
    assert col.source("cam").settings["opacity"] == 100
    flt = next(f for f in col.source("cam").filters if f.kind == "move_value")
    flt.settings.update({"filter": "Blur", "setting": "size", "to": 8.0})
    api.run("move.value", {"source": "Cam", "in": "Chatting"})
    _wait(300)
    assert next(f for f in col.source("cam").filters if f.kind == "blur").settings["size"] == 8
    flt.settings.update({"filter": "", "setting": "color", "to_color": "#000000"})
    col.source("cam").settings["color"] = "#ffffff"
    api.run("move.value", {"source": "Cam", "in": "Chatting"})
    _wait(300)
    assert col.source("cam").settings["color"] == "#000000"
    flt.settings.update({"setting": "nope"})
    with pytest.raises(ApiError):
        api.run("move.value", {"source": "Cam", "in": "Chatting"})


def test_a_sound_move_bounces_while_its_sound_is_loud(move_added):
    move = _mod()

    move._WATCH.clear()
    move._WATCH["tick"] = 0
    col = _collection()
    col.source("cam").filters.append(Filter(id="f-snd", kind="move_sound", name="Talk",
                                            settings={"sound_id": "mic", "react": "grow", "amount": 10.0}))
    api = NativeApi(Host(col))
    move._watch(api, {"mic": (-40.0, -45.0)})
    flt = col.source("cam").filters[-1]
    assert not flt.settings.get("from")  # quiet
    move._PULSED.clear()
    move._watch(api, {"mic": (-5.0, -10.0)})
    assert flt.settings["from"] == [0.0, 0.0, pytest.approx(1.1), 0.0]
    fx = video_effects(col.source("cam").filters)
    assert fx.move[2] == pytest.approx(1.1) and fx.move[4] == move.PULSE_S
    _wait(int(move.PULSE_S * 2000) + 150)
    assert not flt.settings.get("from")  # settled: nothing left to draw
    assert move.pulse_of({"react": "bounce", "amount": 12.0}) == [0.0, -12.0, 1.0, 0.0]


class FakeCamera:
    def __init__(self):
        self.values = {3: 100}
        self.sets = []
        self.closed = False

    def range(self, prop):
        return (100, 500, 10, 100) if prop == 3 else None

    def get(self, prop):
        return self.values.get(prop)

    def set(self, prop, value):
        self.values[prop] = value
        self.sets.append((prop, value))
        return True

    def close(self):
        self.closed = True


def test_a_camera_move_eases_the_zoom_there_and_back(move_added, monkeypatch):
    from sprigcast.core.scene import SourceConfig
    move = _mod()

    camera = FakeCamera()
    monkeypatch.setattr(move, "_open_camera", lambda *a: camera)
    monkeypatch.setattr(move, "_camera_device", lambda source: ("Cam", "", 0))
    col = _collection()
    col.sources.append(SourceConfig(id="webcam", kind="camera", name="Webcam",
                                    filters=[Filter(id="f-cam", kind="move_camera", name="Zoom in",
                                                    settings={"zoom": 50.0, "seconds": 0.1})]))
    next(s for s in col.scenes if s.id == "chat-h").items.append(__import__("sprigcast.core.scene", fromlist=["SceneItem"]).SceneItem(source="webcam"))
    api = NativeApi(Host(col))
    api.run("move.camera", {"source": "Webcam", "in": "Chatting"})
    deadline = time.monotonic() + 3
    while camera.values[3] != 300 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert camera.values[3] == 300 and camera.closed  # half way along 100..500
    assert len(camera.sets) > 1  # eased, not jumped
    api.run("move.camera", {"source": "Webcam", "in": "Chatting", "back": True})
    deadline = time.monotonic() + 3
    while camera.values[3] != 100 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert camera.values[3] == 100
    with pytest.raises(ApiError):
        api.run("move.camera", {"source": "Cam", "in": "Chatting"})  # not a camera


def test_camera_paths_match_however_windows_spells_them():
    from sprigcast.sources.camera_control import path_matches

    shown = "@device:pnp:\\\\?\\usb#vid_0fd9&pid_0078&mi_00#7&1#{65e8773d-8f56-11d0-a3b9-00a0c9250000}\\global"
    assert path_matches(shown, "\\\\?\\USB#VID_0FD9&PID_0078&MI_00#7&1#{65e8773d-8f56-11d0-a3b9-00a0c9250000}\\global")
    assert not path_matches(shown, "\\\\?\\usb#vid_046d&pid_085e#1#{65e8773d}") and not path_matches(shown, "")


def test_obs_move_triggers_swaps_and_value_moves_come_over():
    from sprigcast.core.convert import from_obs
    from test_convert import _like, obs_file

    data = obs_file()
    scene = next(s for s in data["sources"] if s.get("id") == "scene" and len((s.get("settings") or {}).get("items", [])) >= 2)
    names = {s.get("uuid"): s["name"] for s in data["sources"]}
    plain = [names[i["source_uuid"]] for i in scene["settings"]["items"]
             if names.get(i.get("source_uuid")) and next(s for s in data["sources"]
                                                         if s.get("uuid") == i["source_uuid"])["id"] != "scene"]
    a, b = plain[0], plain[-1]
    scene["filters"] = [
        {"id": "move_source_filter", "name": "On show", "settings": {"source": a, "transform_relative": True,
                                                                      "pos": {"x": 0, "y": 50}, "start_trigger": 1}},
        {"id": "move_source_swap_filter", "name": "Swap", "settings": {"source_a": a, "source_b": b, "duration": 500}}]
    shown = next(s for s in data["sources"] if s["name"] == a)
    shown["filters"] = [{"id": "move_value_filter", "name": "Grow text", "settings": {
        "setting_name": "size", "setting_int": 72, "duration": 400}}]
    garden, notes = from_obs(data, _like())
    moves = [f for s in garden.scenes for i in s.items for f in i.effects if f.kind == "move"]
    assert {m.name for m in moves} == {"On show", "Swap"}
    on_show = next(m for m in moves if m.name == "On show").settings
    assert on_show["when"] == "shown" and on_show["y"] != 0
    swap = next(m for m in moves if m.name == "Swap").settings
    assert swap["swap"] == b and swap["seconds"] == 0.5
    values = [f for s in garden.sources for f in s.filters if f.kind == "move_value"]
    assert len(values) == 1 and values[0].settings["setting"] == "size" and values[0].settings["to"] == 72.0
    assert any("Move value" in n for n in notes)
