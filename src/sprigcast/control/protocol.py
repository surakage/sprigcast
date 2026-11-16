"""The obs-websocket 5 wire format, so apps written for OBS can drive SprigCast.

Every message is JSON: {"op": <number>, "d": {...}}. The server says Hello (with a password
challenge when one is set), the app answers Identify, the server confirms with Identified. After
that the app sends Request or RequestBatch and gets a RequestResponse or RequestBatchResponse back;
the server pushes Events the app subscribed to.

Nothing here knows about Qt or sockets: Session.receive() turns one incoming text message into the
steps to take (texts to send, a pause inside a batch, or closing the connection).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

OBS_WEBSOCKET_VERSION = "5.5.4"
RPC_VERSION = 1


class Op:
    HELLO = 0
    IDENTIFY = 1
    IDENTIFIED = 2
    REIDENTIFY = 3
    EVENT = 5
    REQUEST = 6
    REQUEST_RESPONSE = 7
    REQUEST_BATCH = 8
    REQUEST_BATCH_RESPONSE = 9


class CloseCode:
    UNKNOWN_REASON = 4000
    MESSAGE_DECODE_ERROR = 4002
    MISSING_DATA_FIELD = 4003
    INVALID_DATA_FIELD_TYPE = 4004
    INVALID_DATA_FIELD_VALUE = 4005
    UNKNOWN_OP_CODE = 4006
    NOT_IDENTIFIED = 4007
    ALREADY_IDENTIFIED = 4008
    AUTHENTICATION_FAILED = 4009
    UNSUPPORTED_RPC_VERSION = 4010


class Status:
    """requestStatus.code values."""

    SUCCESS = 100
    MISSING_REQUEST_TYPE = 203
    UNKNOWN_REQUEST_TYPE = 204
    GENERIC_ERROR = 205
    UNSUPPORTED_BATCH_EXECUTION_TYPE = 206
    NOT_READY = 207
    MISSING_REQUEST_FIELD = 300
    MISSING_REQUEST_DATA = 301
    INVALID_REQUEST_FIELD = 400
    INVALID_REQUEST_FIELD_TYPE = 401
    REQUEST_FIELD_OUT_OF_RANGE = 402
    REQUEST_FIELD_EMPTY = 403
    OUTPUT_RUNNING = 500
    OUTPUT_NOT_RUNNING = 501
    OUTPUT_PAUSED = 502
    OUTPUT_NOT_PAUSED = 503
    OUTPUT_DISABLED = 504
    STUDIO_MODE_ACTIVE = 505
    STUDIO_MODE_NOT_ACTIVE = 506
    RESOURCE_NOT_FOUND = 600
    RESOURCE_ALREADY_EXISTS = 601
    INVALID_RESOURCE_TYPE = 602
    NOT_ENOUGH_RESOURCES = 603
    INVALID_RESOURCE_STATE = 604
    INVALID_INPUT_KIND = 605
    RESOURCE_ACTION_NOT_SUPPORTED = 703


class Sub:
    """eventSubscriptions bits (an app ORs together the ones it wants)."""

    NONE = 0
    GENERAL = 1 << 0
    CONFIG = 1 << 1
    SCENES = 1 << 2
    INPUTS = 1 << 3
    TRANSITIONS = 1 << 4
    FILTERS = 1 << 5
    OUTPUTS = 1 << 6
    SCENE_ITEMS = 1 << 7
    MEDIA_INPUTS = 1 << 8
    VENDORS = 1 << 9
    UI = 1 << 10
    ALL = (1 << 11) - 1  # every low-volume category, the default
    INPUT_VOLUME_METERS = 1 << 16  # about 15 a second: only sent when asked for by name
    INPUT_ACTIVE_STATE_CHANGED = 1 << 17
    INPUT_SHOW_STATE_CHANGED = 1 << 18
    SCENE_ITEM_TRANSFORM_CHANGED = 1 << 19


class RequestError(Exception):
    """Raised by a request handler: the request fails with this code and comment."""

    def __init__(self, code: int, comment: str = ""):
        super().__init__(comment)
        self.code = code
        self.comment = comment


@dataclass
class Sleep:
    """Inside a batch: wait this long before the next request (the transport schedules it)."""

    seconds: float


@dataclass
class Close:
    code: int
    reason: str


Step = str | Sleep | Close
Handler = Callable[[str, dict[str, Any]], dict[str, Any] | None]

MAX_SLEEP_MS = 50_000
MAX_SLEEP_FRAMES = 10_000


def secret_for(password: str, salt: str) -> str:
    return base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()


def auth_string(password: str, salt: str, challenge: str) -> str:
    """What a client sends back as Identify.authentication (also used by tests and by clients)."""
    secret = secret_for(password, salt)
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


def new_password() -> str:
    """A password that is easy to copy and hard to guess (16 letters and digits)."""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


def dumps(op: int, d: dict[str, Any]) -> str:
    return json.dumps({"op": op, "d": d}, separators=(",", ":"))


@dataclass
class Session:
    """One connected app.

    handler(requestType, requestData) returns the responseData (or None) and raises RequestError
    to fail. frame_seconds is how long one frame lasts, for Sleep with sleepFrames.
    """

    handler: Handler
    password: str = ""
    keys: list[str] = field(default_factory=list)  # paired devices' keys (Pair a device) work like the password
    frame_seconds: float = 1 / 60
    identified: bool = False
    subscriptions: int = Sub.ALL
    salt: str = field(default_factory=lambda: base64.b64encode(secrets.token_bytes(32)).decode())
    challenge: str = field(default_factory=lambda: base64.b64encode(secrets.token_bytes(32)).decode())

    def hello(self) -> str:
        d: dict[str, Any] = {"obsWebSocketVersion": OBS_WEBSOCKET_VERSION, "rpcVersion": RPC_VERSION}
        if self.password:
            d["authentication"] = {"challenge": self.challenge, "salt": self.salt}
        return dumps(Op.HELLO, d)

    def wants(self, intent: int) -> bool:
        return self.identified and bool(self.subscriptions & intent)

    def event(self, event_type: str, intent: int, data: dict[str, Any] | None = None) -> str | None:
        """The Event message for this app, or None when it didn't subscribe to that kind."""
        if not self.wants(intent):
            return None
        d: dict[str, Any] = {"eventType": event_type, "eventIntent": intent}
        if data is not None:
            d["eventData"] = data
        return dumps(Op.EVENT, d)

    # Incoming ------------------------------------------------------------------------

    def receive(self, text: str) -> Iterator[Step]:
        try:
            message = json.loads(text)
        except (ValueError, TypeError):
            yield Close(CloseCode.MESSAGE_DECODE_ERROR, "The message is not valid JSON.")
            return
        if not isinstance(message, dict) or "op" not in message:
            yield Close(CloseCode.MISSING_DATA_FIELD, "The message has no op.")
            return
        op, d = message.get("op"), message.get("d")
        if not isinstance(op, int) or not isinstance(d, dict):
            yield Close(CloseCode.INVALID_DATA_FIELD_TYPE, "op must be a number and d an object.")
            return
        if op == Op.IDENTIFY:
            yield from self._identify(d)
        elif not self.identified:
            yield Close(CloseCode.NOT_IDENTIFIED, "Send Identify first.")
        elif op == Op.REIDENTIFY:
            self._set_subscriptions(d)
            yield dumps(Op.IDENTIFIED, {"negotiatedRpcVersion": RPC_VERSION})
        elif op == Op.REQUEST:
            if "requestType" not in d or "requestId" not in d:
                yield Close(CloseCode.MISSING_DATA_FIELD, "A request needs requestType and requestId.")
                return
            yield dumps(Op.REQUEST_RESPONSE, self._run(d))
        elif op == Op.REQUEST_BATCH:
            yield from self._batch(d)
        else:
            yield Close(CloseCode.UNKNOWN_OP_CODE, f"Unknown op {op}.")

    def _identify(self, d: dict[str, Any]) -> Iterator[Step]:
        if self.identified:
            yield Close(CloseCode.ALREADY_IDENTIFIED, "Already identified; use Reidentify.")
            return
        if d.get("rpcVersion") != RPC_VERSION:
            yield Close(CloseCode.UNSUPPORTED_RPC_VERSION, f"Only rpcVersion {RPC_VERSION} is supported.")
            return
        if self.password:
            given = d.get("authentication")
            right = False
            for secret in [self.password, *self.keys]:
                if isinstance(given, str) and secret and secrets.compare_digest(
                        given, auth_string(secret, self.salt, self.challenge)):
                    right = True
            if not right:
                yield Close(CloseCode.AUTHENTICATION_FAILED, "Wrong password.")
                return
        self._set_subscriptions(d)
        self.identified = True
        yield dumps(Op.IDENTIFIED, {"negotiatedRpcVersion": RPC_VERSION})

    def _set_subscriptions(self, d: dict[str, Any]) -> None:
        subs = d.get("eventSubscriptions", self.subscriptions if self.identified else Sub.ALL)
        if isinstance(subs, int) and not isinstance(subs, bool):
            self.subscriptions = subs

    def _run(self, d: dict[str, Any]) -> dict[str, Any]:
        """One request -> the RequestResponse d."""
        request_type = d.get("requestType")
        out: dict[str, Any] = {"requestType": request_type, "requestId": d.get("requestId")}
        data = d.get("requestData")
        if data is None:
            data = {}
        try:
            if not isinstance(request_type, str) or not request_type:
                raise RequestError(Status.MISSING_REQUEST_TYPE, "requestType is missing.")
            if not isinstance(data, dict):
                raise RequestError(Status.INVALID_REQUEST_FIELD_TYPE, "requestData must be an object.")
            response = self.handler(request_type, data)
        except RequestError as exc:
            out["requestStatus"] = {"result": False, "code": exc.code}
            if exc.comment:
                out["requestStatus"]["comment"] = exc.comment
            return out
        except Exception as exc:  # a bug in a handler must not take the connection down
            out["requestStatus"] = {"result": False, "code": Status.GENERIC_ERROR, "comment": f"{type(exc).__name__}: {exc}"}
            return out
        out["requestStatus"] = {"result": True, "code": Status.SUCCESS}
        if response is not None:
            out["responseData"] = response
        return out

    def _batch(self, d: dict[str, Any]) -> Iterator[Step]:
        requests = d.get("requests")
        if "requestId" not in d or not isinstance(requests, list):
            yield Close(CloseCode.MISSING_DATA_FIELD, "A batch needs requestId and requests.")
            return
        execution = d.get("executionType", 0)
        halt = bool(d.get("haltOnFailure", False))
        results: list[dict[str, Any]] = []
        if execution not in (0, 1, 2):
            for request in requests:
                request = request if isinstance(request, dict) else {}
                results.append({"requestType": request.get("requestType"), "requestId": request.get("requestId"),
                                "requestStatus": {"result": False, "code": Status.UNSUPPORTED_BATCH_EXECUTION_TYPE}})
            yield dumps(Op.REQUEST_BATCH_RESPONSE, {"requestId": d["requestId"], "results": results})
            return
        # Parallel (2) runs one after another too: the results are the same, just in order.
        for request in requests:
            if not isinstance(request, dict):
                continue
            if request.get("requestType") == "Sleep" and execution != 2:
                result, pause = self._sleep(request, execution)
                if pause:
                    yield Sleep(pause)
            else:
                result = self._run(request)
            results.append(result)
            if halt and not result["requestStatus"]["result"]:
                break
        yield dumps(Op.REQUEST_BATCH_RESPONSE, {"requestId": d["requestId"], "results": results})

    def _sleep(self, request: dict[str, Any], execution: int) -> tuple[dict[str, Any], float]:
        data = request.get("requestData") or {}
        out: dict[str, Any] = {"requestType": "Sleep", "requestId": request.get("requestId")}
        key, top = ("sleepMillis", MAX_SLEEP_MS) if execution == 0 else ("sleepFrames", MAX_SLEEP_FRAMES)
        value = data.get(key) if isinstance(data, dict) else None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            out["requestStatus"] = {"result": False, "code": Status.MISSING_REQUEST_FIELD, "comment": f"{key} is missing."}
            return out, 0.0
        if not 0 <= value <= top:
            out["requestStatus"] = {"result": False, "code": Status.REQUEST_FIELD_OUT_OF_RANGE,
                                    "comment": f"{key} must be 0 to {top}."}
            return out, 0.0
        out["requestStatus"] = {"result": True, "code": Status.SUCCESS}
        return out, value / 1000 if execution == 0 else value * self.frame_seconds
