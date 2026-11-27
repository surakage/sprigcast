"""The web-link door: SprigCast API actions as plain web addresses, for buttons and scripts that can
only open a link (a Stream Deck "Website" button, a phone shortcut, curl).

    GET  http://127.0.0.1:4455/api                         every action (like app.actions)
    GET  http://127.0.0.1:4455/api/bloom.show?bloom=BRB    run one; fields in the query
    POST http://127.0.0.1:4455/api/filter.set              fields as a JSON body (query fields too)

The password goes in an "Authorization: Bearer <password>" header, or ?password=… for buttons that
can't send headers. A paired device's key (Pair a device) works the same way, and
GET /api/pair?code=123456&name=Streamer.bot trades the code shown in SprigCast for that key. It shares the WebSocket's port: app/control_server.py looks at each new
connection and hands WebSocket upgrades to the WebSocket server and plain requests here.

Safety: off unless switched on in Settings → Connections, always needs the password, and refuses
requests a web page makes from another site (an Origin header that isn't this PC), so a page you
visit can't drive SprigCast behind your back.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from sprigcast.control.native import NativeApi, answer
from sprigcast.control.pairing import matches

log = logging.getLogger(__name__)

MAX_REQUEST = 64 * 1024
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

STATUS_TEXT = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
               405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large", 500: "Internal Server Error"}
ERROR_STATUS = {"bad_request": 400, "not_found": 404, "unknown_action": 404, "not_allowed": 409, "busy": 409,
                "failed": 500, "auth": 401, "off": 403}


def local_origin(origin: str) -> bool:
    """A page on this PC (or a file, "null"). The whole host is compared: http://localhost.example.com is not local."""
    if origin == "null":
        return True
    try:
        parts = urlsplit(origin)
        return parts.scheme in ("http", "https") and parts.hostname in LOCAL_HOSTS
    except ValueError:
        return False


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes


def header_end(raw: bytes) -> int:
    """Where the headers end (after the blank line), or -1 while they haven't all arrived."""
    at = raw.find(b"\r\n\r\n")
    return -1 if at < 0 else at + 4


def is_websocket(raw: bytes) -> bool:
    lines = raw[:header_end(raw)].decode("latin-1").lower().split("\r\n")
    return any(line.startswith("upgrade:") and "websocket" in line for line in lines)


def parse(raw: bytes) -> Request | None:
    """The request, or None while its body is still arriving. Raises ValueError when it's not HTTP."""
    end = header_end(raw)
    if end < 0:
        if len(raw) > MAX_REQUEST:
            raise ValueError("headers too long")
        return None
    lines = raw[:end].decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        raise ValueError("not an HTTP request")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0") or 0)
    if length > MAX_REQUEST:
        raise ValueError("body too large")
    if len(raw) - end < length:
        return None
    url = urlsplit(parts[1])
    return Request(parts[0].upper(), unquote(url.path), dict(parse_qsl(url.query, keep_blank_values=True)),
                   headers, raw[end:end + length])


def _value(text: str) -> Any:
    """Query values: true, 12, -6.5, null and JSON objects become what they look like; the rest is text."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _pair(request: Request, pairing) -> tuple[int, dict[str, Any]]:
    """/api/pair?code=123456&name=Streamer.bot: the code from Pair a device, once, for this device's own key."""
    fields = dict(request.query)
    if request.body:
        try:
            body = json.loads(request.body.decode("utf-8"))
            if isinstance(body, dict):
                fields.update({k: str(v) for k, v in body.items()})
        except ValueError:
            return _error("bad_request", "The body must be JSON.")
    device = pairing.redeem(fields.get("code", ""), fields.get("name", "")) if pairing is not None else None
    if device is None and pairing is not None and pairing.locked_seconds():
        return 429, {"ok": False, "error": {"code": "busy", "message": "Too many wrong pairing codes: try again in "
                                                                     f"{pairing.locked_seconds()} s."}}
    if device is None:
        return _error("auth", "Wrong or expired pairing code. Press Pair a device in SprigCast → Settings → "
                              "Connections for a new one.")
    log.info("control: paired %s (%s) with a code (web link)", device.name, device.id)
    return 200, {"ok": True, "result": {"key": device.key, "name": device.name}}


def _error(code: str, message: str) -> tuple[int, dict[str, Any]]:
    return ERROR_STATUS.get(code, 400), {"ok": False, "error": {"code": code, "message": message}}


def handle(api: NativeApi, request: Request, *, enabled: bool, password: str, keys: list[str] | None = None,
           pairing=None) -> tuple[int, dict[str, Any]]:
    """(HTTP status, JSON answer) for one request. A paired device's key works wherever the password does."""
    if request.method == "OPTIONS":
        return _error("off", "Web pages on other sites can't use SprigCast's web links.")
    if not request.path.rstrip("/").startswith("/api"):
        return _error("not_found", "Use /api/<action>, like /api/bloom.show?bloom=BRB.")
    origin = request.headers.get("origin")
    if origin is not None and not local_origin(origin):
        return _error("off", "Requests from web pages on other sites are refused.")
    if request.path.rstrip("/") == "/api/protocol.json":  # the API's own description: nothing of yours in it
        from sprigcast.control.spec import protocol

        return 200, protocol(include_plugins=False)
    if request.path.rstrip("/") == "/api/pair":  # works with web links off too: the key also opens the WebSocket
        return _pair(request, pairing)
    if not enabled:
        return _error("off", "Web links are off: turn them on in SprigCast → Settings → Connections.")
    if not password:
        return _error("off", "Web links need a password: turn on \"Ask for a password\" in Settings → Connections.")
    given = request.query.pop("password", None)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    given = given or request.headers.get("x-sprigcast-password")
    if not given or not matches(given, [password, *(keys or [])]):
        return _error("auth", "Wrong or missing password.")
    if pairing is not None:
        pairing.seen(given)  # a paired device's key: Settings shows when it was last seen
    if request.method not in ("GET", "POST"):
        return 405, {"ok": False, "error": {"code": "bad_request", "message": "Use GET or POST."}}
    name = request.path.rstrip("/")[len("/api"):].lstrip("/") or "app.actions"
    fields: dict[str, Any] = {k: _value(v) for k, v in request.query.items()}
    if request.body:
        try:
            body = json.loads(request.body.decode("utf-8"))
        except ValueError:
            return _error("bad_request", "The body must be JSON.")
        if not isinstance(body, dict):
            return _error("bad_request", "The body must be a JSON object.")
        fields.update(body)
    fields.pop("do", None)
    out = answer(api, {"do": name, **fields})
    out.pop("id", None)
    if out["ok"]:
        return 200, out
    return ERROR_STATUS.get(out["error"]["code"], 400), out


def response(status: int, body: dict[str, Any]) -> bytes:
    data = json.dumps(body, default=str).encode("utf-8")
    head = (f"HTTP/1.1 {status} {STATUS_TEXT.get(status, 'OK')}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(data)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n")
    return head.encode("latin-1") + data
