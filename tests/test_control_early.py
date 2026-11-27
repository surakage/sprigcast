"""The control port opens while SprigCast starts: apps sign in at once, their requests wait for the window.

Streamer.bot tried while SprigCast was still starting, was refused, and waited 30 s to try again (PC, 2026-11-26)."""

import json

from sprigcast.control.native import NativeApi
from test_control_api import FakeHost, _collection, _qt, _wait


class _LateHost(FakeHost):
    """A host whose window isn't there yet: anything that reaches for the garden fails loudly."""

    def __init__(self):
        self._real = None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if self._real is None:
            raise AssertionError(f"asked for {name} before the window was there")
        return getattr(self._real, name)


def test_apps_sign_in_before_the_window_and_are_answered_once_it_is_there():
    app = _qt()
    from PySide6.QtCore import QUrl
    from PySide6.QtWebSockets import QWebSocket

    from sprigcast.app.control_server import ControlServer
    from sprigcast.control.api import ControlApi

    host = _LateHost()
    server = ControlServer(ControlApi(host), native=NativeApi(host))
    server.ready = False
    assert server.start(0)
    ours, obs = QWebSocket(), QWebSocket()
    got_ours, got_obs = [], []
    ours.textMessageReceived.connect(lambda t: got_ours.append(json.loads(t)))
    obs.textMessageReceived.connect(lambda t: got_obs.append(json.loads(t)))
    try:
        ours.open(QUrl(f"ws://127.0.0.1:{server.port}/sprigcast"))
        obs.open(QUrl(f"ws://127.0.0.1:{server.port}"))
        assert _wait(app, lambda: got_ours and got_obs)
        obs.sendTextMessage(json.dumps({"op": 1, "d": {"rpcVersion": 1}}))
        ours.sendTextMessage(json.dumps({"events": "all"}))
        assert _wait(app, lambda: len(got_ours) >= 2 and len(got_obs) >= 2)  # signed in, no window yet
        assert "welcome" in got_ours[1] and got_obs[1]["op"] == 2
        ours.sendTextMessage(json.dumps({"id": "a", "do": "blooms.list"}))
        obs.sendTextMessage(json.dumps({"op": 6, "d": {"requestType": "GetSceneList", "requestId": "b"}}))
        _wait(app, lambda: False, 0.3)
        assert not any(m.get("id") == "a" for m in got_ours) and len(server._held) == 2  # waiting
        host._real = FakeHost(_collection())
        server.become_ready()
        assert _wait(app, lambda: any(m.get("id") == "a" for m in got_ours)
                     and any(m.get("op") == 7 for m in got_obs))
        assert next(m for m in got_ours if m.get("id") == "a")["ok"] is True
    finally:
        ours.close()
        obs.close()
        _wait(app, lambda: not server.sessions, 2.0)
        server.stop()


def test_open_early_is_handed_to_the_window(monkeypatch):
    _qt()
    from sprigcast.app import control_server

    monkeypatch.setattr(control_server, "saved_settings", lambda: {
        "enabled": True, "port": 0, "password": "", "use_password": False, "lan": False, "obs_door": True,
        "web_door": False})
    monkeypatch.setattr(control_server.ControlServer, "_write_info", lambda self, key: None)
    control_server.open_early(None)
    early = control_server._EARLY
    assert early is not None and early.listening and not early.ready
    window = type("W", (), {"collection": _collection()})()
    monkeypatch.setattr(control_server.ControlServer, "setParent", lambda self, parent: None)
    try:
        server = control_server.start_for(window)
        assert server is early and server.ready and server.api.host.w is window
        assert control_server._EARLY is None
    finally:
        early.stop()
