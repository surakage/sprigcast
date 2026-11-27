"""The API described for programs (control/spec.py): every field typed, every event listed, the docs written from
it up to date, and the welcome, errors and step lists saying what Phase 1 promises."""

import json
import re
from pathlib import Path

from sprigcast.control import native, spec, web
from sprigcast.control.native import NativeSession
from test_control_native import _api

SRC = Path(native.__file__).resolve().parents[1]


def test_every_field_has_a_type():
    untyped = [f"{a['name']}.{f['name']}" for a in spec.protocol(include_plugins=False)["actions"]
               for f in a["fields"] if f["type"] == "any"]
    assert not untyped, f"add these to spec.FIELD_TYPES: {', '.join(untyped)}"


def test_field_specs_read_the_words():
    db = spec.field_spec("sound.volume", "db", "?new volume, -100 to 26")
    assert db == {"name": "db", "required": False, "about": "new volume, -100 to 26", "type": "number",
                  "min": -100.0, "max": 26.0}
    on = spec.field_spec("filter.add", "on", "?true (default) or false")
    assert on["type"] == "boolean" and on["default"] is True
    shape = spec.field_spec("item.show", "shape", "?both (default), horizontal or vertical")
    assert shape["values"] == ["both", "horizontal", "vertical"] and shape["default"] == "both"
    assert spec.field_spec("app.broadcast", "data", "any object")["type"] == "object"
    assert spec.field_spec("screenshot.take", "data", "?true: send it back")["type"] == "boolean"
    assert spec.field_spec("place.set", "bitrateKbps", "?500 to 50000")["min"] == 500


def test_every_event_sent_is_described():
    sent = set()
    for path in [*SRC.joinpath("control").glob("*.py"), SRC / "app" / "control_server.py", *SRC.joinpath("store").glob("*.py")]:
        text = path.read_text(encoding="utf-8")
        sent |= set(re.findall(r'out\.append\(\("([a-z_]+\.[a-zA-Z_]+)"', text))
        sent |= set(re.findall(r'(?:send_event|_event)\(api, "([a-z_]+\.[a-zA-Z_]+)"', text))
        sent |= set(re.findall(r'native_broadcast\("([a-z_.]+)"', text))
    sent |= {f"{k}.changed" for k in ("garden", "path")} | {f"{k}s.changed" for k in ("garden", "path")}
    sent |= {"replay.started", "replay.ended"}
    missing = sorted(sent - set(spec.EVENTS))
    assert not missing, f"add these to spec.EVENTS: {', '.join(missing)}"


def test_the_written_docs_are_up_to_date():
    stale = [p.name for p, text in spec.written_files().items()
             if not p.exists() or p.read_text(encoding="utf-8") != text]
    assert not stale, f"run python -m sprigcast.control.spec ({', '.join(stale)} behind)"


def test_app_protocol_and_the_web_link():
    api, _ = _api()
    out = api.run("app.protocol", {})
    assert out["app"] == "SprigCast" and out["version"] and any(a["name"] == "bloom.show" for a in out["actions"])
    assert {"name", "group", "about", "fields", "looksOnly", "oneStep"} <= set(out["actions"][0])
    request = web.Request(method="GET", path="/api/protocol.json", query={}, headers={}, body=b"")
    status, body = web.handle(api, request, enabled=False, password="")
    assert status == 200 and body["api"] == native.API_VERSION and body["actions"]
    assert not native.may_change({"do": "app.protocol"})


def _welcomed(session, events):
    [welcome] = [json.loads(m) for m in session.receive(json.dumps({"events": events}))]
    return welcome["welcome"]


def test_events_by_name_and_unknown_ones():
    api, _ = _api()
    session = NativeSession(api=api)
    welcome = _welcomed(session, ["bloom", "place.changed", "scenes", "bloom.shwn", "meters"])
    assert welcome["events"] == ["bloom", "meters", "place.changed"]
    assert welcome["unknown"] == ["scenes", "bloom.shwn"]
    assert session.event("place.changed", {"place": "out-1"}) is not None
    assert session.event("stream.changed", {"on": True}) is None  # same group, not asked for
    assert session.event("bloom.shown", {}) is not None
    assert "unknown" not in _welcomed(session, "all")  # sent again: replaces the choice
    assert session.event("stream.changed", {"on": True}) is not None


def test_errors_name_their_field():
    api, _ = _api()
    session = NativeSession(api=api)
    list(session.receive(json.dumps({"events": "none"})))

    def ask(**message):
        [out] = [json.loads(m) for m in session.receive(json.dumps(message))]
        return out["error"]

    assert ask(do="transition.set", ms=5) == {"code": "bad_request", "message": "ms must be 50 to 20000.",
                                             "field": "ms", "reason": "out_of_range"}
    assert ask(do="sound.mute")["field"] == "sound" and ask(do="sound.mute")["reason"] == "missing"
    assert ask(do="transition.set", easing="wobbly")["reason"] == "not_one_of"
    assert ask(do="sound.volume", sound="Mic", db="loud")["reason"] == "wrong_type"
    taken = ask(do="bloom.new", name="BRB")
    assert taken["code"] == "busy" and taken["field"] == "name" and taken["reason"] == "already_exists"
    assert "field" not in ask(do="bloom.show", bloom="Nope")


def test_steps_answer_with_their_own_ids():
    api, _ = _api()
    session = NativeSession(api=api)
    list(session.receive(json.dumps({"events": "none"})))
    out = [m for m in session.receive(json.dumps({"id": "s", "steps": [
        {"id": "hide", "do": "item.hide", "bloom": "Chatting", "item": "Alert"},
        {"id": "pause", "wait": 0},
        {"do": "item.show", "bloom": "Chatting", "item": "Alert"}]})) if isinstance(m, str)]
    steps = json.loads(out[-1])["result"]["steps"]
    assert [s.get("id") for s in steps] == ["hide", "pause", None]
    [one] = [json.loads(m) for m in session.receive(json.dumps({"one_step": True, "steps": [
        {"id": "a", "do": "item.hide", "bloom": "Chatting", "item": "Alert"}]}))]
    assert one["result"]["steps"][0]["id"] == "a"


def test_a_plugin_event_named_like_ours_keeps_ours_in_its_group(monkeypatch):
    monkeypatch.setattr(native, "_PLUGIN_EVENTS", set())
    monkeypatch.setattr(native, "_PLUGIN_EVENT_NAMES", set())
    api, _ = _api()
    native.send_event(api, "stream.limit", {})  # Break & End Guard
    assert native.event_group("stream.limit") == "plugin"
    assert native.event_group("stream.changed") == "output"
    native.send_event(api, "snap.taken", {})
    assert native.event_group("snap.taken") == "plugin" and native.event_group("snap.other") == "plugin"


def _answer(api, **message):
    return native.answer(api, message)


def test_camel_case_spellings_reach_the_snake_case_fields():
    api, host = _api()
    out = _answer(api, do="item.place", bloom="Chatting", item="Alert", shape="horizontal", scaleX=2, boundsWidth=20,
                  boundsHeight=10, bounds="fit")
    assert out["ok"], out
    item = host.collection.scene("chat-h").items[2]
    assert item.scale == 2
    both = _answer(api, do="item.place", bloom="Chatting", item="Alert", shape="horizontal", scale_y=3, scaleY=9)
    assert both["ok"] and host.collection.scene("chat-h").items[2].scale_y == 3  # the declared spelling wins
    assert native.aliased({"bitrateKbps": "?"}, {"bitrateKbps": 600}) == {"bitrateKbps": 600}
    session = NativeSession(api=api)
    list(session.receive(json.dumps({"events": "none"})))
    [dry] = [json.loads(m) for m in session.receive(json.dumps({"dryRun": True, "steps": [
        {"do": "item.hide", "bloom": "Chatting", "item": "Alert"}]}))]
    assert dry["result"]["dry_run"] is True and host.collection.scene("chat-h").items[2].visible


def test_id_fields_take_only_an_id():
    api, host = _api()
    assert _answer(api, do="bloom.show", bloomId="brb-h")["ok"]
    assert host.collection.active_scenes["horizontal"] == "brb-h"
    by_name = _answer(api, do="bloom.show", bloomId="BRB")
    assert by_name["error"]["code"] == "not_found"  # a name, given as an id
    assert _answer(api, do="item.hide", bloomId="chat-h", itemId="alert")["ok"]
    assert _answer(api, do="item.hide", bloomId="chat-h", itemId="Alert")["error"]["code"] == "not_found"
    assert _answer(api, do="sound.mute", soundId="mic", muted=True)["ok"]
    assert _answer(api, do="sound.mute", soundId="Mic", muted=True)["error"]["code"] == "not_found"
    clash = _answer(api, do="bloom.show", bloom="Chatting", bloomId="brb-h")["error"]
    assert clash["field"] == "bloomId" and clash["reason"] == "conflict"
    assert _answer(api, do="bloom.show", bloom="BRB", bloomId="BRB")["error"]["code"] == "not_found"


def test_toggle_actions_and_newer_names():
    api, host = _api()
    mic = host.collection.source("mic")
    was = bool(mic.settings.get("muted", False))
    assert _answer(api, do="sound.toggle", sound="Mic", muted=was)["ok"]  # a value given anyway is ignored
    assert mic.settings.get("muted") is (not was)
    assert _answer(api, do="sound.toggle", sound="Mic")["ok"] and mic.settings.get("muted") is was
    locked = _answer(api, do="item.toggleLock", bloom="Chatting", item="Alert")
    assert locked["ok"] and locked["result"] == {"locked": True}
    assert _answer(api, do="item.toggleLock", bloom="Chatting", item="Alert")["result"] == {"locked": False}
    assert "item.toggleLock" in native.BATCH_ACTIONS and "sound.toggle" in native.BATCH_ACTIONS
    assert _answer(api, do="record.togglePause")["error"]["code"] != "unknown_action"
    assert _answer(api, do="filter.share", source="Cam", on=True)["error"]["code"] != "unknown_action"
