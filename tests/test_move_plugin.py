"""The Move store plugin: something in a bloom moves when a key, a Stream Deck button or an app says, eased,
drawn frame by frame by the engines (Python and native do the same sums)."""

import json
import math

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
