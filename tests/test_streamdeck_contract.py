"""The Stream Deck plugin (integrations/streamdeck) against the SprigCast API: every request a key sends works,
and answers with what the key reads to light up."""

import re
from pathlib import Path

import pytest

from sprigcast.control.native import EVENT_GROUPS, ApiError, NativeApi, NativeSession
from test_control_api import FakeHost, _collection

DECK = Path(__file__).resolve().parents[1] / "integrations" / "streamdeck" / "src"
PLUGIN_ACTIONS = {"deaths.add", "deaths.remove", "deaths.reset", "deaths.show", "deaths.get",  # Death count plugin
                  "pngtuber.expression", "pngtuber.expressions", "pngtuber.next", "pngtuber.reset"}  # PNGtuber


def _api(wrapped=False):
    col = _collection()
    if wrapped:
        col.wrap_blooms()  # as every SprigCast collection is now: blooms hold sprigs only
    host = FakeHost(col)
    host.cam = False
    host.camera_on = lambda: host.cam
    host.set_camera = lambda on: setattr(host, "cam", on)
    return NativeApi(host), host


def _ok(api, name, /, **data):
    try:
        return api.run(name, data)
    except ApiError as exc:
        raise AssertionError(f"{name} failed: {exc.code} {exc.message}") from None


def test_every_action_the_plugin_sends_exists():
    api, _ = _api()
    known = {a["do"] for a in _ok(api, "app.actions")["actions"]} | PLUGIN_ACTIONS
    code = "".join(p.read_text(encoding="utf-8") for p in DECK.glob("*.js"))
    events = set(re.findall(r'event === "([a-z]+\.[a-zA-Z]+)"', code))
    files = {"api.json"}  # file names in the code, not actions
    sent = set(re.findall(r'"([a-z]+\.[a-zA-Z]+)"', code)) - events - files
    assert len(sent) > 30 and sent <= known, sorted(sent - known)


def test_every_event_group_the_plugin_asks_for_exists():
    code = (DECK / "sprigcast.js").read_text(encoding="utf-8")
    wanted = re.search(r"EVENTS = \[([^\]]*)\]", code).group(1)
    groups = re.findall(r'"([a-z]+)"', wanted)
    assert groups and set(groups) <= set(EVENT_GROUPS), set(groups) - set(EVENT_GROUPS)


def test_catch_up_answers_what_the_keys_read():
    api, _ = _api()
    assert _ok(api, "bloom.current")["onAir"]["horizontal"]["name"] == "Chatting"
    mic = next(s for s in _ok(api, "sounds.list")["sounds"] if s["name"] == "Mic")
    assert mic["muted"] is False and mic["volumeDb"] == -6.0
    for name in ("stream.status", "record.status", "clips.status", "camera.status"):
        assert isinstance(_ok(api, name)["on"], bool), name
    assert {"garden", "gardens"} <= set(_ok(api, "gardens.list"))
    assert {"path", "paths"} <= set(_ok(api, "paths.list"))
    assert {"kind", "ms", "kinds"} <= set(_ok(api, "transition.get"))
    assert "media" in _ok(api, "media.list")
    assert {"name", "state", "states"} <= set(_ok(api, "sprigs.list")["sprigs"][0])


def test_keys_get_what_they_light_from():
    api, host = _api()
    _ok(api, "bloom.show", bloom="BRB", shape="horizontal")
    assert _ok(api, "sound.mute", sound="Mic")["muted"] is True
    assert _ok(api, "sound.volume", sound="Mic", change=-3)["volumeDb"] == -9.0
    assert _ok(api, "sound.volume", sound="Mic", db=-12)["volumeDb"] == -12.0
    assert _ok(api, "item.toggle", bloom="Chatting", item="Alert") == {"visible": False}
    assert _ok(api, "item.status", bloom="Chatting", item="Alert") == {"visible": False}
    _ok(api, "clips.set", on=True)
    assert host.clips is True
    _ok(api, "camera.set")
    assert host.cam is True and _ok(api, "camera.status")["on"] is True
    _ok(api, "transition.set", kind="cut")
    _ok(api, "transition.set", ms=500)
    _ok(api, "sprig.state", sprig="Frame", state="Quiet")
    assert host.collection.scene("p-frame").state == "Quiet"


def test_item_keys_reach_inside_a_blooms_own_sprig():
    """A key set to bloom Chatting, item Alert still works now Chatting holds a Chatting sprig."""
    api, host = _api(wrapped=True)
    col = host.collection
    assert all(col.part_of(col.source(i.source)) for i in col.scene("chat-h").items)  # sprigs only
    api.changes()
    assert _ok(api, "item.hide", bloom="Chatting", item="Alert") == {"visible": False}
    assert _ok(api, "item.status", bloom="Chatting", item="Alert") == {"visible": False}
    events = [data for name, data in api.changes() if name == "item.changed"]
    assert events and all(e["item"] == "Alert" and e["visible"] is False for e in events)
    assert all("Chatting" in e["blooms"] for e in events)  # so a key keyed by bloom lights up
    assert _ok(api, "item.toggle", bloom="Chatting", item="Alert") == {"visible": True}
    with pytest.raises(ApiError):
        api.run("item.toggle", {"bloom": "Chatting", "item": "Nope"})


def test_a_deck_session_hears_its_groups():
    api, host = _api()
    session = NativeSession(api)
    session.welcomed = True
    session._set_events(["bloom", "sound", "output", "item", "plugin", "media", "filter", "transition", "garden",
                         "path", "sprig"])
    for name in ("bloom.shown", "sound.changed", "stream.changed", "record.changed", "clips.changed",
                 "camera.changed", "item.changed", "media.changed", "filter.changed", "transition.changed",
                 "garden.changed", "path.changed", "sprig.state"):
        assert session.event(name, {}) is not None, name


def test_every_app_hears_sprigcast_closing():
    api, _ = _api()
    session = NativeSession(api)
    session._set_events("none")
    assert session.event("app.closing", {}) is None  # not welcomed yet
    session.welcomed = True
    assert session.event("app.closing", {}) == '{"event": "app.closing"}'
    assert session.event("bloom.shown", {}) is None


def test_api_json_lets_apps_on_this_pc_connect_by_themselves(tmp_path):
    """While the API runs, api.json holds its port and a key (the Stream Deck plugin reads it); gone when it stops."""
    import json as _json

    from test_control_api import _qt

    _qt()
    from sprigcast.app.control_server import ControlServer
    from sprigcast.control.api import ControlApi

    api, host = _api()
    server = ControlServer(ControlApi(host), native=api)
    server.info_path = tmp_path / "SprigCast" / "api.json"
    assert server.start(0, "leafy")
    try:
        info = _json.loads(server.info_path.read_text(encoding="utf-8"))
        # This PC's own key, never the password (PC test 2026-11-15: the password sat in plain text).
        assert info == {"app": "SprigCast", "port": server.port, "path": "/sprigcast", "password": server.local_key}
        assert server.local_key.startswith("sc_local_") and server.local_key in server.keys()
    finally:
        server.stop()
    assert not server.info_path.exists()
