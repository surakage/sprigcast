"""Connect a Twitch account so SprigCast fetches the stream key itself (no copy-pasting).

Uses Twitch's Device Code Grant Flow, the sign-in made for apps without a web server:
SprigCast shows a short code, you approve it at twitch.tv/activate, and Twitch hands back
tokens. Only the `channel:read:stream_key` scope is requested. Tokens live in the OS credential
store (like stream keys), never in scenes.json.

Twitch needs a registered app's Client ID. A Public client has no secret, so the ID can be
shipped in open-source code; it can also be overridden in QSettings ("twitch/client_id").
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from sprigcast.output.keys import get_key, set_key

DEFAULT_CLIENT_ID = "jsv3nwvolg6ph5e5s9tyzphwzcyvbp"  # SprigCast's public Twitch app (Public client: no secret)
SCOPES = "channel:read:stream_key"
TOKEN_REF = "twitch/oauth"  # keyring entry holding {"access_token", "refresh_token", "expires_at", "login", "user_id"}
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"
TIMEOUT = 10


class TwitchAuthError(Exception):
    """Something the user has to act on, worded for them."""


class Pending(Exception):
    """The user hasn't approved the code yet (or we were asked to slow down)."""


def client_id() -> str:
    try:
        from PySide6.QtCore import QSettings

        override = str(QSettings().value("twitch/client_id", "") or "").strip()
    except Exception:
        override = ""
    return override or DEFAULT_CLIENT_ID


def _post(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b"{}")
        except ValueError:
            body = {}
        body.setdefault("status", exc.code)
        return {"error": body}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TwitchAuthError(f"Couldn't reach Twitch: {exc}") from exc


def _get(path: str, token: str, cid: str) -> dict:
    request = urllib.request.Request(
        f"{HELIX}{path}", headers={"Authorization": f"Bearer {token}", "Client-Id": cid}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("token rejected") from exc
        raise TwitchAuthError(f"Twitch said {exc.code} for {path.split('?')[0]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TwitchAuthError(f"Couldn't reach Twitch: {exc}") from exc


# Device sign-in -----------------------------------------------------------------------


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_at: float


def start_device_flow(cid: str | None = None) -> DeviceCode:
    cid = cid or client_id()
    if not cid:
        raise TwitchAuthError("SprigCast has no Twitch Client ID yet (Settings → Stream).")
    reply = _post(DEVICE_URL, {"client_id": cid, "scopes": SCOPES})
    if "error" in reply:
        raise TwitchAuthError(f"Twitch refused to start sign-in: {reply['error'].get('message', reply['error'])}")
    return DeviceCode(
        device_code=reply["device_code"],
        user_code=reply["user_code"],
        verification_uri=reply["verification_uri"],
        interval=int(reply.get("interval", 5)),
        expires_at=time.time() + int(reply.get("expires_in", 1800)),
    )


def poll_device_flow(code: DeviceCode, cid: str | None = None) -> dict:
    """One check: returns the saved account once approved, raises Pending while waiting."""
    cid = cid or client_id()
    if time.time() > code.expires_at:
        raise TwitchAuthError("The code expired. Click Connect again.")
    reply = _post(
        TOKEN_URL,
        {
            "client_id": cid,
            "scopes": SCOPES,
            "device_code": code.device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    if "error" in reply:
        message = str(reply["error"].get("message", ""))
        if message in ("authorization_pending", "slow_down"):
            raise Pending(message)
        if message == "access_denied":
            raise TwitchAuthError("Sign-in was declined on Twitch.")
        raise TwitchAuthError(f"Twitch sign-in failed: {message or reply['error']}")
    return _save_tokens(reply, cid)


def _save_tokens(reply: dict, cid: str, account: dict | None = None) -> dict:
    tokens = {
        "access_token": reply["access_token"],
        "refresh_token": reply.get("refresh_token", ""),
        "expires_at": time.time() + int(reply.get("expires_in", 3600)) - 60,
    }
    if account is None:
        users = _get("/users", tokens["access_token"], cid).get("data") or []
        if not users:
            raise TwitchAuthError("Twitch didn't return your account.")
        account = {"login": users[0]["login"], "user_id": users[0]["id"]}
    tokens.update(login=account["login"], user_id=account["user_id"])
    set_key(TOKEN_REF, json.dumps(tokens))
    _announce(account["login"])
    return tokens


# Using the account ----------------------------------------------------------------------


def connected_account() -> str | None:
    """The connected Twitch login name, or None."""
    tokens = _load()
    return tokens.get("login") if tokens else None


def disconnect() -> None:
    set_key(TOKEN_REF, "")
    _announce("")


# Who's connected, without opening the credential store ----------------------------------

LOGIN_SETTING = "twitch/login"  # the connected login name ("" = none), kept in QSettings: it isn't secret
_events = None


def account_events():
    """A Qt object whose `changed(str)` fires with the login name when Twitch is connected, "" when
    disconnected. Create it on the UI thread first (the Chat panel does), so signals land there."""
    global _events
    if _events is None:
        from PySide6.QtCore import QObject, Signal

        class _Events(QObject):
            changed = Signal(str)

        _events = _Events()
    return _events


def remembered_login() -> str | None:
    """The connected login from QSettings: None if never recorded (older installs), "" if not connected."""
    from PySide6.QtCore import QSettings

    value = QSettings().value(LOGIN_SETTING)
    return None if value is None else str(value)


def _announce(login: str) -> None:
    try:
        from PySide6.QtCore import QSettings

        QSettings().setValue(LOGIN_SETTING, login)
    except Exception:
        return
    if _events is not None:
        _events.changed.emit(login)


def _load() -> dict | None:
    raw = get_key(TOKEN_REF)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _refresh(tokens: dict, cid: str) -> dict:
    if not tokens.get("refresh_token"):
        raise TwitchAuthError("Twitch sign-in expired. Reconnect in Settings → Stream.")
    reply = _post(
        TOKEN_URL, {"client_id": cid, "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
    )
    if "error" in reply:
        raise TwitchAuthError("Twitch sign-in expired. Reconnect in Settings → Stream.")
    return _save_tokens(reply, cid, {"login": tokens["login"], "user_id": tokens["user_id"]})


def fetch_stream_key(cid: str | None = None) -> str:
    """Your current stream key, straight from Twitch. Refreshes the sign-in when needed."""
    cid = cid or client_id()
    tokens = _load()
    if not tokens:
        raise TwitchAuthError("No Twitch account connected.")
    if time.time() >= float(tokens.get("expires_at", 0)):
        tokens = _refresh(tokens, cid)
    path = f"/streams/key?broadcaster_id={tokens['user_id']}"
    try:
        data = _get(path, tokens["access_token"], cid)
    except PermissionError:
        tokens = _refresh(tokens, cid)
        data = _get(path, tokens["access_token"], cid)
    keys = data.get("data") or []
    if not keys or not keys[0].get("stream_key"):
        raise TwitchAuthError("Twitch didn't return a stream key.")
    return keys[0]["stream_key"]
