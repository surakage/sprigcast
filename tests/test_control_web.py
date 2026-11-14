"""The web-link door: /api/<action> on the control port."""

import json

from test_control_api import FakeHost, _collection, _qt, _wait

from sprigcast.control import web
from sprigcast.control.native import NativeApi


def _request(text: str) -> web.Request:
    request = web.parse(text.encode())
    assert request is not None
    return request


def _api():
    host = FakeHost(_collection())
    return NativeApi(host), host


def test_parse_waits_for_the_whole_request():
    assert web.parse(b"GET /api HTTP/1.1\r\nHost: x\r\n") is None  # headers not done
    assert web.parse(b"POST /api/x HTTP/1.1\r\nContent-Length: 10\r\n\r\n{}") is None  # body not done
    request = _request("GET /api/bloom.show?bloom=BRB&shape=vertical HTTP/1.1\r\nHost: x\r\n\r\n")
    assert request.path == "/api/bloom.show" and request.query == {"bloom": "BRB", "shape": "vertical"}
    assert web.is_websocket(b"GET /sprigcast HTTP/1.1\r\nUpgrade: websocket\r\n\r\n")
    assert not web.is_websocket(b"GET /api HTTP/1.1\r\n\r\n")


def test_links_run_actions_with_the_password():
    api, host = _api()
    ok = dict(enabled=True, password="leafy")
    status, body = web.handle(api, _request("GET /api/bloom.show?bloom=BRB&password=leafy HTTP/1.1\r\n\r\n"), **ok)
    assert status == 200 and body["ok"] and host.collection.active_scenes["horizontal"] == "brb-h"
    # Query values become numbers and booleans; a JSON body works too.
    status, body = web.handle(api, _request(
        "GET /api/sound.volume?sound=Mic&db=-12 HTTP/1.1\r\nAuthorization: Bearer leafy\r\n\r\n"), **ok)
    assert status == 200 and body["result"]["volumeDb"] == -12.0
    raw = '{"sound": "Mic", "muted": true}'
    status, body = web.handle(api, _request(
        f"POST /api/sound.mute HTTP/1.1\r\nX-SprigCast-Password: leafy\r\nContent-Length: {len(raw)}\r\n\r\n{raw}"), **ok)
    assert status == 200 and host.collection.source("mic").settings["muted"] is True
    # /api alone lists the actions.
    status, body = web.handle(api, _request("GET /api?password=leafy HTTP/1.1\r\n\r\n"), **ok)
    assert any(a["do"] == "bloom.show" for a in body["result"]["actions"])
    # Errors carry a fitting status.
    assert web.handle(api, _request("GET /api/bloom.show?bloom=Nope&password=leafy HTTP/1.1\r\n\r\n"), **ok)[0] == 404
    assert web.handle(api, _request("GET /api/bloom.show?bloom=BRB&password=nope HTTP/1.1\r\n\r\n"), **ok)[0] == 401


def test_links_are_safe_by_default():
    api, host = _api()
    req = "GET /api/stream.start?password=leafy HTTP/1.1\r\n\r\n"
    assert web.handle(api, _request(req), enabled=False, password="leafy")[0] == 403  # off
    assert web.handle(api, _request(req), enabled=True, password="")[0] == 403  # no password set
    evil = "GET /api/stream.start?password=leafy HTTP/1.1\r\nOrigin: https://example.com\r\n\r\n"
    assert web.handle(api, _request(evil), enabled=True, password="leafy")[0] == 403
    assert host.live is None


def test_one_port_serves_links_and_websockets():
    app = _qt()
    from PySide6.QtCore import QUrl
    from PySide6.QtNetwork import QTcpSocket
    from PySide6.QtWebSockets import QWebSocket

    from sprigcast.app.control_server import ControlServer
    from sprigcast.control.api import ControlApi

    host = FakeHost(_collection())
    server = ControlServer(ControlApi(host), native=NativeApi(host))
    server.web_door = True
    assert server.start(0, password="leafy")
    ws, got = QWebSocket(), []
    ws.textMessageReceived.connect(lambda t: got.append(json.loads(t)))
    sock, reply = QTcpSocket(), bytearray()
    sock.readyRead.connect(lambda: reply.extend(bytes(sock.readAll())))
    try:
        ws.open(QUrl(f"ws://127.0.0.1:{server.port}/sprigcast"))
        assert _wait(app, lambda: got and "hello" in got[0])
        sock.connectToHost("127.0.0.1", server.port)
        assert _wait(app, lambda: sock.state() == QTcpSocket.SocketState.ConnectedState)
        sock.write(b"GET /api/bloom.show?bloom=BRB HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer leafy\r\n\r\n")
        assert _wait(app, lambda: b"\r\n\r\n" in reply and reply.endswith(b"}"))
        head, _, body = bytes(reply).partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200") and json.loads(body)["ok"] is True
        assert host.collection.active_scenes["horizontal"] == "brb-h"
        # The WebSocket app keeps working on the same port.
        ws.sendTextMessage(json.dumps({"auth": "x"}))  # wrong password: closed
        assert _wait(app, lambda: ws.state().name == "UnconnectedState" or len(server.sessions) == 0)
    finally:
        ws.close()
        sock.close()
        server.stop()
