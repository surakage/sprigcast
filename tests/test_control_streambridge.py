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
