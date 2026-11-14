"""The SprigCast API: our own control language on ws://…/sprigcast."""

import json

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
            "clip.save", "sprig.state", "bloom.go"} <= set(listed)
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
    _ok(api, "bloom.lineUp", bloom="Chatting")
    assert host.studio() and host.staged() == {"horizontal": "chat-h", "vertical": "chat-v"}
    _ok(api, "bloom.go")
    assert host.collection.active_scenes["horizontal"] == "chat-h"


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
    assert _ok(api, "stream.toggle") == {"on": True}
    assert _fails(api, "stream.start") == "busy"
    assert _ok(api, "stream.status")["on"] is True
    _ok(api, "stream.stop")
    _ok(api, "record.start")
    assert _ok(api, "record.stop") == {"file": "C:/rec.mkv"}
    assert _ok(api, "clip.save") == {"saved": [], "started": True}
    assert _ok(api, "clip.save") == {"saved": ["clip.mkv"], "started": False}
    assert _ok(api, "transition.set", kind="leaf", ms=500) == {"kind": "leaf", "ms": 500}
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
    assert fail == {"id": 2, "ok": False, "error": {"code": "not_found", "message": "No bloom called 'Nope'."}}
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
