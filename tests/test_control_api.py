"""Remote control over obs-websocket 5: the wire protocol, what requests do, events, and the Qt server."""

import json
import os

import pytest

from sprigcast.control.api import ControlApi, scene_names
from sprigcast.control.protocol import Close, CloseCode, Session, Sleep, Status, Sub, auth_string
from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig, SprigState


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=64, height=36), Canvas(id="vertical", width=36, height=64)],
        sources=[
            SourceConfig(id="mic", kind="audio_input", name="Mic", settings={"volume_db": -6.0}),
            SourceConfig(id="desk", kind="audio_output", name="Desktop audio"),
            SourceConfig(id="cam", kind="color", name="Cam"),
            SourceConfig(id="alert", kind="color", name="Alert"),
            SourceConfig(id="frame-h", kind="scene", name="Frame", settings={"scene": "p-frame"}),
            SourceConfig(id="frame-v", kind="scene", name="Frame", settings={"scene": "p-frame"}),
        ],
        scenes=[
            Scene(id="chat-h", name="Chatting", canvas="horizontal",
                  items=[SceneItem(source="cam"), SceneItem(source="frame-h"), SceneItem(source="alert")]),
            Scene(id="chat-v", name="Chatting", canvas="vertical", linked_to="chat-h",
                  items=[SceneItem(source="cam"), SceneItem(source="frame-v"), SceneItem(source="alert")]),
            Scene(id="brb-h", name="BRB", canvas="horizontal"),
            Scene(id="brb-v", name="BRB", canvas="vertical", linked_to="brb-h"),
            Scene(id="solo-v", name="Shorts", canvas="vertical"),
            Scene(id="p-frame", name="Frame", canvas="horizontal", kind="part",
                  items=[SceneItem(source="cam")], states=[SprigState(name="Quiet", hide=["cam"])]),
        ],
        active_scenes={"horizontal": "chat-h", "vertical": "chat-v"},
    )


class FakeHost:
    def __init__(self, collection):
        self.collection = collection
        self.commits = []
        self.live = None
        self.rec = None
        self.clips = False
        self.saved = []
        self.shots = []
        self.media = {}  # source id -> info, for loaded media

    def commit(self, what):
        self.commits.append(what)

    def stream_status(self):
        return self.live

    def record_status(self):
        return self.rec

    def clips_ready(self):
        return self.clips

    def start_stream(self):
        self.live = {"seconds": 0.0}

    def stop_stream(self):
        self.live = None

    def start_record(self):
        self.rec = {"seconds": 0.0, "path": "C:/rec.mkv"}

    def stop_record(self):
        self.rec = None

    def set_clips(self, on):
        self.clips = on

    def save_clip(self):
        self.saved.append("clip.mkv")
        return ["clip.mkv"]

    def source_sizes(self):
        return {"cam": (16, 9)}

    def media_info(self, source_id):
        return dict(self.media[source_id]) if source_id in self.media else None

    def media_control(self, source_id, what, seconds=None):
        if source_id not in self.media:
            return None
        info = self.media[source_id]
        info["state"] = {"play": "playing", "pause": "paused", "restart": "playing", "stop": "stopped",
                         "toggle": "paused" if info["state"] == "playing" else "playing"}.get(what, info["state"])
        info["position"] = 0.0 if what in ("restart", "stop") else seconds if what == "seek" else info["position"]
        return dict(info)

    def screenshot(self, shapes, width, save, as_data):
        self.shots.append((shapes, width, save, as_data))
        out = []
        for shape in shapes:
            shot = {"shape": shape, "width": width or 1920, "height": 1080}
            if save:
                shot["path"] = f"Screenshots/screenshot {shape}.png"
            if as_data:
                shot["png"] = "iVBOR"
            out.append(shot)
        return out

    def stats(self):
        return {"activeFps": 60.0}

    def hotkeys(self):
        return [("toggle_stream", "Go live")]

    def trigger_hotkey(self, action):
        self.triggered = action


def _api():
    host = FakeHost(_collection())
    return ControlApi(host), host


def _ok(api, request, data=None):
    from sprigcast.control.protocol import RequestError

    try:
        return api.handle(request, data or {})
    except RequestError as exc:
        raise AssertionError(f"{request} failed: {exc.code} {exc.comment}") from None


def _fails(api, request, data=None):
    from sprigcast.control.protocol import RequestError

    with pytest.raises(RequestError) as info:
        api.handle(request, data or {})
    return info.value.code


# Protocol --------------------------------------------------------------------------------


def _messages(steps):
    return [json.loads(s) if isinstance(s, str) else s for s in steps]


def test_hello_password_and_identify():
    session = Session(handler=lambda t, d: {"echo": t}, password="leafy")
    hello = json.loads(session.hello())
    assert hello["op"] == 0 and hello["d"]["rpcVersion"] == 1
    auth = hello["d"]["authentication"]
    # Requests before Identify close the connection.
    assert _messages(session.receive(json.dumps({"op": 6, "d": {"requestType": "GetVersion", "requestId": "1"}}))) == [
        Close(CloseCode.NOT_IDENTIFIED, "Send Identify first.")]
    wrong = _messages(session.receive(json.dumps({"op": 1, "d": {"rpcVersion": 1, "authentication": "nope"}})))
    assert wrong[0].code == CloseCode.AUTHENTICATION_FAILED and not session.identified
    good = auth_string("leafy", auth["salt"], auth["challenge"])
    out = _messages(session.receive(json.dumps({"op": 1, "d": {"rpcVersion": 1, "authentication": good,
                                                               "eventSubscriptions": Sub.SCENES}})))
    assert out == [{"op": 2, "d": {"negotiatedRpcVersion": 1}}] and session.identified
    assert session.event("StreamStateChanged", Sub.OUTPUTS, {}) is None  # not subscribed
    assert json.loads(session.event("CurrentProgramSceneChanged", Sub.SCENES, {"sceneName": "x"}))["op"] == 5
    again = _messages(session.receive(json.dumps({"op": 1, "d": {"rpcVersion": 1}})))
    assert again[0].code == CloseCode.ALREADY_IDENTIFIED
    assert _messages(session.receive("not json"))[0].code == CloseCode.MESSAGE_DECODE_ERROR


def test_no_password_and_requests():
    session = Session(handler=lambda t, d: {"got": d.get("x")} if t == "Echo" else None)
    assert "authentication" not in json.loads(session.hello())["d"]
    list(session.receive(json.dumps({"op": 1, "d": {"rpcVersion": 1}})))
    [response] = _messages(session.receive(json.dumps(
        {"op": 6, "d": {"requestType": "Echo", "requestId": "a", "requestData": {"x": 3}}})))
    assert response["op"] == 7
    assert response["d"] == {"requestType": "Echo", "requestId": "a",
                             "requestStatus": {"result": True, "code": 100}, "responseData": {"got": 3}}


def test_batch_with_sleep_and_halt():
    from sprigcast.control.protocol import RequestError

    def handler(t, d):
        if t == "Fail":
            raise RequestError(Status.RESOURCE_NOT_FOUND, "nope")
        return {"t": t}

    session = Session(handler=handler, frame_seconds=0.5)
    list(session.receive(json.dumps({"op": 1, "d": {"rpcVersion": 1}})))
    batch = {"op": 8, "d": {"requestId": "b", "haltOnFailure": True, "requests": [
        {"requestType": "A", "requestId": "1"},
        {"requestType": "Sleep", "requestData": {"sleepMillis": 250}},
        {"requestType": "Fail"},
        {"requestType": "B"},
    ]}}
    steps = list(session.receive(json.dumps(batch)))
    assert steps[0] == Sleep(0.25)
    done = json.loads(steps[1])
    assert done["op"] == 9 and [r["requestStatus"]["code"] for r in done["d"]["results"]] == [100, 100, 600]
    frames = list(session.receive(json.dumps({"op": 8, "d": {"requestId": "c", "executionType": 1, "requests": [
        {"requestType": "Sleep", "requestData": {"sleepFrames": 4}}]}})))
    assert frames[0] == Sleep(2.0)


# Requests ---------------------------------------------------------------------------------


def test_scene_names_and_list():
    api, host = _api()
    names = [(n, s.id) for n, s in scene_names(host.collection)]
    # Linked vertical twins answer to the horizontal name; an unlinked vertical bloom is listed;
    # the sprig's name clashes with nothing here.
    assert names == [("Chatting", "chat-h"), ("BRB", "brb-h"), ("Shorts", "solo-v"), ("Frame", "p-frame")]
    listing = _ok(api, "GetSceneList")
    assert listing["currentProgramSceneName"] == "Chatting" and listing["currentPreviewSceneName"] is None
    assert [s["sceneName"] for s in listing["scenes"]] == ["Frame", "Shorts", "BRB", "Chatting"]
    assert listing["scenes"][0]["sceneIndex"] == 0


def test_switching_blooms_switches_twins():
    api, host = _api()
    _ok(api, "SetCurrentProgramScene", {"sceneName": "BRB"})
    assert host.collection.active_scenes == {"horizontal": "brb-h", "vertical": "brb-v"}
    assert host.commits == ["program"]
    assert _ok(api, "GetCurrentProgramScene")["sceneName"] == "BRB"
    assert _fails(api, "SetCurrentProgramScene", {"sceneName": "Frame"}) == Status.INVALID_RESOURCE_TYPE
    assert _fails(api, "SetCurrentProgramScene", {"sceneName": "Nope"}) == Status.RESOURCE_NOT_FOUND
    _ok(api, "SetCurrentProgramScene", {"sceneUuid": "chat-h"})
    assert host.collection.active_scenes["vertical"] == "chat-v"
    # Vendor: one canvas only.
    _ok(api, "CallVendorRequest", {"vendorName": "sprigcast", "requestType": "SetCanvasProgramScene",
                                   "requestData": {"sceneName": "Shorts"}})
    assert host.collection.active_scenes == {"horizontal": "chat-h", "vertical": "solo-v"}


def test_items_show_and_hide_in_both_shapes():
    api, host = _api()
    col = host.collection
    alert = _ok(api, "GetSceneItemId", {"sceneName": "Chatting", "sourceName": "Alert"})["sceneItemId"]
    _ok(api, "SetSceneItemEnabled", {"sceneName": "Chatting", "sceneItemId": alert, "sceneItemEnabled": False})
    assert not col.scene("chat-h").items[2].visible and not col.scene("chat-v").items[2].visible
    assert _ok(api, "GetSceneItemEnabled", {"sceneName": "Chatting", "sceneItemId": alert}) == {"sceneItemEnabled": False}
    # A sprig placed in both shapes has two placement sources: both follow.
    frame = _ok(api, "GetSceneItemId", {"sceneName": "Chatting", "sourceName": "Frame"})["sceneItemId"]
    _ok(api, "SetSceneItemEnabled", {"sceneName": "Chatting", "sceneItemId": frame, "sceneItemEnabled": False})
    assert not col.scene("chat-v").items[1].visible
    # Items inside a sprig, addressed like a nested scene.
    items = _ok(api, "GetSceneItemList", {"sceneName": "Frame"})["sceneItems"]
    assert [i["sourceName"] for i in items] == ["Cam"] and items[0]["sceneItemTransform"]["sourceWidth"] == 16
    listed = _ok(api, "GetSceneItemList", {"sceneName": "Chatting"})["sceneItems"]
    assert listed[1]["sourceType"] == "OBS_SOURCE_TYPE_SCENE" and listed[1]["sourceUuid"] == "p-frame"
    # Ids stay the same when items are reordered.
    col.scene("chat-h").items.reverse()
    assert _ok(api, "GetSceneItemId", {"sceneName": "Chatting", "sourceName": "Alert"})["sceneItemId"] == alert
    assert _fails(api, "GetSceneItemId", {"sceneName": "Chatting", "sourceName": "Mic"}) == Status.RESOURCE_NOT_FOUND
    _ok(api, "SetSceneItemTransform", {"sceneName": "Chatting", "sceneItemId": alert,
                                       "sceneItemTransform": {"positionX": 5, "scaleX": 2.0}})
    moved = next(i for i in col.scene("chat-h").items if i.source == "alert")
    assert moved.x == 5 and moved.scale == 2.0
    assert next(i for i in col.scene("chat-v").items if i.source == "alert").x == 0  # moving never mirrors


def test_sound_mute_and_volume():
    api, host = _api()
    mic = host.collection.source("mic")
    assert _ok(api, "GetInputVolume", {"inputName": "Mic"})["inputVolumeDb"] == -6.0
    assert _ok(api, "ToggleInputMute", {"inputName": "Mic"}) == {"inputMuted": True}
    assert mic.settings["muted"] is True
    _ok(api, "SetInputVolume", {"inputName": "Mic", "inputVolumeMul": 1.0})
    assert mic.settings["volume_db"] == 0.0
    _ok(api, "SetInputVolume", {"inputUuid": "mic", "inputVolumeDb": -12})
    assert mic.settings["volume_db"] == -12.0
    assert _fails(api, "SetInputVolume", {"inputName": "Mic", "inputVolumeDb": 40}) == Status.REQUEST_FIELD_OUT_OF_RANGE
    assert _fails(api, "SetInputMute", {"inputName": "Cam", "inputMuted": True}) == Status.INVALID_RESOURCE_STATE
    assert _fails(api, "SetInputMute", {"inputName": "Mic"}) == Status.MISSING_REQUEST_FIELD
    assert _ok(api, "GetSpecialInputs")["mic1"] == "Mic"
    kinds = {i["inputName"]: i["inputKind"] for i in _ok(api, "GetInputList")["inputs"]}
    assert kinds["Mic"] == "wasapi_input_capture" and "Frame" not in kinds  # sprigs are scenes, not inputs


def test_outputs_studio_and_vendor_requests():
    api, host = _api()
    assert _ok(api, "GetStreamStatus")["outputActive"] is False
    assert _ok(api, "ToggleStream") == {"outputActive": True}
    assert _fails(api, "StartStream") == Status.OUTPUT_RUNNING
    _ok(api, "StopStream")
    _ok(api, "StartRecord")
    assert _ok(api, "StopRecord") == {"outputPath": "C:/rec.mkv"}
    assert _fails(api, "PauseRecord") == Status.OUTPUT_NOT_RUNNING  # recordings pause now; this one has stopped
    assert _fails(api, "SaveReplayBuffer") == Status.OUTPUT_NOT_RUNNING
    # SprigCast's SaveClip starts clips when they're off.
    out = _ok(api, "CallVendorRequest", {"vendorName": "sprigcast", "requestType": "SaveClip"})
    assert out["responseData"] == {"saved": [], "started": True} and host.clips
    _ok(api, "SaveReplayBuffer")
    assert _ok(api, "GetLastReplayBufferReplay") == {"savedReplayPath": "clip.mkv"}
    # No Studio mode: OBS clients are told it's off, and nothing is lined up.
    assert _ok(api, "GetStudioModeEnabled") == {"studioModeEnabled": False}
    assert _fails(api, "SetStudioModeEnabled", {"studioModeEnabled": True}) == Status.STUDIO_MODE_NOT_ACTIVE
    _ok(api, "SetStudioModeEnabled", {"studioModeEnabled": False})
    assert _fails(api, "SetCurrentPreviewScene", {"sceneName": "BRB"}) == Status.STUDIO_MODE_NOT_ACTIVE
    assert _fails(api, "GetCurrentPreviewScene") == Status.STUDIO_MODE_NOT_ACTIVE
    assert _fails(api, "TriggerStudioModeTransition") == Status.STUDIO_MODE_NOT_ACTIVE
    assert _fails(api, "CallVendorRequest", {"vendorName": "sprigcast", "requestType": "LineUp"}) \
        == Status.UNKNOWN_REQUEST_TYPE
    assert _ok(api, "GetSceneList")["currentPreviewSceneName"] is None
    # Sprig states.
    _ok(api, "CallVendorRequest", {"vendorName": "sprigcast", "requestType": "SetSprigState",
                                   "requestData": {"sceneName": "Frame", "state": "Quiet"}})
    assert host.collection.scene("p-frame").state == "Quiet"
    states = _ok(api, "CallVendorRequest", {"vendorName": "sprigcast", "requestType": "GetSprigStates"})
    assert states["responseData"]["sprigs"][0]["state"] == "Quiet"
    assert _fails(api, "CallVendorRequest", {"vendorName": "other", "requestType": "x"}) == Status.RESOURCE_NOT_FOUND
    assert _fails(api, "Nope") == Status.UNKNOWN_REQUEST_TYPE
    _ok(api, "TriggerHotkeyByName", {"hotkeyName": "toggle_stream"})
    assert host.triggered == "toggle_stream"
    version = _ok(api, "GetVersion")
    assert "SetCurrentProgramScene" in version["availableRequests"] and version["rpcVersion"] == 1
    _ok(api, "SetCurrentSceneTransition", {"transitionName": "Cut"})
    assert host.collection.transition.kind == "cut"


def test_events_from_changes():
    api, host = _api()
    assert api.changes() == []  # the first look only remembers
    col = host.collection
    col.set_active("brb-h")
    col.source("mic").settings["muted"] = True
    col.scene("chat-h").items[2].visible = False
    host.live = {"seconds": 1.0}
    col.set_state("p-frame", "Quiet")
    events = {(e[0], e[2].get("eventType")): e for e in api.changes()}
    assert events[("CurrentProgramSceneChanged", None)][2] == {"sceneName": "BRB", "sceneUuid": "brb-h"}
    assert events[("InputMuteStateChanged", None)][2]["inputMuted"] is True
    assert events[("SceneItemEnableStateChanged", None)][2]["sceneItemEnabled"] is False
    assert events[("StreamStateChanged", None)][2]["outputActive"] is True
    assert events[("VendorEvent", "CanvasProgramChanged")][1] == Sub.VENDORS
    assert events[("VendorEvent", "SprigStateChanged")][2]["eventData"]["state"] == "Quiet"
    assert api.changes() == []  # nothing new
    col.scenes.append(Scene(id="new-h", name="Intro", canvas="horizontal"))
    kinds = [e[0] for e in api.changes()]
    assert kinds == ["SceneCreated", "SceneListChanged"]


# The Qt server -------------------------------------------------------------------------------


def _qt():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWebSockets")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _wait(app, condition, seconds=5.0):
    import time

    end = time.monotonic() + seconds
    while not condition() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)
    return condition()


def test_server_end_to_end():
    app = _qt()
    from PySide6.QtCore import QUrl
    from PySide6.QtWebSockets import QWebSocket

    from sprigcast.app.control_server import ControlServer

    api, host = _api()
    server = ControlServer(api)
    assert server.start(0, password="secret")  # port 0: any free port
    got = []
    client = QWebSocket()
    client.textMessageReceived.connect(lambda text: got.append(json.loads(text)))
    try:
        client.open(QUrl(f"ws://127.0.0.1:{server.port}"))
        assert _wait(app, lambda: got)
        auth = got[0]["d"]["authentication"]
        client.sendTextMessage(json.dumps({"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": Sub.ALL,
                                                          "authentication": auth_string("secret", auth["salt"], auth["challenge"])}}))
        assert _wait(app, lambda: len(got) >= 2) and got[1]["op"] == 2
        client.sendTextMessage(json.dumps({"op": 6, "d": {"requestType": "SetCurrentProgramScene", "requestId": "s",
                                                          "requestData": {"sceneName": "BRB"}}}))
        assert _wait(app, lambda: any(m["op"] == 5 for m in got))
        response = next(m for m in got if m["op"] == 7)
        assert response["d"]["requestStatus"]["result"] is True
        event = next(m for m in got if m["op"] == 5 and m["d"]["eventType"] == "CurrentProgramSceneChanged")
        assert event["d"]["eventData"]["sceneName"] == "BRB"
        # A batch that sleeps answers later without blocking.
        client.sendTextMessage(json.dumps({"op": 8, "d": {"requestId": "b", "requests": [
            {"requestType": "Sleep", "requestData": {"sleepMillis": 50}}, {"requestType": "GetVersion"}]}}))
        assert _wait(app, lambda: any(m["op"] == 9 for m in got))
        assert len(server.sessions) == 1
        # Meters only go to apps that asked.
        server.meters({"mic": (-6.0, -12.0)})
        app.processEvents()
        assert not any(m["op"] == 5 and m["d"]["eventType"] == "InputVolumeMeters" for m in got)
    finally:
        client.close()
        _wait(app, lambda: not server.sessions, 2.0)
        server.stop()
    assert not server.listening


def test_main_window_runs_the_server(tmp_path, monkeypatch):
    pytest.importorskip("moderngl")
    app = _qt()
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("ControlTest")
    QSettings().clear()
    from sprigcast.app import control_server
    from sprigcast.app.main_window import MainWindow

    q = QSettings()
    q.setValue(control_server.ENABLED, True)
    q.setValue(control_server.PORT, 0)  # any free port
    w = MainWindow(_collection(), tmp_path / "scenes.json")
    try:
        assert w.control.listening
        api = w.control.api
        api.handle("SetCurrentProgramScene", {"sceneName": "BRB"})
        assert w.collection.active_scenes == {"horizontal": "brb-h", "vertical": "brb-v"}
        assert api.handle("ToggleInputMute", {"inputName": "Mic"}) == {"inputMuted": True}
        assert w.collection.source("mic").settings["muted"] is True
        assert api.handle("GetStreamStatus", {})["outputActive"] is False
        assert "toggle_stream" in api.handle("GetHotkeyList", {})["hotkeys"]
        # Turned off in Settings: the server stops.
        q.setValue(control_server.ENABLED, False)
        control_server.apply_settings(w.control, w)
        assert not w.control.listening
    finally:
        w.close()
        app.processEvents()
        QSettings().clear()


def test_connections_settings_page():
    _qt()
    from PySide6.QtCore import QCoreApplication, QSettings

    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("ConnectionsPageTest")
    QSettings().clear()
    from sprigcast.app import control_server
    from sprigcast.app.connections_page import ConnectionsPage

    page = ConnectionsPage()
    assert page.enabled.isChecked() and page.port.value() == 4455  # on by default: the Stream Deck connects by itself
    assert len(page.password.text()) == 16  # made on first look
    assert "Port: 4455" in page.connect_text()
    page.port.setValue(4460)
    page.apply()
    settings = control_server.saved_settings()
    assert settings["enabled"] and settings["port"] == 4460 and settings["use_password"]
    QSettings().clear()


def test_an_app_connecting_after_settings_closed_doesnt_touch_the_closed_page():
    """Settings closed (page deleted), then an app connected: the server called into a deleted spin box."""
    _qt()
    import gc

    from PySide6.QtCore import QCoreApplication, QEvent, QSettings
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("ConnectionsPageTest")
    from sprigcast.app.connections_page import ConnectionsPage

    class Server:
        listening, sessions, error = True, [], ""
        on_clients_changed = None

    server = Server()
    page = ConnectionsPage(server)
    server.on_clients_changed(1)  # open: it updates
    assert page.status.text().startswith("On")
    page.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    del page
    gc.collect()
    server.on_clients_changed(2)  # nothing to update, and no error
    QSettings().clear()
