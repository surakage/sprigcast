"""screenshot.take of a bloom that isn't on air: drawn on its own canvas, nothing on air changes (2026-11-20)."""

import numpy as np
import pytest

from sprigcast.control.native import ApiError, NativeApi
from sprigcast.render.engine import SCENE_SHOT_SETTLE_S, SCENE_SHOT_WAIT_S, SLEEP_AFTER

from test_control_api import FakeHost, _collection
from test_sleeping_sources import FakeCompositor, _engine


class Compositor(FakeCompositor):
    def __init__(self):
        super().__init__()
        self.textures, self.drawn = {}, []

    def upload(self, source_id, *a):
        super().upload(source_id, *a)
        self.textures[source_id] = True

    def forget(self, source_id):
        super().forget(source_id)
        self.textures.pop(source_id, None)

    def render(self, canvas, scene, *a, **k):
        self.drawn.append((canvas.id, getattr(scene, "id", None)))
        self.targets[canvas.id] = object()

    def read(self, key):
        return bytes(64 * 36 * 4)


def test_a_bloom_off_air_is_pictured_once_its_sources_woke_and_then_they_rest_again():
    engine, col = _engine()
    col.scenes[1].items[0].visible = True  # "Starting soon" shows its source
    fake, got = Compositor(), []
    engine._tick(fake, 100.0)
    engine._tick(fake, 101.0)
    soon = engine._sources["soon"]
    assert soon.asleep and "soon" not in fake.textures
    engine.grab_scene("b", got.append)
    engine._tick(fake, 102.0)  # it wakes; no picture taken yet
    assert not soon.asleep and got == [] and col.active_scenes == {"h": "a"}
    engine._tick(fake, 102.1)
    assert got == []  # its picture only just arrived: one more moment
    engine._tick(fake, 102.1 + SCENE_SHOT_SETTLE_S + 0.05)
    assert len(got) == 1 and got[0].shape == (36, 64, 4)
    assert ("sceneshot:h", "b") in fake.drawn and "sceneshot:h" not in fake.targets  # its target is let go
    assert engine._scene_shots == [] and col.active_scenes == {"h": "a"}
    engine._tick(fake, 103.0)
    engine._tick(fake, 103.0 + SLEEP_AFTER + 1)
    assert soon.asleep  # nothing shows it any more


def test_a_source_that_never_gets_a_picture_doesnt_hold_it_up_for_ever_and_a_gone_bloom_answers_none():
    engine, col = _engine()
    col.scenes[1].items[0].visible = True
    fake, got = Compositor(), []
    fake.upload = lambda *a: None  # no picture ever arrives
    engine.grab_scene("b", got.append)
    engine.grab_scene("nope", got.append)
    engine._tick(fake, 100.0)
    assert got == [None]  # the bloom that doesn't exist
    engine._tick(fake, 100.0 + SCENE_SHOT_WAIT_S - 0.5)
    assert len(got) == 1
    engine._tick(fake, 100.0 + SCENE_SHOT_WAIT_S + 0.1)
    assert len(got) == 2 and got[1] is not None  # taken as it is


class Host(FakeHost):
    def __init__(self, collection):
        super().__init__(collection)
        self.asked = []

    def bloom_screenshot(self, scene_id, name, width, save, as_data):
        self.asked.append((scene_id, name, width, save, as_data))
        return {"width": 64, "height": 36, "png": "iVBOR"}


def test_the_api_pictures_a_bloom_off_air_on_the_shape_asked_but_not_while_live():
    host = Host(_collection())
    api = NativeApi(host)
    out = api.run("screenshot.take", {"bloom": "BRB", "shape": "vertical", "save": False, "data": True})
    assert host.asked == [("brb-v", "BRB", None, False, True)]
    assert out == {"shots": [{"shape": "vertical", "width": 64, "height": 36, "png": "iVBOR", "bloom": "BRB",
                              "on_air": False}]}
    both = api.run("screenshot.take", {"bloom": "BRB", "save": False, "data": True})
    assert [s["shape"] for s in both["shots"]] == ["horizontal", "vertical"]
    host.start_stream()
    with pytest.raises(ApiError) as err:
        api.run("screenshot.take", {"bloom": "BRB", "shape": "vertical", "save": False, "data": True})
    assert err.value.code == "not_allowed" and "live or recording" in err.value.message
    host.stop_stream()
    host.start_record()
    with pytest.raises(ApiError):
        api.run("screenshot.take", {"bloom": "BRB", "save": False, "data": True})
    assert "screenshot.bloom_off_air" in api.run("app.info", {})["features"]
