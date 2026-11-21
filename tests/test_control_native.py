"""The SprigCast API: our own control language on ws://…/sprigcast."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from sprigcast.control.native import ApiError, NativeApi, NativeSession
from sprigcast.control.protocol import Close, auth_string
from sprigcast.core.scene import Filter
from test_control_api import FakeHost, _collection, _qt, _wait


def _api():
    col = _collection()
    col.source("cam").filters.append(Filter(id="f-blur", kind="color_correction", name="Colour"))
    host = FakeHost(col)
    return NativeApi(host), host


def _ok(api, name, /, **data):
    try:
        return api.run(name, data)
    except ApiError as exc:
        raise AssertionError(f"{name} failed: {exc.code} {exc.message}") from None


def _fails(api, name, /, **data):
    with pytest.raises(ApiError) as info:
        api.run(name, data)
    return info.value.code


def test_actions_describe_themselves():
    api, _ = _api()
    listed = {a["do"]: a for a in _ok(api, "app.actions")["actions"]}
    assert {"bloom.show", "item.toggle", "sound.mute", "filter.set", "source.set", "stream.toggle",
            "clip.save", "sprig.state"} <= set(listed)
    assert "bloom.lineUp" not in listed and "studio.set" not in listed  # no Studio mode
    assert listed["bloom.show"]["fields"]["shape"].startswith("?")
    assert _ok(api, "app.info")["shapes"] == ["horizontal", "vertical"]
    assert _fails(api, "nope.nothing") == "unknown_action"


def test_blooms_in_both_shapes_or_one():
    api, host = _api()
    blooms = _ok(api, "blooms.list")["blooms"]
    assert [b["name"] for b in blooms] == ["Chatting", "BRB", "Shorts"]
    assert blooms[0]["shapes"] == {"horizontal": "chat-h", "vertical": "chat-v"} and blooms[0]["onAir"] == ["horizontal", "vertical"]
    _ok(api, "bloom.show", bloom="BRB")
    assert host.collection.active_scenes == {"horizontal": "brb-h", "vertical": "brb-v"}
    _ok(api, "bloom.show", bloom="Chatting", shape="vertical")
    assert host.collection.active_scenes == {"horizontal": "brb-h", "vertical": "chat-v"}
    assert _fails(api, "bloom.show", bloom="Shorts", shape="horizontal") == "not_found"
    assert _fails(api, "bloom.show", bloom="Frame") == "not_allowed"  # a sprig
    assert _fails(api, "bloom.show", bloom="BRB", shape="sideways") == "bad_request"
    assert _fails(api, "bloom.show") == "bad_request"
    assert _fails(api, "bloom.lineUp", bloom="Chatting") == "unknown_action"  # Studio mode is gone


def test_items_and_sprigs():
    api, host = _api()
    col = host.collection
    assert _ok(api, "item.hide", bloom="Chatting", item="Alert") == {"visible": False}
    assert not col.scene("chat-h").items[2].visible and not col.scene("chat-v").items[2].visible
    _ok(api, "item.show", bloom="Chatting", item="Alert", shape="vertical")
    assert col.scene("chat-v").items[2].visible and not col.scene("chat-h").items[2].visible
    # A sprig placed in both shapes is one item (its id is the sprig's id).
    assert _ok(api, "item.toggle", bloom="Chatting", item="p-frame") == {"visible": False}
    assert not col.scene("chat-h").items[1].visible and not col.scene("chat-v").items[1].visible
    listed = _ok(api, "items.list", bloom="Chatting")["shapes"]
    assert [i["name"] for i in listed["horizontal"]] == ["Alert", "Frame", "Cam"] and listed["horizontal"][1]["sprig"]
    _ok(api, "item.hide", sprig="Frame", item="Cam")
    assert not col.scene("p-frame").items[0].visible
    assert _fails(api, "item.hide", bloom="Chatting", item="Nope") == "not_found"
    _ok(api, "sprig.state", sprig="Frame", state="Quiet")
    assert col.scene("p-frame").state == "Quiet"
    _ok(api, "sprig.state", sprig="Frame", state=None)
    assert col.scene("p-frame").state is None
    assert _fails(api, "sprig.state", sprig="Frame", state="Loud") == "not_found"


def test_sounds_filters_and_sources():
    api, host = _api()
    col = host.collection
    assert _ok(api, "sound.mute", sound="Mic")["muted"] is True
    assert _ok(api, "sound.mute", sound="Mic")["muted"] is False  # no muted: toggle
    assert _ok(api, "sound.volume", sound="Mic", change=-3)["volumeDb"] == -9.0
    assert _ok(api, "sound.volume", sound="Mic", db=100)["volumeDb"] == 26.0  # kept in range
    assert _fails(api, "sound.mute", sound="Cam") == "not_allowed"
    assert _fails(api, "sound.volume", sound="Mic", db="loud") == "bad_request"
    # Filters by name, id or kind; settings merge.
    out = _ok(api, "filter.set", source="Cam", filter="Colour", on=False, settings={"saturation": 0.4})
    assert out["on"] is False and out["settings"]["saturation"] == 0.4 and out["settings"]["gamma"] == 0.0
    assert _fails(api, "filter.set", source="Cam", filter="Colour", settings={"sparkle": 1}) == "bad_request"
    assert _ok(api, "filter.toggle", source="Cam", filter="color_correction")["on"] is True
    kinds = {k["kind"] for k in _ok(api, "filters.kinds")["kinds"]}
    assert "color_correction" in kinds
    assert _fails(api, "filter.add", source="Cam", kind="color_correction") == "not_allowed"  # one per video kind
    added = _ok(api, "filter.add", source="Mic", kind=next(k["kind"] for k in _ok(api, "filters.kinds")["kinds"]
                                                         if k["media"] == "audio"))
    assert col.source("mic").filters[-1].id == added["id"]
    _ok(api, "filter.move", source="Mic", filter=added["id"], index=0)
    _ok(api, "filter.remove", source="Mic", filter=added["id"])
    assert col.source("mic").filters == []
    _ok(api, "source.set", source="Alert", settings={"color": "#00ff00ff"})
    assert col.source("alert").settings["color"] == "#00ff00ff"
    _ok(api, "source.rename", source="Alert", name="Follow alert")
    assert col.source("alert").name == "Follow alert"


def test_outputs_and_transitions():
    api, host = _api()
    assert _ok(api, "stream.toggle") == {"on": True, "places": []}
    assert _fails(api, "stream.start") == "busy"
    assert _ok(api, "stream.status")["on"] is True
    _ok(api, "stream.stop")
    _ok(api, "record.start")
    assert _ok(api, "record.stop") == {"file": "C:/rec.mkv"}
    assert _ok(api, "clip.save") == {"saved": [], "started": True}
    assert _ok(api, "clip.save") == {"saved": ["clip.mkv"], "started": False}
    assert _ok(api, "transition.set", kind="leaf", ms=500) == {"kind": "leaf", "ms": 500, "easing": "smooth",
                                                              "unmatched": "fade"}
    assert _ok(api, "transition.set", kind="move", easing="spring", unmatched="slide")["easing"] == "spring"
    assert api.col.transition.unmatched == "slide" and api.col.transition.duration_ms == 500
    assert _fails(api, "transition.set", easing="bouncy") == "bad_request"
    assert _fails(api, "transition.set", unmatched="melt") == "bad_request"
    assert {"easings", "unmatched_choices"} <= set(_ok(api, "transition.get"))
    _ok(api, "transition.set", kind="leaf")
    assert _fails(api, "transition.set", kind="spin") == "bad_request"


def test_events_in_our_words():
    api, host = _api()
    col = host.collection
    assert api.changes() == []
    col.set_active("brb-h")
    col.source("mic").settings["muted"] = True
    col.source("cam").filters[0].enabled = False
    col.scene("chat-v").items[2].visible = False
    host.live = {"seconds": 1.0}
    events = {name: data for name, data in api.changes()}
    assert events["bloom.shown"]["bloom"] == "BRB"
    assert events["sound.changed"]["muted"] is True
    assert events["filter.changed"]["on"] is False and events["filter.changed"]["source"] == "Cam"
    assert events["item.changed"] == {"bloom": "Chatting", "bloomId": "chat-h", "shape": "vertical", "item": "Alert",
                                      "itemId": "alert", "visible": False}
    assert events["stream.changed"] == {"on": True}
    assert api.changes() == []
    # The half-second watch: the settings as last written out (an edit or a request asks for a full look).
    col.source("mic").settings["gate"] = 0.5
    assert api.changes(full=False) == []
    assert [name for name, _ in api.changes()] == ["source.changed"]
    col.sources.remove(col.source("mic"))
    api.changes(full=False)  # a source gone between two looks is never named from the old one


def test_a_stream_ended_and_started_again_between_two_looks_says_both():
    api, host = _api()
    host.stream_key = lambda: host.key
    host.key, host.live = 1, {"seconds": 1.0}
    api.changes()
    assert [e for e in api.changes() if e[0] == "stream.changed"] == []
    host.key = 2  # stopped and started again before the next look
    assert [e for e in api.changes() if e[0] == "stream.changed"] == [("stream.changed", {"on": False}),
                                                                       ("stream.changed", {"on": True})]
    host.key, host.live = 0, None
    assert [e for e in api.changes() if e[0] == "stream.changed"] == [("stream.changed", {"on": False})]


def test_session_password_and_requests():
    api, _ = _api()
    session = NativeSession(api=api, password="leafy")
    hello = json.loads(session.hello())["hello"]
    assert hello["app"] == "SprigCast" and hello["auth"]
    wrong = list(session.receive(json.dumps({"auth": "nope"})))
    assert isinstance(wrong[0], Close) and wrong[0].code == 4009
    answer = auth_string("leafy", hello["auth"]["salt"], hello["auth"]["challenge"])
    out = [json.loads(m) for m in session.receive(json.dumps({"auth": answer, "events": ["bloom"],
                                                              "id": "1", "do": "bloom.show", "bloom": "BRB"}))]
    assert out[0]["welcome"]["events"] == ["bloom"]
    assert out[1]["id"] == "1" and out[1]["ok"] is True
    [fail] = [json.loads(m) for m in session.receive(json.dumps({"id": 2, "do": "bloom.show", "bloom": "Nope"}))]
    assert fail == {"id": 2, "ok": False, "error": {"code": "not_found", "message": "No bloom called 'Nope'.",
                                                    "what": "bloom", "name": "Nope"}}
    assert session.event("sound.changed", {}) is None  # not subscribed
    assert json.loads(session.event("bloom.shown", {"bloom": "BRB"})) == {"event": "bloom.shown", "bloom": "BRB"}


def test_both_doors_on_one_port():
    app = _qt()
    from PySide6.QtCore import QUrl
    from PySide6.QtWebSockets import QWebSocket

    from sprigcast.app.control_server import ControlServer
    from sprigcast.control.api import ControlApi

    host = FakeHost(_collection())
    server = ControlServer(ControlApi(host), native=NativeApi(host))
    assert server.start(0)
    ours, obs = QWebSocket(), QWebSocket()
    got_ours, got_obs = [], []
    ours.textMessageReceived.connect(lambda t: got_ours.append(json.loads(t)))
    obs.textMessageReceived.connect(lambda t: got_obs.append(json.loads(t)))
    try:
        ours.open(QUrl(f"ws://127.0.0.1:{server.port}/sprigcast"))
        obs.open(QUrl(f"ws://127.0.0.1:{server.port}"))
        assert _wait(app, lambda: got_ours and got_obs)
        assert "hello" in got_ours[0] and got_obs[0]["op"] == 0
        obs.sendTextMessage(json.dumps({"op": 1, "d": {"rpcVersion": 1}}))
        ours.sendTextMessage(json.dumps({"events": "all"}))
        assert _wait(app, lambda: len(got_ours) >= 2 and len(got_obs) >= 2)
        ours.sendTextMessage(json.dumps({"id": "a", "do": "bloom.show", "bloom": "BRB"}))
        # Our app gets the answer and the event in our words; the OBS app hears it in OBS words.
        assert _wait(app, lambda: any(m.get("event") == "bloom.shown" for m in got_ours))
        assert _wait(app, lambda: any(m.get("op") == 5 and m["d"]["eventType"] == "CurrentProgramSceneChanged"
                                      for m in got_obs))
        assert next(m for m in got_ours if m.get("id") == "a")["ok"] is True
        # With compatibility mode off, new OBS-style connections are turned away.
        server.obs_door = False
        late, closed = QWebSocket(), []
        late.disconnected.connect(lambda: closed.append(late.closeCode()))
        late.open(QUrl(f"ws://127.0.0.1:{server.port}"))
        assert _wait(app, lambda: closed)
        assert len(server._natives()) == 1
    finally:
        ours.close()
        obs.close()
        _wait(app, lambda: not server.sessions, 2.0)
        server.stop()


def test_plugins_add_actions(monkeypatch):
    from sprigcast.control import native

    monkeypatch.setattr(native, "_PLUGIN_ACTIONS", {})
    native.register_action("confetti.burst", "Throw confetti.", {"amount": "?how much"},
                           lambda api, data: {"blooms": len(api.col.scenes), "amount": data.get("amount", 1)})
    api, _ = _api()
    assert _ok(api, "confetti.burst", amount=3) == {"blooms": 6, "amount": 3}
    assert any(a["do"] == "confetti.burst" for a in _ok(api, "app.actions")["actions"])
    with pytest.raises(ValueError):
        native.register_action("nodot", "x", {}, lambda api, data: None)


def test_step_lists_wait_between_actions():
    from sprigcast.control.protocol import Sleep

    api, host = _api()
    session = NativeSession(api=api)
    list(session.receive(json.dumps({"events": "none"})))
    steps = list(session.receive(json.dumps({"id": "s", "steps": [
        {"do": "item.hide", "bloom": "Chatting", "item": "Alert"},
        {"wait": 5000},
        {"do": "item.show", "bloom": "Chatting", "item": "Alert"},
    ]})))
    assert steps[0] == Sleep(5.0)
    done = json.loads(steps[1])
    assert done["id"] == "s" and done["ok"] and [r["ok"] for r in done["result"]["steps"]] == [True, True, True]
    assert host.collection.scene("chat-h").items[2].visible
    # Stops at the first failure unless told not to.
    [stopped] = [json.loads(m) for m in session.receive(json.dumps({"steps": [
        {"do": "bloom.show", "bloom": "Nope"}, {"do": "bloom.show", "bloom": "BRB"}]}))]
    assert not stopped["ok"] and len(stopped["result"]["steps"]) == 1
    assert host.collection.active_scenes["horizontal"] == "chat-h"
    [bad] = [json.loads(m) for m in session.receive(json.dumps({"steps": [{"wait": 11 * 60 * 1000}]}))]
    assert bad["error"]["code"] == "bad_request"


def test_items_move_and_reorder():
    api, host = _api()
    col = host.collection
    out = _ok(api, "item.place", bloom="Chatting", item="Alert", shape="vertical", x=10, y=20, scale=0.5)
    assert out["items"] == [{"x": 10.0, "y": 20.0, "scale": 0.5, "rotation": 0.0}]
    assert col.scene("chat-h").items[2].x == 0  # the other shape stays
    _ok(api, "item.place", bloom="Chatting", item="Alert", shape="vertical", dx=5, dy=-5)
    assert (col.scene("chat-v").items[2].x, col.scene("chat-v").items[2].y) == (15.0, 15.0)
    assert _fails(api, "item.place", bloom="Chatting", item="Alert") == "bad_request"
    _ok(api, "item.order", bloom="Chatting", item="Alert", to="bottom")
    assert col.scene("chat-h").items[0].source == "alert" and col.scene("chat-v").items[0].source == "alert"
    _ok(api, "item.order", bloom="Chatting", item="Alert", to="up", shape="horizontal")
    assert col.scene("chat-h").items[1].source == "alert" and col.scene("chat-v").items[0].source == "alert"
    assert _fails(api, "item.order", bloom="Chatting", item="Alert", to="sideways") == "bad_request"


def test_screenshots_per_shape():
    api, host = _api()
    shots = _ok(api, "screenshot.take")["shots"]
    assert [s["shape"] for s in shots] == ["horizontal", "vertical"] and all("path" in s for s in shots)
    small = _ok(api, "screenshot.take", shape="vertical", width=320, save=False, data=True)["shots"]
    assert small == [{"shape": "vertical", "width": 320, "height": 1080, "png": "iVBOR"}]
    assert host.shots[-1] == (["vertical"], 320, False, True)
    assert _fails(api, "screenshot.take", save=False) == "bad_request"
    assert _fails(api, "screenshot.take", width=4) == "bad_request"
    assert _fails(api, "screenshot.take", shape="square") == "bad_request"
    host.screenshot = lambda *a: []
    assert _fails(api, "screenshot.take") == "not_allowed"


def test_screenshot_grabs_the_next_frame(tmp_path):
    _qt()
    from sprigcast.app import screenshot
    from sprigcast.render.engine import Frame

    class Engine:
        def __init__(self):
            self.taps = {}

        def add_frame_tap(self, canvas_id, callback, nv12_size=None):
            self.taps[canvas_id] = callback
            if canvas_id == "shot:horizontal":  # only this canvas is drawing (screenshots tap its "shot:" copy)
                callback(Frame(canvas_id, 4, 2, bytes([0, 200, 0, 255]) * 8))

        def remove_frame_tap(self, canvas_id, callback):
            assert self.taps.pop(canvas_id) is callback

    engine = Engine()
    shots = screenshot.take(engine, ["horizontal", "vertical"], tmp_path, width=None, as_data=True)
    assert len(shots) == 1 and shots[0]["shape"] == "horizontal" and (shots[0]["width"], shots[0]["height"]) == (4, 2)
    saved = Path(shots[0]["path"])
    assert saved.parent.parent == tmp_path and saved.suffix == ".png" and saved.exists()  # a folder per day
    assert shots[0]["png"].startswith("iVBOR")  # PNG magic, base64
    assert engine.taps == {}  # taps removed again
    when = datetime(2026, 11, 14, 9, 40, 0)
    first = screenshot.path_for(tmp_path, "vertical", when)
    first.write_bytes(b"")
    assert screenshot.path_for(tmp_path, "vertical", when).name == "screenshot vertical 2026-11-14 09-40-00 (2).png"


def test_media_controls_and_events():
    from sprigcast.core.scene import SourceConfig

    api, host = _api()
    host.collection.sources.append(SourceConfig(id="m1", kind="media", name="Intro", settings={"path": "intro.mp4"}))
    host.collection.sources.append(SourceConfig(id="m2", kind="media", name="Music", settings={"path": "song.mp3"}))
    assert _ok(api, "media.status", media="Intro")["state"] == "unloaded"
    assert _fails(api, "media.play", media="Intro") == "not_allowed"  # its bloom was never on screen
    host.media["m1"] = {"state": "playing", "position": 1.0, "duration": 10.0, "video": True}
    host.media["m2"] = {"state": "stopped", "position": 0.0, "duration": 180.0, "video": False}
    api.changes()
    assert _ok(api, "media.pause", media="Intro")["state"] == "paused"
    assert ("media.changed", {"media": "Intro", "id": "m1", "file": "intro.mp4", "loop": False, "state": "paused",
                              "position": 1.0, "duration": 10.0, "video": True}) in api.changes()
    assert _ok(api, "media.toggle", media="m1")["state"] == "playing"
    assert _ok(api, "media.seek", media="Intro", change=2.5)["position"] == 3.5
    assert _ok(api, "media.seek", media="Intro", seconds=-4)["position"] == 0.0
    assert _fails(api, "media.seek", media="Intro") == "bad_request"
    assert _ok(api, "media.restart", media="Music")["state"] == "playing"
    assert _ok(api, "media.stop", media="Music")["state"] == "stopped"
    assert _ok(api, "media.loop", media="Music")["loop"] is True and "sources" in host.commits
    assert [m["media"] for m in _ok(api, "media.list")["media"]] == ["Intro", "Music"]
    assert _fails(api, "media.play", media="Cam") == "not_allowed"  # a camera isn't media
    assert _ok(api, "sound.mute", sound="Music")["muted"] is True  # media sound is in the Sound card


def test_app_info_lists_only_the_shapes_that_are_on():
    api, _ = _api()
    api.col.canvas("vertical").enabled = False
    assert _ok(api, "app.info")["shapes"] == ["horizontal"]


def test_camera_on_off_and_event():
    from sprigcast.control.native import NativeApi

    host = FakeHost(_collection())
    host.cam = False
    host.camera_on = lambda: host.cam

    def set_camera(on):
        host.cam = on

    host.set_camera = set_camera
    api = NativeApi(host)
    api.changes()
    assert api.run("camera.set", {}) == {"on": True}
    assert ("camera.changed", {"on": True}) in api.changes()
    assert api.run("camera.status", {}) == {"on": True}
    assert api.run("camera.set", {"on": False}) == {"on": False}


def test_the_half_second_watch_writes_settings_out_only_after_a_change(monkeypatch):
    """PC heavy run 2026-11-17: every source's settings written out as JSON on each look held Python's lock
    13 ms at a time. The watch now reuses them unless something was edited or asked (or every 3 s)."""
    from sprigcast.app import control_server
    from sprigcast.control import native as native_mod

    dumps = []
    real = native_mod.json.dumps
    monkeypatch.setattr(native_mod.json, "dumps", lambda *a, **k: dumps.append(1) or real(*a, **k))

    class Api:
        def __init__(self):
            self.calls = []

        def changes(self, full=True):
            self.calls.append(full)
            return []

    server = control_server.ControlServer.__new__(control_server.ControlServer)
    server.native, server._notify_queued, server._edited = Api(), False, True
    server._obs, server._natives = (lambda: []), (lambda: [object()])
    server.notify(full=False)  # edited since the last look: a full one
    for _ in range(control_server.FULL_EVERY - 1):
        server.notify(full=False)
    server.notify(full=False)  # every FULL_EVERY watches: a full one anyway
    server._edited = True
    server.notify(full=False)
    assert server.native.calls == [True] + [False] * (control_server.FULL_EVERY - 1) + [True, True]


def test_only_asking_never_starts_a_full_look():
    """PC native run 2026-11-17: an app reconnecting 23 times a minute to ask a status started a full look (every
    source's settings as JSON on the window's thread) each time; only a message that may change something does."""
    from sprigcast.app import control_server
    from sprigcast.control.native import may_change

    assert not may_change({"do": "record.status"}) and not may_change({"do": "sources.list"})
    assert not may_change({"auth": "x"}) and not may_change({"events": "all"})
    assert may_change({"do": "record.start"}) and may_change({"do": "item.hide", "item": "Cam"})
    assert not may_change({"steps": [{"do": "stream.status"}, {"wait": 10}]})
    assert may_change({"steps": [{"do": "stream.status"}, {"do": "bloom.show", "bloom": "BRB"}]})

    class Socket:
        def __init__(self):
            self.sent = []

        def sendTextMessage(self, text):
            self.sent.append(text)

    server = control_server.ControlServer.__new__(control_server.ControlServer)
    server._notify_queued, server._edited = False, False
    looks = []
    server.soon = lambda: looks.append(1)
    socket = Socket()
    session = NativeSession(api=NativeApi(FakeHost(_collection())))
    server.sessions = {socket: session}
    server._received(socket, json.dumps({"events": "all"}))
    server._received(socket, json.dumps({"do": "record.status", "id": 1}))
    assert socket.sent and looks == []
    server._received(socket, json.dumps({"do": "record.start", "id": 2}))
    assert looks


def test_sound_changes_are_logged_with_the_asking_app(caplog):
    """PC, 2026-11-20: 38 pages went silent and the log couldn't say which connected app did it."""
    import logging

    from sprigcast.control.who import asking

    api, _host = _api()
    with caplog.at_level(logging.INFO, logger="sprigcast.control"), asking("StreamBridge (a SprigCast API app)"):
        _ok(api, "sound.mute", sound="Mic")
        _ok(api, "source.set", source="Alert", settings={"color": "#00ff00ff"})  # not a sound setting: no line
    lines = [r.getMessage() for r in caplog.records if "sound:" in r.getMessage()]
    assert lines == ["control: StreamBridge (a SprigCast API app) changed Mic (mic) sound: muted None -> True"]


def test_the_asking_app_is_named_never_by_its_key():
    from types import SimpleNamespace

    pytest.importorskip("PySide6.QtWebSockets")
    from sprigcast.app import control_server

    server = control_server.ControlServer.__new__(control_server.ControlServer)
    server.pairing = SimpleNamespace(devices=[SimpleNamespace(name="StreamBridge", key="sc_secret")])
    paired, other = object(), object()
    local = object()
    server.local_key = "sc_local"
    server.sessions = {paired: NativeSession(api=None, key="sc_secret"), other: NativeSession(api=None),
                       local: NativeSession(api=None, key="sc_local")}
    assert server._who(paired) == "StreamBridge (a SprigCast API app)"
    assert server._who(local) == "a SprigCast API app with this PC's key"
    assert server._who(other) == "a SprigCast API app"
