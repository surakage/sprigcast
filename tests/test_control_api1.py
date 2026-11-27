"""API 1, asked for with "api": 1 in an app's first message: fields under data, camelCase answers and events,
events as {event, group, data, at}, set actions that need their value. API 0 apps see none of it."""

import json

from sprigcast.control.native import NativeSession, camel_out, flat
from sprigcast.control.protocol import Close, auth_string
from test_control_native import _api


def _session(**first):
    api, host = _api()
    session = NativeSession(api=api)
    out = list(session.receive(json.dumps({"events": "all", **first})))
    return session, host, out


def _ask(session, message):
    return [json.loads(m) for m in session.receive(json.dumps(message)) if isinstance(m, str)]


def test_hello_offers_the_versions_and_a_welcome_names_the_one_chosen():
    api, _ = _api()
    hello = json.loads(NativeSession(api=api).hello())["hello"]
    assert hello["api"] == 0 and hello["apis"] == [0, 1]
    _, _, out = _session(api=1)
    assert json.loads(out[0])["welcome"]["api"] == 1
    _, _, old = _session()
    assert json.loads(old[0])["welcome"]["api"] == 0
    for wrong in (2, "1", True):
        _, _, closed = _session(api=wrong)
        assert isinstance(closed[0], Close) and closed[0].code == 4010


def test_api_1_with_a_password():
    api, _ = _api()
    session = NativeSession(api=api, password="leafy")
    hello = json.loads(session.hello())["hello"]
    answer = auth_string("leafy", hello["auth"]["salt"], hello["auth"]["challenge"])
    out = [json.loads(m) for m in session.receive(json.dumps({"api": 1, "auth": answer, "events": "none", "id": "1",
                                                              "do": "bloom.show", "data": {"bloom": "BRB"}}))]
    assert out[0]["welcome"]["api"] == 1 and out[1] == {"id": "1", "ok": True, "result": out[1]["result"]}


def test_fields_go_under_data():
    session, host, _ = _session(api=1)
    [shown] = _ask(session, {"id": "7", "do": "bloom.show", "data": {"bloom": "BRB"}})
    assert shown["ok"] and host.collection.active_scenes["horizontal"] == "brb-h"
    [sent] = _ask(session, {"do": "app.broadcast", "data": {"data": {"hello": 1}}})
    assert sent["ok"]
    out = [json.loads(m) for m in session.receive(json.dumps({"id": "s", "steps": [
        {"id": "a", "do": "item.hide", "data": {"bloom": "Chatting", "item": "Alert"}}]})) if isinstance(m, str)]
    assert out[-1]["ok"] and out[-1]["result"]["steps"][0]["id"] == "a"
    assert not host.collection.scene("chat-h").items[2].visible
    assert flat({"do": "x", "id": "1", "data": {"do": "y", "a": 1}}) == {"do": "x", "id": "1", "a": 1}


def test_answers_and_events_are_camel_case():
    session, _, _ = _session(api=1)
    [layout] = _ask(session, {"do": "layout.get", "data": {"bloom": "Chatting", "shape": "horizontal"}})
    item = layout["result"]["shapes"]["horizontal"]["items"][0]
    assert "scaleX" in item and "scale_x" not in item and "sourceSize" in item
    old, _, _ = _session()
    [flat_layout] = _ask(old, {"do": "layout.get", "bloom": "Chatting", "shape": "horizontal"})
    assert "scale_x" in flat_layout["result"]["shapes"]["horizontal"]["items"][0]
    event = json.loads(session.event("item.moved", {"item": "Alert", "scale_x": 2, "settings": {"volume_db": 1}}))
    assert event["event"] == "item.moved" and event["group"] == "item" and event["at"].endswith("Z")
    assert event["data"] == {"item": "Alert", "scaleX": 2, "settings": {"volume_db": 1}}
    closing = json.loads(session.event("app.closing", {}))
    assert closing["group"] is None
    assert json.loads(old.event("bloom.shown", {"bloom": "BRB"})) == {"event": "bloom.shown", "bloom": "BRB"}
    assert camel_out({"settings": {"scale_x": 1}, "data": {"on_air": 1}, "x": [{"on_air": 1}]}) == \
        {"settings": {"scale_x": 1}, "data": {"on_air": 1}, "x": [{"onAir": 1}]}


def test_set_actions_need_their_value():
    session, host, _ = _session(api=1)
    [refused] = _ask(session, {"do": "sound.mute", "data": {"sound": "Mic"}})
    assert refused["error"] == {"code": "bad_request", "message": "muted is missing (to toggle, use sound.toggle).",
                                "field": "muted", "reason": "missing"}
    assert _ask(session, {"do": "sound.mute", "data": {"sound": "Mic", "muted": True}})[0]["ok"]
    assert host.collection.source("mic").settings["muted"] is True
    assert _ask(session, {"do": "sound.toggle", "data": {"sound": "Mic"}})[0]["ok"]
    [batch] = _ask(session, {"oneStep": True, "steps": [{"do": "item.lock", "data": {"bloom": "Chatting",
                                                                                     "item": "Alert"}}]})
    assert batch["error"]["field"] == "locked"
    old, _, _ = _session()
    assert _ask(old, {"do": "sound.mute", "sound": "Mic"})[0]["ok"]  # API 0: still a toggle
