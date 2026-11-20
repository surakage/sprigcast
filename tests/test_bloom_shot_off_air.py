"""screenshot.take of a bloom that isn't on air: drawn on its own canvas, nothing on air changes (2026-11-20)."""

import types

import pytest

from sprigcast.control.native import ApiError, NativeApi

from test_control_api import FakeHost, _collection


def test_a_bloom_off_air_is_drawn_through_a_view_once_its_sources_woke(monkeypatch):
    pytest.importorskip("PySide6.QtGui")
    from PySide6.QtGui import QImage

    from sprigcast.app import screenshot

    col = _collection()
    brb = col.scene("brb-h")
    brb.items.append(col.scene("chat-h").items[0].model_copy())  # the Cam, asleep while BRB is off air
    cam = types.SimpleNamespace(asleep=True)
    seen = []

    class Engine:
        _collection, _sources, views = col, {"cam": cam}, {}

        def set_view(self, name, canvas_id=None, kind="bloom", ref=None):
            seen.append((name, canvas_id, kind, ref))
            if canvas_id is not None:
                self.views[name] = (canvas_id, kind, ref)
            else:
                self.views.pop(name, None)

    engine = Engine()
    naps = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 3:
            cam.asleep = False  # the view woke it

    def grab(eng, ids):
        assert not cam.asleep and eng.views == {"apishot": ("horizontal", "bloom", "brb-h")}  # still drawn, awake
        return {ids[0]: QImage(8, 4, QImage.Format.Format_RGBA8888_Premultiplied)}

    monkeypatch.setattr(screenshot.time, "sleep", nap)
    monkeypatch.setattr(screenshot, "grab", grab)
    shot = screenshot.take_scene(engine, brb, "BRB", None, as_data=True)
    assert (shot["width"], shot["height"]) == (8, 4) and shot["png"]
    assert seen == [("apishot", "horizontal", "bloom", "brb-h"), ("apishot", None, "bloom", None)]
    assert engine.views == {} and naps[-1] == screenshot.SCENE_SETTLE_S  # the view is gone: its sources rest again
    # Nothing drawing: no picture, and the view still goes.
    monkeypatch.setattr(screenshot, "grab", lambda eng, ids: {})
    assert screenshot.take_scene(engine, brb, "BRB", None, as_data=True) is None and engine.views == {}


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
