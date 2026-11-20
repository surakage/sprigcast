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


def test_a_bloom_off_air_is_really_drawn_by_the_engine():
    """Through a real engine, not a stand-in grab: the view's picture comes back (it was tapped under a name
    nothing draws, and every off-air bloom answered "Nothing is drawing" after the timeout, PC 2026-11-20)."""
    pytest.importorskip("PySide6.QtGui")
    import threading

    from sprigcast.app import screenshot
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
    from sprigcast.render.compositor import Compositor
    from sprigcast.render.engine import Engine

    try:
        compositor = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    try:
        col = SceneCollection(
            canvases=[Canvas(id="h", width=64, height=36)],
            sources=[SourceConfig(id="red", kind="color", name="Red",
                                  settings={"color": "#ff0000ff", "width": 64, "height": 36})],
            scenes=[Scene(id="live", name="Live", canvas="h"),
                    Scene(id="brb", name="BRB", canvas="h", items=[SceneItem(source="red")])],
            active_scenes={"h": "live"})
        engine = Engine(col)
        engine._sync_sources(compositor)
        got = []
        worker = threading.Thread(target=lambda: got.append(screenshot.grab_scene(engine, col.scene("brb"))))
        worker.start()
        t = 0.0
        while worker.is_alive():  # the render loop's ticks, here
            t += 1 / 30
            engine._tick(compositor, t)
            worker.join(0.01)
        image = got[0]
        assert image is not None and (image.width(), image.height()) == (64, 36)
        pixel = image.pixelColor(32, 18)
        assert (pixel.red(), pixel.green(), pixel.blue()) == (255, 0, 0)  # BRB, though Live is on air
        assert engine.views == {}
    finally:
        compositor.release()


def test_an_apps_screenshot_is_made_while_the_window_keeps_answering():
    """Over a connection the picture is made on a worker and answered Later: the window's thread never waits for
    it (514 ms not answering in one wait on the PC, 2026-11-20). Both shapes are asked for at once, and a message
    sent after it is answered after it."""
    import json
    import time

    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWebSockets import QWebSocket

    from sprigcast.app.control_server import ControlServer
    from sprigcast.control.api import ControlApi
    from sprigcast.control.native import Later
    from test_control_api import _qt, _wait

    app = _qt()

    class Slow(Host):
        def bloom_screenshots_later(self, scene_ids, name, width, save, as_data):
            self.asked.append(tuple(scene_ids))

            def job():
                time.sleep(0.4)
                return [{"width": 64, "height": 36, "png": "x"}, None]

            return Later(job, lambda future: future.result())

    host = Slow(_collection())
    server = ControlServer(ControlApi(host), native=NativeApi(host))
    assert server.start(0)
    client, got = QWebSocket(), []
    client.textMessageReceived.connect(lambda t: got.append(json.loads(t)))
    beats, gaps = [time.perf_counter()], []

    def beat():
        now = time.perf_counter()
        gaps.append(now - beats[-1])
        beats.append(now)

    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(beat)
    try:
        client.open(QUrl(f"ws://127.0.0.1:{server.port}/sprigcast"))
        assert _wait(app, lambda: got)
        client.sendTextMessage(json.dumps({"id": "shot", "do": "screenshot.take", "bloom": "BRB", "save": False,
                                           "data": True}))
        client.sendTextMessage(json.dumps({"id": "after", "do": "app.info"}))
        beats[-1] = time.perf_counter()
        timer.start()
        assert _wait(app, lambda: sum(1 for m in got if m.get("id") in ("shot", "after")) == 2, 5.0)
        timer.stop()
        answers = [m for m in got if m.get("id") in ("shot", "after")]
        assert [m["id"] for m in answers] == ["shot", "after"]
        assert answers[0]["ok"] and answers[0]["result"]["shots"] == [
            {"shape": "horizontal", "width": 64, "height": 36, "png": "x", "bloom": "BRB", "on_air": False}]
        assert host.asked == [("brb-h", "brb-v")]  # both shapes in one go
        assert max(gaps) < 0.2  # the window answered all along (the picture alone took 0.4 s)
    finally:
        client.close()
        _wait(app, lambda: not server.sessions, 2.0)
        server.stop()
