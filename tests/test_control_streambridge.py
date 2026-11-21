"""What THSV StreamBridge needs: make blooms, browser sources and items, per-place stream status and
stop/start while live, through both the SprigCast API and the OBS door."""

from sprigcast.control.api import ControlApi
from sprigcast.control.protocol import Status
from sprigcast.core.scene import Output
from test_control_api import FakeHost, _collection
from test_control_api import _fails as _obs_fails
from test_control_api import _ok as _obs_ok
from test_control_native import _fails, _ok


class PlacesHost(FakeHost):
    def __init__(self, collection):
        super().__init__(collection)
        self.places = {}

    def stream_places(self):
        return {k: dict(v) for k, v in self.places.items()} if self.live is not None else {}

    def start_place(self, output_id):
        self.places[output_id] = {"on": True, "reconnecting": False, "seconds": 0.0, "skipped": 0, "frames": 0}

    def stop_place(self, output_id):
        self.places.pop(output_id, None)


def _host():
    col = _collection()
    col.outputs = [Output(id="out-1", type="stream", service="twitch", canvas="horizontal", enabled=True),
                   Output(id="out-2", type="stream", service="youtube", canvas="vertical", enabled=False)]
    return PlacesHost(col)


def _native(host=None):
    from sprigcast.control.native import NativeApi

    host = host or _host()
    return NativeApi(host), host


def test_native_makes_a_bloom_a_browser_source_and_an_item():
    api, host = _native()
    col = host.collection
    bloom = _ok(api, "bloom.new", name="Chat First")
    assert bloom["name"] == "Chat First" and set(bloom["shapes"]) == {"horizontal", "vertical"}
    assert "scenes" in host.commits
    assert _fails(api, "bloom.new", name="Chat First") == "busy"
    source = _ok(api, "source.new", name="Hydrate", kind="browser",
                 settings={"url": "http://127.0.0.1:8080/hydrate", "width": 800, "height": 600})
    assert source["settings"]["url"].endswith("/hydrate") and source["settings"]["keep_running"] is False
    assert _fails(api, "source.new", name="Hydrate", kind="browser") == "busy"
    assert _fails(api, "source.new", name="X", kind="nope") == "bad_request"
    added = _ok(api, "item.add", bloom="Chat First", source="Hydrate", visible=False)
    assert added["item"] == source["id"] and set(added["shapes"]) == {"horizontal", "vertical"}
    # It went in the bloom's own sprig (blooms hold sprigs only), in both shapes, hidden.
    sprig = next(s for s in col.scenes if s.kind == "part" and s.name == "Chat First" and s.shape_of is None)
    for shape in col.shapes(sprig):
        assert [(i.source, i.visible) for i in shape.items] == [(source["id"], False)]
    # item.place works on it, and items.list shows it under the bloom.
    _ok(api, "item.place", bloom="Chat First", item="Hydrate", x=10, y=20)
    _ok(api, "item.show", bloom="Chat First", item="Hydrate")
    assert all(shape.items[0].visible for shape in col.shapes(sprig))
    # Removing the bloom takes its sprig and the now-unused source with it.
    assert _ok(api, "bloom.remove", bloom="Chat First") == {"removed": "Chat First"}
    assert not any(s.name == "Chat First" for s in col.scenes)
    assert not any(s.name == "Hydrate" for s in col.sources)


def test_native_wont_remove_a_canvas_s_last_bloom():
    api, host = _native()
    col = host.collection
    _ok(api, "bloom.remove", bloom="BRB")
    _ok(api, "bloom.remove", bloom="Shorts")
    assert _fails(api, "bloom.remove", bloom="Chatting") == "not_allowed"
    assert col.active_scenes == {"horizontal": "chat-h", "vertical": "chat-v"}


def test_native_places_and_stop_start_while_live():
    api, host = _native()
    status = _ok(api, "stream.status")
    assert status["on"] is False and [p["id"] for p in status["places"]] == ["out-1", "out-2"]
    assert not any(p["on"] for p in status["places"])
    assert _fails(api, "place.stop", place="out-1") == "not_allowed"
    host.start_stream()
    host.start_place("out-1")
    places = {p["id"]: p for p in _ok(api, "stream.status")["places"]}
    assert places["out-1"]["on"] and not places["out-2"]["on"]
    assert set(places["out-1"]) >= {"id", "name", "on", "reconnecting", "seconds", "skipped", "frames"}
    after = {p["id"]: p["on"] for p in _ok(api, "place.start", place="out-2")["places"]}
    assert after == {"out-1": True, "out-2": True}
    assert host.collection.outputs[1].enabled  # on for next time too
    after = {p["id"]: p["on"] for p in _ok(api, "place.stop", place="out-1")["places"]}
    assert after == {"out-1": False, "out-2": True}
    assert _fails(api, "place.stop", place="nope") == "not_found"


def _obs():
    host = _host()
    return ControlApi(host), host


def test_obs_door_creates_scene_input_and_item():
    api, host = _obs()
    col = host.collection
    uuid = _obs_ok(api, "CreateScene", {"sceneName": "Chat First"})["sceneUuid"]
    assert col.scene(uuid).name == "Chat First"
    assert _obs_fails(api, "CreateScene", {"sceneName": "Chat First"}) == Status.RESOURCE_ALREADY_EXISTS
    names = [s["sceneName"] for s in _obs_ok(api, "GetSceneList")["scenes"]]
    assert "Chat First" in names
    made = _obs_ok(api, "CreateInput", {"sceneName": "Chat First", "inputName": "Hydrate", "inputKind": "browser_source",
                                        "inputSettings": {"url": "http://x/h", "width": 800, "height": 600,
                                                          "shutdown": True},
                                        "sceneItemEnabled": False})
    assert isinstance(made["sceneItemId"], int) and made["inputUuid"]
    source = col.source(made["inputUuid"])
    assert source.kind == "browser" and source.settings["keep_running"] is False
    assert _obs_ok(api, "GetInputSettings", {"inputName": "Hydrate"})["inputSettings"]["shutdown"] is True
    _obs_ok(api, "SetInputSettings", {"inputName": "Hydrate", "inputSettings": {"shutdown": False}})
    assert source.settings["keep_running"] is True
    assert _obs_ok(api, "GetInputSettings", {"inputName": "Hydrate"})["inputSettings"]["shutdown"] is False
    assert {"inputName": "Hydrate", "inputUuid": source.id, "inputKind": "browser_source",
            "unversionedInputKind": "browser_source"} in _obs_ok(api, "GetInputList")["inputs"]
    # The item answers to the scene's name, and transform and enable work on it.
    items = _obs_ok(api, "GetSceneItemList", {"sceneName": "Chat First"})["sceneItems"]
    assert [(i["sourceName"], i["sceneItemId"], i["sceneItemEnabled"]) for i in items] == \
        [("Hydrate", made["sceneItemId"], False)]
    assert _obs_ok(api, "GetSceneItemId", {"sceneName": "Chat First", "sourceName": "Hydrate"})["sceneItemId"] \
        == made["sceneItemId"]
    _obs_ok(api, "SetSceneItemTransform", {"sceneName": "Chat First", "sceneItemId": made["sceneItemId"],
                                           "sceneItemTransform": {"positionX": 40, "positionY": 50,
                                                                  "scaleX": 0.5, "scaleY": 0.5}})
    _obs_ok(api, "SetSceneItemEnabled", {"sceneName": "Chat First", "sceneItemId": made["sceneItemId"],
                                         "sceneItemEnabled": True})
    sprig = next(s for s in col.scenes if s.kind == "part" and s.name == "Chat First" and s.shape_of is None)
    for shape in col.shapes(sprig):
        assert shape.items[0].visible
    item = col.shape_for(sprig, "horizontal").items[0]
    assert (item.x, item.y, item.scale) == (40, 50, 0.5)
    # CreateSceneItem puts an existing input in too, with its own id.
    again = _obs_ok(api, "CreateSceneItem", {"sceneName": "Chat First", "sourceName": "Alert"})["sceneItemId"]
    assert again != made["sceneItemId"]
    _obs_ok(api, "RemoveSceneItem", {"sceneName": "Chat First", "sceneItemId": again})
    assert [i["sourceName"] for i in _obs_ok(api, "GetSceneItemList", {"sceneName": "Chat First"})["sceneItems"]] \
        == ["Hydrate"]
    assert _obs_fails(api, "CreateInput", {"sceneName": "Chat First", "inputName": "Hydrate",
                                           "inputKind": "browser_source"}) == Status.RESOURCE_ALREADY_EXISTS
    assert _obs_fails(api, "CreateInput", {"sceneName": "Chat First", "inputName": "Z",
                                           "inputKind": "nope"}) == Status.INVALID_INPUT_KIND
    _obs_ok(api, "RemoveScene", {"sceneName": "Chat First"})
    assert not any(s.name == "Chat First" for s in col.scenes)
    available = _obs_ok(api, "GetVersion")["availableRequests"]
    assert {"CreateScene", "RemoveScene", "CreateInput", "CreateSceneItem", "GetOutputStatus", "StartOutput",
            "StopOutput"} <= set(available)


def test_obs_door_items_in_an_imported_bloom():
    """An OBS scene imported becomes a bloom holding one sprig: its items answer to the scene's name."""
    api, host = _obs()
    col = host.collection
    _obs_ok(api, "CreateScene", {"sceneName": "Hydrate time"})
    first = _obs_ok(api, "CreateSceneItem", {"sceneName": "Hydrate time", "sourceName": "Cam"})["sceneItemId"]
    second = _obs_ok(api, "CreateSceneItem", {"sceneName": "Hydrate time", "sourceName": "Alert"})["sceneItemId"]
    _obs_ok(api, "SetSceneItemEnabled", {"sceneName": "Hydrate time", "sceneItemId": first, "sceneItemEnabled": False})
    sprig = next(s for s in col.scenes if s.kind == "part" and s.name == "Hydrate time" and s.shape_of is None)
    assert [i.visible for i in col.shape_for(sprig, "vertical").items] == [False, True]
    assert first != second


def test_obs_door_outputs_per_place():
    api, host = _obs()
    listed = {o["outputName"]: o for o in _obs_ok(api, "GetOutputList")["outputs"]}
    flags = {"OBS_OUTPUT_AUDIO": True, "OBS_OUTPUT_VIDEO": True, "OBS_OUTPUT_ENCODED": True, "OBS_OUTPUT_SERVICE": True}
    assert listed["simple_stream"]["outputFlags"] == flags and listed["sprigcast:out-1"]["outputFlags"] == flags
    status = _obs_ok(api, "GetOutputStatus", {"outputName": "sprigcast:out-1"})
    assert set(status) == {"outputActive", "outputReconnecting", "outputTimecode", "outputDuration",
                           "outputCongestion", "outputBytes", "outputSkippedFrames", "outputTotalFrames"}
    assert status["outputActive"] is False
    assert _obs_fails(api, "StartOutput", {"outputName": "sprigcast:out-1"}) == Status.OUTPUT_NOT_RUNNING
    _obs_ok(api, "StartOutput", {"outputName": "simple_stream"})
    assert host.live is not None
    host.places["out-1"] = {"seconds": 12.0, "frames": 700, "skipped": 3, "reconnecting": False}
    status = _obs_ok(api, "GetOutputStatus", {"outputName": "sprigcast:out-1"})
    assert status["outputActive"] and status["outputTotalFrames"] == 700 and status["outputSkippedFrames"] == 3
    assert _obs_ok(api, "GetOutputStatus", {"outputName": "sprigcast:out-2"})["outputActive"] is False
    _obs_ok(api, "StartOutput", {"outputName": "sprigcast:out-2"})
    assert "out-2" in host.places
    _obs_ok(api, "StopOutput", {"outputName": "sprigcast:out-1"})
    assert "out-1" not in host.places
    assert _obs_fails(api, "StopOutput", {"outputName": "sprigcast:out-1"}) == Status.OUTPUT_NOT_RUNNING
    assert _obs_fails(api, "GetOutputStatus", {"outputName": "sprigcast:nope"}) == Status.RESOURCE_NOT_FOUND
    _obs_ok(api, "StopOutput", {"outputName": "simple_stream"})
    assert host.live is None


def test_connection_file_says_where_without_secrets(tmp_path):
    import json

    from test_control_api import _api as _plain_api
    from test_control_api import _qt

    from sprigcast.app.control_server import ControlServer

    _qt()
    api, _ = _plain_api()
    server = ControlServer(api)
    server.connection_path = tmp_path / "SprigCast" / "connection.json"
    server.obs_door = True
    assert server.start(0, password="secret")
    try:
        info = json.loads(server.connection_path.read_text(encoding="utf-8"))
        assert info["port"] == server.port and info["path"] == "/sprigcast"
        assert info["obsCompatible"] is True and info["passwordSet"] is True and info["app"] == "SprigCast"
        assert "secret" not in server.connection_path.read_text(encoding="utf-8") and "password" not in info
        server.obs_door = False
        server.write_connection()
        assert json.loads(server.connection_path.read_text(encoding="utf-8"))["obsCompatible"] is False
    finally:
        server.stop()
    assert not server.connection_path.exists()


class ShotHost(PlacesHost):
    def source_screenshot(self, source_id, name, width, save, as_data):
        self.shots.append((source_id, name, width, save, as_data))
        return {"width": 160, "height": 90, "png": "iVBOR"} if source_id != "alert" else None


def test_native_screenshot_of_one_source_or_bloom():
    api, host = _native(ShotHost(_host().collection))
    out = _ok(api, "screenshot.take", source="Cam", save=False, data=True)
    assert out == {"shots": [{"width": 160, "height": 90, "png": "iVBOR", "source": "Cam"}]}
    assert host.shots[-1] == ("cam", "Cam", None, False, True)
    assert _fails(api, "screenshot.take", source="Alert", save=False, data=True) == "not_allowed"  # not drawing
    assert _fails(api, "screenshot.take", source="Nobody", save=False, data=True) == "not_found"
    # A bloom on air is its canvas; one off air holding one sprig is that sprig on its own.
    on_air = _ok(api, "screenshot.take", bloom="Chatting", save=False, data=True)["shots"]
    assert [s["shape"] for s in on_air] == ["horizontal"] and on_air[0]["bloom"] == "Chatting"
    _ok(api, "bloom.new", name="Snap me")
    _ok(api, "item.add", bloom="Snap me", source="Cam")
    shot = _ok(api, "screenshot.take", bloom="Snap me", save=False, data=True)["shots"][0]
    assert shot["bloom"] == "Snap me" and host.shots[-1][1] == "Snap me"
    assert _fails(api, "screenshot.take", bloom="BRB", save=False, data=True) == "not_allowed"  # empty, off air
    assert _fails(api, "screenshot.take", bloom="Nobody", save=False, data=True) == "not_found"


def test_native_stream_stop_answers_off_and_rotate():
    api, host = _native()
    host.start_stream()
    assert _ok(api, "stream.stop") == {"on": False}
    assert _ok(api, "stream.status")["on"] is False
    out = _ok(api, "source.rotate", source="Alert", degrees=-90)
    assert out == {"source": "Alert", "degrees": 270.0, "items": 2}
    assert all(i.rotation == 270.0 for s in host.collection.scenes for i in s.items if i.source == "alert")
    assert _fails(api, "source.rotate", source="Desktop audio", degrees=10) == "not_allowed"
    assert _fails(api, "source.rotate", source="Nobody", degrees=10) == "not_found"
    assert _fails(api, "bloom.show", bloom="Nobody") == "not_found"
    assert _fails(api, "filter.set", source="Cam", filter="Nobody", on=True) == "not_found"


def test_one_source_picture_from_the_engine(tmp_path):
    import numpy as np

    from sprigcast.app import screenshot

    class Engine:
        def grab_source(self, source_id, callback):
            callback(np.full((90, 160, 4), 200, dtype=np.uint8) if source_id == "cam" else None)

    shot = screenshot.take_one(Engine(), "cam", "Face/cam", tmp_path, width=80, as_data=True)
    assert shot["width"] == 80 and shot["height"] == 45 and shot["png"]
    assert "Face_cam" in shot["path"] and shot["path"].endswith(".png")
    assert screenshot.take_one(Engine(), "gone", "Gone", None, as_data=True) is None


class DoctorHost(PlacesHost):
    """Going live or recording when SprigCast would ask first: the doctor's warnings, or low space."""

    warnings = ["Your PC is busy (CPU 95%)."]

    def start_stream(self, force=False):
        if not force:
            return {"warnings": list(self.warnings), "started": False}
        self.live = {"seconds": 0.0}
        return {"warnings": list(self.warnings), "started": True}

    def start_record(self, force=False):
        if not force:
            return {"warnings": ["Only 3 minutes of space left."], "started": False}
        self.rec = {"seconds": 0.0, "path": "C:/rec.mkv"}
        return {"warnings": ["Only 3 minutes of space left."], "started": True}


def test_remote_starts_never_wait_on_the_doctor():
    from sprigcast.control.native import NativeApi, answer as handle_message

    host = DoctorHost(_host().collection)
    api = NativeApi(host)
    answer = handle_message(api, {"id": "1", "do": "stream.start"})
    assert answer["ok"] is False and answer["error"]["code"] == "doctor_warnings"
    assert answer["error"]["warnings"] == ["Your PC is busy (CPU 95%)."] and host.live is None
    assert _fails(api, "stream.toggle") == "doctor_warnings" and host.live is None
    assert _ok(api, "stream.start", force=True) == {"on": True, "warnings": ["Your PC is busy (CPU 95%)."],
                                                    "places": [{"id": "out-1", "service": "twitch",
                                                                "shape": "horizontal"}]}
    assert host.live is not None
    assert _fails(api, "record.start") == "doctor_warnings" and host.rec is None
    assert _ok(api, "record.toggle", force=True)["on"] is True and host.rec is not None
    # With nothing to warn about it just goes live.
    host = DoctorHost(_host().collection)
    host.warnings = []
    host.start_stream = lambda force=False: None
    assert _ok(NativeApi(host), "stream.start")["on"] is True


def test_obs_door_starts_never_wait_on_the_doctor():
    host = DoctorHost(_host().collection)
    api = ControlApi(host)
    assert _obs_fails(api, "StartStream") == Status.OUTPUT_DISABLED and host.live is None
    assert _obs_fails(api, "ToggleStream") == Status.OUTPUT_DISABLED and host.live is None
    assert _obs_fails(api, "StartOutput", {"outputName": "simple_stream"}) == Status.OUTPUT_DISABLED
    _obs_ok(api, "StartStream", {"force": True})
    assert host.live is not None
    assert _obs_fails(api, "StartRecord") == Status.OUTPUT_DISABLED and host.rec is None


def test_main_window_remote_start_asks_nothing(monkeypatch):
    """The real going-live code from an app: the doctor's warnings come back, and no pop-up ever opens."""
    import types

    pytest_mw = __import__("pytest").importorskip("sprigcast.app.main_window")
    from PySide6.QtWidgets import QMessageBox

    from sprigcast.app import message as msgbox

    def no_popups(*args, **kwargs):
        raise AssertionError("a pop-up opened")

    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(msgbox, name, staticmethod(no_popups))
    monkeypatch.setattr(QMessageBox, "exec", no_popups)
    monkeypatch.setattr(msgbox.MessageCard, "exec", no_popups)
    col = _host().collection
    window = types.SimpleNamespace(collection=col, _stream_target=lambda o, ask=True, quiet=False: (object(), None),
                                   _doctor_warnings=lambda: ["Your PC is busy (CPU 95%)."],
                                   _doctor_says_go=no_popups)
    start = pytest_mw.MainWindow._start_streaming
    assert start(window, remote=True) == {"warnings": ["Your PC is busy (CPU 95%)."]}
    window._stream_target = lambda o, ask=True, quiet=False: (None, "Twitch: add your stream key in Stream…")
    assert start(window, remote=True) == {"problem": "Not ready to go live: Twitch: add your stream key in Stream…"}
    col.outputs = []
    assert "Tick at least one place" in start(window, remote=True)["problem"]


def test_go_live_hotkey_from_an_app_asks_nothing():
    from sprigcast.app.control_server import WindowHost

    col = _host().collection

    class Window:
        collection, streaming, recording = col, None, None

        def _start_streaming(self, remote=False, force=False):
            assert remote
            return {"warnings": ["Your PC is busy (CPU 95%)."]}

        def _on_hotkey(self, action):
            raise AssertionError("the window's own hotkey path can ask with a pop-up")

    host = WindowHost.__new__(WindowHost)
    host.w = Window()
    assert host.trigger_hotkey("toggle_stream") == "Not started: Your PC is busy (CPU 95%)."


def test_app_info_features_and_canvas_sizes():
    api, host = _native()
    info = _ok(api, "app.info")
    assert {"bloom.new", "screenshot.source", "plugins.enabled", "effects.autoend", "canvas.size", "item.scale_xy",
            "events.effect", "events.plugins", "events.lists", "notfound.what", "page.api"} <= set(info["features"])
    assert info["canvases"] == [{"id": "horizontal", "width": 64, "height": 36, "fps": 30, "enabled": True},
                                {"id": "vertical", "width": 36, "height": 64, "fps": 30, "enabled": True}] or \
        [(c["id"], c["width"], c["height"]) for c in info["canvases"]] == [("horizontal", 64, 36), ("vertical", 36, 64)]
    assert info["api"] == 0 and info["shapes"] == ["horizontal", "vertical"]  # old fields stay


def test_item_place_stretches_and_not_found_says_what():
    from sprigcast.control.native import answer

    api, host = _native()
    _ok(api, "bloom.new", name="Overlay")
    item = _ok(api, "source.new", name="Full", kind="browser", settings={"url": "http://x", "width": 1920,
                                                                        "height": 1080})["id"]
    _ok(api, "item.add", bloom="Overlay", source=item)
    out = _ok(api, "item.place", bloom="Overlay", item=item, shape="vertical", x=0, y=0, scale_x=0.5625, scale_y=1.7778)
    assert out["items"] == [{"x": 0.0, "y": 0.0, "scale": 0.5625, "rotation": 0.0, "scale_x": 0.5625,
                             "scale_y": 1.7778}]
    placed = _ok(api, "item.place", bloom="Overlay", item=item, shape="vertical", scale_y=0.5625)["items"][0]
    assert placed["scale_x"] == placed["scale_y"] == 0.5625  # same both ways: not stretched any more
    assert "scale_x" not in _ok(api, "item.place", bloom="Overlay", item=item, x=5)["items"][0]  # old answer
    assert _fails(api, "item.place", bloom="Overlay", item=item, scale_x=0) == "bad_request"
    for request, what, name in (({"do": "bloom.show", "bloom": "Nope"}, "bloom", "Nope"),
                                ({"do": "source.get", "source": "Nope"}, "source", "Nope"),
                                ({"do": "sprig.state", "sprig": "Nope", "state": None}, "sprig", "Nope"),
                                ({"do": "item.show", "bloom": "Overlay", "item": "Nope"}, "item", "Nope"),
                                ({"do": "filter.set", "source": "Cam", "filter": "Nope", "on": True}, "filter", "Nope")):
        error = answer(api, request)["error"]
        assert error["code"] == "not_found" and error["what"] == what and error["name"] == name, request
        assert name in error["message"]


def test_events_for_lists_and_effects():
    from sprigcast.control.native import event_group

    api, host = _native()
    col = host.collection
    api.changes()  # first look
    _ok(api, "bloom.new", name="Overlay")
    _ok(api, "source.new", name="Hydrate", kind="browser", settings={"url": "http://x"})
    _ok(api, "item.add", bloom="Overlay", source="Hydrate")
    events = dict(api.changes())
    assert events["sources.changed"]["added"] == ["Hydrate"]
    assert events["items.changed"]["blooms"] == ["Overlay"]
    assert [i["item"] for i in events["items.changed"]["items"]] == ["Hydrate"]
    _ok(api, "source.rename", source="Hydrate", name="Water")
    assert dict(api.changes())["sources.changed"]["renamed"] == [{"from": "Hydrate", "to": "Water"}]
    # An effect switching off (its time was up) says so; one swapped for another says replaced.
    from sprigcast.core.scene import Filter

    cam = col.source("cam")
    cam.filters += [Filter(id="f-px", kind="pixelate", name="Pixelate", enabled=True),
                    Filter(id="f-bo", kind="blackout", name="Blackout", enabled=False)]
    api.changes()
    cam.filters[-2].enabled = False
    assert ("effect.ended", {"source": "Cam", "sourceId": "cam", "effect": "hide.pixelate", "reason": "ended"}) \
        in api.changes()
    cam.filters[-2].enabled = True
    api.changes()
    cam.filters[-2].enabled, cam.filters[-1].enabled = False, True
    ended = [e for n, e in api.changes() if n == "effect.ended"]
    assert ended == [{"source": "Cam", "sourceId": "cam", "effect": "hide.pixelate", "reason": "replaced"}]
    assert (event_group("effect.ended"), event_group("sources.changed"), event_group("items.changed"),
            event_group("plugins.changed")) == ("filter", "source", "item", "plugin")


def test_pages_see_where_they_are():
    from sprigcast.app import browser_host

    shim = browser_host.shim()
    for name in ("window.sprigcast", "obsSceneChanged", "sprigcastVisibleChanged", "sprigcastStateChanged",
                 "obsSourceVisibleChanged", "obsStreamingStarted", "obsRecordingStopped", '"SprigCast"'):
        assert name in shim
    assert "__VERSION__" not in shim
    import types

    host = types.SimpleNamespace(_state={"blooms": {"horizontal": "Chatting", "vertical": "Shorts"},
                                         "shapes": {"web": "vertical"}, "streaming": True, "recording": False})
    page_state = browser_host.BrowserHost.page_state
    assert page_state(host, "web") == {"bloom": "Shorts", "blooms": {"horizontal": "Chatting", "vertical": "Shorts"},
                                      "shape": "vertical", "streaming": True, "recording": False}
    assert page_state(host, "other")["bloom"] == "Chatting"


def test_main_window_page_state():
    import types

    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import SceneItem, SourceConfig

    col = _host().collection
    col.sources.append(SourceConfig(id="web", kind="browser", name="Web"))
    col.scene("p-frame").items.append(SceneItem(source="web"))  # inside the Frame sprig, shown in Chatting
    window = types.SimpleNamespace(collection=col, streaming=None, recording=object())
    state = MainWindow._page_state(window)
    assert state["blooms"] == {"horizontal": "Chatting", "vertical": "Chatting"}
    assert state["shapes"] == {"web": "horizontal"} and state["recording"] and not state["streaming"]


def test_record_paused_event_and_status():
    """record.paused (output group) when a recording pauses or carries on, for Stream Deck's Pause recording key."""
    from sprigcast.control.native import FEATURES, NativeApi, event_group

    host = FakeHost(_collection())
    api = NativeApi(host)
    api.changes()
    host.rec = {"seconds": 3.0, "path": "x.mkv", "paused": False}
    assert ("record.changed", {"on": True}) in api.changes()
    host.rec = {**host.rec, "paused": True}
    assert api.changes() == [("record.paused", {"paused": True})]
    assert api.run("record.status", {})["paused"] is True
    host.rec = {**host.rec, "paused": False}
    assert api.changes() == [("record.paused", {"paused": False})]
    host.rec = None
    assert api.changes() == [("record.changed", {"on": False})]  # stopping says nothing about pausing
    assert event_group("record.paused") == "output" and "record.pause" in FEATURES


def test_place_set_switches_twitch_test_mode():
    """place.set bandwidth_test: test tooling switches Twitch's test mode without the window."""
    from sprigcast.control.native import FEATURES

    api, host = _native()
    col = host.collection
    out = col.outputs[0]
    out.service = "twitch"
    other = col.outputs[1]
    other.service = "youtube"
    places = {p["id"]: p for p in _ok(api, "places.list")["places"]}
    assert places[out.id]["bandwidth_test"] is False and "bandwidth_test" not in places[other.id]
    _ok(api, "place.set", place=out.id, bandwidth_test=True)
    assert out.bandwidth_test is True and "places" in host.commits
    _ok(api, "place.set", place=out.id, on=False)  # on alone still works, test mode kept
    assert out.enabled is False and out.bandwidth_test is True
    assert _ok(api, "places.list")["places"][0]["bandwidth_test"] is True
    assert _fails(api, "place.set", place=other.id, bandwidth_test=True) == "bad_request"  # YouTube has none
    assert _fails(api, "place.set", place=out.id) == "bad_request"  # nothing to change
    _ok(api, "place.set", place=out.id, bitrateKbps=4500)
    assert out.bitrate_kbps == 4500
    assert next(p for p in _ok(api, "places.list")["places"] if p["id"] == out.id)["bitrateKbps"] == 4500
    assert _fails(api, "place.set", place=out.id, bitrateKbps=100) == "bad_request"
    assert _fails(api, "place.set", place=out.id, bitrateKbps="4500") == "bad_request"
    # places.list says what each place sends: its own size and rate, else its shape's; Enhanced on Twitch.
    canvas = next(c for c in col.canvases if c.id == out.canvas)
    places = {p["id"]: p for p in _ok(api, "places.list")["places"]}
    assert places[out.id]["size"] == [canvas.width, canvas.height] and places[out.id]["fps"] == canvas.fps
    assert places[out.id]["enhanced"] is False and "enhanced" not in places[other.id]
    out.size, out.fps, out.enhanced = (1280, 720), 60, True
    place = next(p for p in _ok(api, "places.list")["places"] if p["id"] == out.id)
    assert (place["size"], place["fps"], place["enhanced"]) == ([1280, 720], 60, True)
    assert "places.size" in FEATURES
    assert _fails(api, "place.set", place=out.id, bandwidth_test="yes") == "bad_request"
    host.start_stream()
    assert _fails(api, "place.set", place=out.id, bandwidth_test=False) == "busy"
    assert "place.bandwidth_test" in FEATURES


def test_places_list_says_which_places_are_ready():
    """A place added from + Add stays off until it has its key (#795): an app sees why (`problem`), not just off."""
    from sprigcast.control.native import NativeApi

    col = _collection()
    col.outputs = [Output(id="o1", type="stream", canvas="horizontal", service="twitch", enabled=True),
                   Output(id="o2", type="stream", canvas="horizontal", service="facebook", enabled=False)]
    host = FakeHost(col)
    api = NativeApi(host)
    first = _ok(api, "places.list")["places"]
    assert [(p["name"], p["ready"], p["problem"]) for p in first] == [("Twitch", True, None), ("Facebook", True, None)]
    host.place_problem = lambda output_id: "Needs a stream key" if output_id == "o2" else None
    second = _ok(api, "places.list")["places"][1]
    assert second["ready"] is False and second["problem"] == "Needs a stream key" and second["service"] == "facebook"
    assert "places.ready" in _ok(api, "app.info")["features"]


def test_source_get_says_what_a_loaded_source_is_doing():
    """PC 2026-11-21: nothing but the log said whether a capture card had opened, or at what size. source.get
    says: loaded, state, size, and for a camera the size the device gives."""
    from types import SimpleNamespace

    from sprigcast.app.control_server import WindowHost
    from sprigcast.control.native import FEATURES

    api, host = _native()
    source = _ok(api, "source.new", name="Card", kind="camera", settings={})
    got = _ok(api, "source.get", source="Card")
    assert got["loaded"] is False and "state" not in got  # its bloom hasn't been on screen
    feed = SimpleNamespace(picture=(2560, 1440), status="capturing 2560x1440")
    live = SimpleNamespace(status="starting", asleep=False, _size=(1920, 1080), _feed=feed)
    window = SimpleNamespace(engine=SimpleNamespace(_sources={source["id"]: live}))
    host.source_now = lambda source_id: WindowHost.source_now(SimpleNamespace(w=window), source_id)
    got = _ok(api, "source.get", source="Card")
    assert (got["loaded"], got["state"], got["size"], got["cameraSize"], got["asleep"]) == (
        True, "capturing 2560x1440", [1920, 1080], [2560, 1440], False)
    assert "source.state" in FEATURES
