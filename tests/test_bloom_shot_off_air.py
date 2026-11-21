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
                              "bloomId": "brb-v", "on_air": False}]}
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
            {"shape": "horizontal", "width": 64, "height": 36, "png": "x", "bloom": "BRB", "bloomId": "brb-h",
             "on_air": False}]
        assert host.asked == [("brb-h", "brb-v")]  # both shapes in one go
        assert max(gaps) < 0.2  # the window answered all along (the picture alone took 0.4 s)
    finally:
        client.close()
        _wait(app, lambda: not server.sessions, 2.0)
        server.stop()


def test_both_shapes_means_both_and_a_shape_left_out_is_said():
    """PC 2026-11-21: {"bloom", "shape": "both"} gave one picture and no word why."""
    host = Host(_collection())
    api = NativeApi(host)
    # On air on both canvases: both pictures (it gave only the first).
    out = api.run("screenshot.take", {"bloom": "Chatting", "save": False, "data": True})
    assert [s["shape"] for s in out["shots"]] == ["horizontal", "vertical"] and "skipped" not in out
    assert all(s["bloom"] == "Chatting" for s in out["shots"])
    # On air in one shape only: that one, and the other is named.
    host.collection.active_scenes["vertical"] = "solo-v"
    out = api.run("screenshot.take", {"bloom": "Chatting", "save": False, "data": True})
    assert [s["shape"] for s in out["shots"]] == ["horizontal"]
    assert out["skipped"] == [{"shape": "vertical", "why": "'Chatting' isn't on air there."}]
    # A bloom made for one canvas (its other look is a bloom of its own): the shape it has no layout for is named.
    out = api.run("screenshot.take", {"bloom": "Shorts", "save": False, "data": True})
    assert [s["shape"] for s in out["shots"]] == ["vertical"]
    assert out["skipped"] == [{"shape": "horizontal", "why": "'Shorts' has no horizontal layout."}]
    out = api.run("screenshot.take", {"bloom": "BRB", "shape": "vertical", "save": False, "data": True})
    assert "skipped" not in out


def test_a_canvas_picture_has_nothing_see_through():
    """PC 2026-11-21: the PNG kept the canvas's see-through pixels, so it looked black in one viewer and white in
    another. It's put on black, as viewers get it."""
    from PySide6.QtGui import QColor, QImage

    from sprigcast.app import screenshot

    canvas = QImage(4, 2, QImage.Format.Format_RGBA8888_Premultiplied)
    canvas.fill(QColor(0, 0, 0, 0))
    canvas.setPixelColor(0, 0, QColor(255, 0, 0, 255))
    canvas.setPixelColor(1, 0, QColor(255, 255, 255, 26))  # the chat area: barely there
    flat = screenshot.on_black(canvas)
    assert not flat.hasAlphaChannel()
    assert flat.pixelColor(0, 0) == QColor(255, 0, 0) and flat.pixelColor(3, 1) == QColor(0, 0, 0)
    assert flat.pixelColor(1, 0).red() in (25, 26, 27)  # white at alpha 26, on black

    class Frame:
        width, height, data, fmt = 4, 2, bytes(canvas.constBits()), "rgba"

    class Engine:
        def add_frame_tap(self, key, tap):
            tap(Frame())

        def remove_frame_tap(self, key, tap):
            pass

    got = screenshot.grab(Engine(), ["horizontal"])
    assert got and not got["horizontal"].hasAlphaChannel()


def test_a_bloom_named_by_its_own_canvas_is_pictured_on_that_canvas():
    """PC 2026-11-21: {"bloom": "bloom-8"} (the vertical Gameplay, no shape) answered with the horizontal picture."""
    host = Host(_collection())
    api = NativeApi(host)
    ask = {"save": False, "data": True}
    assert [s["shape"] for s in api.run("screenshot.take", {"bloom": "chat-v", **ask})["shots"]] == ["vertical"]
    assert [s["shape"] for s in api.run("screenshot.take", {"bloom": "brb-v", **ask})["shots"]] == ["vertical"]  # off air
    assert host.asked[-1][0] == "brb-v"
    host.collection.scene("brb-v").name = "BRB (Vertical)"  # a name only the vertical one has
    out = api.run("screenshot.take", {"bloom": "BRB (Vertical)", **ask})
    assert [s["shape"] for s in out["shots"]] == ["vertical"] and "skipped" not in out
    # The main bloom, a name both share, or shape given: as before.
    for asked in ({"bloom": "chat-h"}, {"bloom": "Chatting"}, {"bloom": "chat-v", "shape": "both"}):
        assert [s["shape"] for s in api.run("screenshot.take", {**asked, **ask})["shots"]] == ["horizontal", "vertical"]
    assert [s["shape"] for s in api.run("screenshot.take", {"bloom": "chat-v", "shape": "horizontal", **ask})["shots"]] \
        == ["horizontal"]


def test_the_native_engine_draws_a_bloom_pictured_off_air(monkeypatch):
    """PC 2026-11-21: every screenshot of a bloom off air cost a late frame of 35 to 57 ms: Python's compositor
    drew every sprig of it for the first time in one tick. The view is the native engine's now (a slot of its own,
    nothing tapping it) and the picture is read from there; Python draws it only when the engine can't."""
    pytest.importorskip("PySide6.QtGui")
    from sprigcast.app import screenshot

    col = _collection()
    brb = col.scene("brb-h")
    calls, taps = [], []

    class Engine:
        _collection, _sources, _ticks = col, {}, 0
        ready_after = 2

        def set_view(self, name, canvas_id=None, kind="bloom", ref=None, shot=False):
            calls.append((name, canvas_id, shot))

        def native_view_ready(self, name):
            self.ready_after -= 1
            return self.ready_after <= 0

        def native_shot(self, canvas_id):
            calls.append(("read", canvas_id))
            return bytes([0, 0, 255, 128] * 8 * 4), 8, 4  # BGRA premultiplied, half see-through

        def add_frame_tap(self, key, tap):
            taps.append(key)

        def remove_frame_tap(self, key, tap):
            pass

    monkeypatch.setattr(screenshot.time, "sleep", lambda s: None)
    image = screenshot.grab_scene(Engine(), brb)
    assert calls == [("apishot", "horizontal", True), ("read", "view:apishot"), ("apishot", None, False)]
    assert not taps  # Python's compositor was never asked to draw it
    assert (image.width(), image.height()) == (8, 4) and image.pixelColor(0, 0).alpha() == 255  # on black


def test_a_shot_view_is_the_engines_with_nothing_tapping_it():
    from sprigcast.render.engine import Engine

    col = _collection()
    engine = Engine.__new__(Engine)
    engine._collection, engine.views, engine._shot_views = col, {}, frozenset()
    engine.staged, engine.editing_part, engine.clean_hidden = {}, None, {}
    off_air = next(s for s in col.scenes if s.canvas == "horizontal" and s.id != col.active_scenes["horizontal"])
    engine.set_view("apishot", "horizontal", "bloom", off_air.id, shot=True)
    engine.set_view("other", "horizontal", "bloom", off_air.id)  # an ordinary view nothing taps: not drawn
    engine.set_view("onair", "horizontal", "bloom", col.active_scenes["horizontal"], shot=True)
    views, riders = engine._native_views({}, {})
    assert set(views) == {"apishot"} and views["apishot"][1].id == off_air.id and not riders
    engine.set_view("apishot", None)
    assert engine._shot_views == frozenset({"onair"}) and engine._native_views({}, {})[0] == {}
