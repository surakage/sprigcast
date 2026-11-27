"""Connect a Twitch account so SprigCast fetches the stream key itself (no copy-pasting).

Uses Twitch's Device Code Grant Flow, the sign-in made for apps without a web server:
SprigCast shows a short code, you approve it at twitch.tv/activate, and Twitch hands back
tokens. Only `channel:read:stream_key`, `user:write:chat` (send chat from the Chat panel) and
`channel:manage:broadcast` (Stream details: title, category, tags) are requested, plus chat moderation and, for
store plugins, `channel:read:ads` (Ad break countdown) and `channel:manage:clips` (Random clip player: your own
clips' video files). Tokens live in the OS credential
store (like stream keys), never in scenes.json.

Twitch needs a registered app's Client ID. A Public client has no secret, so the ID can be
shipped in open-source code; it can also be overridden in QSettings ("twitch/client_id").
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from sprigcast.output.keys import get_key, set_key

log = logging.getLogger(__name__)

DEFAULT_CLIENT_ID = "jsv3nwvolg6ph5e5s9tyzphwzcyvbp"  # SprigCast's public Twitch app (Public client: no secret)
# Your stream key, sending chat from the Chat panel, and changing your title, category and tags (Stream details)
CHAT_SCOPE = "user:write:chat"
# Chat moderation from the Chat panel: timeout and ban, delete a message, block an account.
MOD_SCOPES = ("moderator:manage:banned_users", "moderator:manage:chat_messages", "user:manage:blocked_users")
# Store plugins: the Ad break countdown reads when ads come; the Random clip player fetches your own clips' videos.
ADS_SCOPE = "channel:read:ads"
CLIPS_SCOPE = "channel:manage:clips"
# The Chat panel's mod bar: slow, followers-only, subscribers-only and emote-only modes, and Shield Mode.
SETTINGS_SCOPE = "moderator:manage:chat_settings"
SHIELD_SCOPE = "moderator:manage:shield_mode"
SCOPES = ("channel:read:stream_key user:write:chat channel:manage:broadcast " + " ".join(MOD_SCOPES)
          + f" {ADS_SCOPE} {CLIPS_SCOPE} {SETTINGS_SCOPE} {SHIELD_SCOPE}")
TOKEN_REF = "twitch/oauth"  # keyring entry holding {"access_token", "refresh_token", "expires_at", "login", "user_id"}
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"
TIMEOUT = 10
# Twitch hands public apps (no client secret) a refresh token that works once. Two refreshes at the same
# moment (going live while a chat message goes out) would spend it twice and sign you out: one at a time.
_refresh_lock = threading.Lock()


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
        "scope": list(reply.get("scope") or []),
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
    with _refresh_lock:
        latest = _load() or tokens
        if latest.get("refresh_token") != tokens.get("refresh_token") and latest.get("access_token"):
            return latest  # another refresh finished while this one waited: its token is the fresh one
        if not latest.get("refresh_token"):
            raise TwitchAuthError("Twitch sign-in expired. Reconnect in Settings → Stream.")
        reply = _post(
            TOKEN_URL, {"client_id": cid, "grant_type": "refresh_token", "refresh_token": latest["refresh_token"]}
        )
        if "error" in reply:
            raise TwitchAuthError("Twitch sign-in expired. Reconnect in Settings → Stream.")
        reply.setdefault("refresh_token", latest["refresh_token"])  # not every answer repeats it
        return _save_tokens(reply, cid, {"login": latest["login"], "user_id": latest["user_id"]})


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


# Reading as the signed-in account (store plugins) --------------------------------------------


class MissingScope(TwitchAuthError):
    """Signed in before SprigCast asked for this permission: connect Twitch again once."""


def has_scope(scope: str) -> bool:
    tokens = _load()
    return bool(tokens) and scope in (tokens.get("scope") or [])


def helix_get(path_for, scope: str | None = None, cid: str | None = None) -> dict:
    """GET a Helix path as the signed-in account, refreshing the token when it's old or turned down.
    path_for(user_id) gives the path (most ask for your own id). Raises TwitchAuthError worded for the user:
    MissingScope when the sign-in predates `scope`."""
    cid = cid or client_id()
    tokens = _load()
    if not tokens:
        raise TwitchAuthError("Connect Twitch first (Settings → Stream).")
    if scope and scope not in (tokens.get("scope") or []):
        raise MissingScope("Connect Twitch again once to allow this (Settings → Stream).")
    if time.time() >= float(tokens.get("expires_at", 0)):
        tokens = _refresh(tokens, cid)
    try:
        return _get(path_for(str(tokens["user_id"])), tokens["access_token"], cid)
    except PermissionError:
        tokens = _refresh(tokens, cid)
        return _get(path_for(str(tokens["user_id"])), tokens["access_token"], cid)


# Sending chat -----------------------------------------------------------------------------


def can_send_chat() -> bool:
    """Was Twitch connected with permission to send chat? (Connections made before it was asked for weren't.)"""
    tokens = _load()
    return bool(tokens) and CHAT_SCOPE in (tokens.get("scope") or [])


def _post_json(path: str, body: dict, token: str, cid: str) -> dict:
    request = urllib.request.Request(
        f"{HELIX}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Client-Id": cid, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("token rejected") from exc
        try:
            message = json.loads(exc.read() or b"{}").get("message", "")
        except ValueError:
            message = ""
        if exc.code == 403:
            raise TwitchAuthError("Reconnect Twitch to send chat (Settings → Stream).") from exc
        raise TwitchAuthError(f"Twitch said {exc.code}: {message or 'could not send'}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TwitchAuthError(f"Couldn't reach Twitch: {exc}") from exc


def send_chat(channel: str, text: str, cid: str | None = None) -> None:
    """Say `text` in `channel`'s chat as the connected account. Raises TwitchAuthError worded for the user."""
    cid = cid or client_id()
    tokens = _load()
    if not tokens:
        raise TwitchAuthError("Connect Twitch to send chat.")
    if CHAT_SCOPE not in (tokens.get("scope") or []):
        raise TwitchAuthError("Reconnect Twitch once so SprigCast may send chat (Settings → Stream).")
    if time.time() >= float(tokens.get("expires_at", 0)):
        tokens = _refresh(tokens, cid)
    channel = channel.lower().lstrip("#")

    def attempt(tok: dict) -> dict:
        broadcaster = tok["user_id"]
        if channel != str(tok.get("login", "")).lower():
            users = _get(f"/users?login={urllib.parse.quote(channel)}", tok["access_token"], cid).get("data") or []
            if not users:
                raise TwitchAuthError(f"No Twitch channel called {channel}.")
            broadcaster = users[0]["id"]
        return _post_json("/chat/messages", {"broadcaster_id": broadcaster, "sender_id": tok["user_id"],
                                             "message": text[:500]}, tok["access_token"], cid)

    try:
        reply = attempt(tokens)
    except PermissionError:
        reply = attempt(_refresh(tokens, cid))
    sent = (reply.get("data") or [{}])[0]
    if not sent.get("is_sent", False):
        reason = (sent.get("drop_reason") or {}).get("message") or "Twitch didn't post it"
        raise TwitchAuthError(reason)


# Moderating chat ---------------------------------------------------------------------------


class NeedsReconnect(TwitchAuthError):
    """Twitch was connected before SprigCast asked for this: connecting again once allows it."""


def _send(method: str, path: str, body: dict | None, token: str, cid: str) -> dict:
    head = {"Authorization": f"Bearer {token}", "Client-Id": cid}
    if body is not None:
        head["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{HELIX}{path}", data=None if body is None else json.dumps(body).encode(),
                                     method=method, headers=head)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("token rejected") from exc
        try:
            message = str(json.loads(exc.read() or b"{}").get("message", ""))
        except ValueError:
            message = ""
        if exc.code == 403:
            raise TwitchAuthError("You're not a moderator in this chat.") from exc
        raise TwitchAuthError(_plain(message) or f"Twitch said {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TwitchAuthError(f"Couldn't reach Twitch: {exc}") from exc


def _plain(message: str) -> str:
    """Twitch's own reason, in short words where SprigCast knows it."""
    low = message.lower()
    if "already banned" in low:
        return "Already banned."
    if "may not be banned" in low or "cannot be banned" in low:
        return "Twitch won't ban or time out that account."
    if "message" in low and ("not found" in low or "too old" in low or "broadcaster" in low or "moderator" in low):
        return "Twitch won't delete that message (too old, or from a moderator)."
    return message[:120]


MOD_ACTIONS = ("timeout", "ban", "delete", "block")


def moderate(channel: str, action: str, *, user_id: str = "", message_id: str = "", seconds: int = 0,
             cid: str | None = None) -> None:
    """Time out or ban an account in `channel`'s chat, delete one message there, or block an account, as the
    connected account. Worker thread. Raises TwitchAuthError worded for the user (NeedsReconnect when the
    connection is older than this permission)."""
    if action not in MOD_ACTIONS:
        raise ValueError(action)
    cid = cid or client_id()
    tokens = _load()
    if not tokens:
        raise NeedsReconnect("Sign in to Twitch to moderate chat.")
    needed = MOD_SCOPES[2] if action == "block" else MOD_SCOPES[1] if action == "delete" else MOD_SCOPES[0]
    if needed not in (tokens.get("scope") or []):
        raise NeedsReconnect("Sign in to Twitch again once to allow moderation (Settings → Stream).")
    if time.time() >= float(tokens.get("expires_at", 0)):
        tokens = _refresh(tokens, cid)
    channel = channel.lower().lstrip("#")
    quote = urllib.parse.quote

    def attempt(tok: dict) -> None:
        if action == "block":
            _send("PUT", f"/users/blocks?target_user_id={quote(user_id)}", None, tok["access_token"], cid)
            return
        broadcaster = tok["user_id"]
        if channel != str(tok.get("login", "")).lower():
            users = _get(f"/users?login={quote(channel)}", tok["access_token"], cid).get("data") or []
            if not users:
                raise TwitchAuthError(f"No Twitch channel called {channel}.")
            broadcaster = users[0]["id"]
        where = f"broadcaster_id={broadcaster}&moderator_id={tok['user_id']}"
        if action == "delete":
            _send("DELETE", f"/moderation/chat?{where}&message_id={quote(message_id)}", None, tok["access_token"], cid)
            return
        data: dict = {"user_id": user_id}
        if action == "timeout":
            data["duration"] = max(1, min(int(seconds), 1_209_600))  # Twitch's longest timeout is two weeks
        _send("POST", f"/moderation/bans?{where}", {"data": data}, tok["access_token"], cid)

    try:
        try:
            attempt(tokens)
        except PermissionError:
            attempt(_refresh(tokens, cid))
    except PermissionError as exc:
        raise NeedsReconnect("Sign in to Twitch again to allow moderation (Settings → Stream).") from exc
    log.info("twitch chat: %s done", action)  # in the logbook, no names


# The mod bar --------------------------------------------------------------------------------------------

CHAT_MODES = ("slow_mode", "follower_mode", "subscriber_mode", "emote_mode")
SLOW_SECONDS = 30  # Slow on: one message every 30 seconds (Twitch allows 3 to 120)


def _as_moderator(channel: str, scope: str, call, cid: str | None = None):
    """call(token, where, broadcaster_id) as the connected account in `channel`'s chat, where = the
    broadcaster_id and moderator_id query. NeedsReconnect when the connection is older than `scope`."""
    cid = cid or client_id()
    tokens = _load()
    if not tokens:
        raise NeedsReconnect("Sign in to Twitch to moderate chat.")
    if scope not in (tokens.get("scope") or []):
        raise NeedsReconnect("Sign in to Twitch again once to allow the mod bar (Settings → Stream).")
    if time.time() >= float(tokens.get("expires_at", 0)):
        tokens = _refresh(tokens, cid)
    channel = channel.lower().lstrip("#")

    def attempt(tok: dict):
        broadcaster = tok["user_id"]
        if channel != str(tok.get("login", "")).lower():
            users = _get(f"/users?login={urllib.parse.quote(channel)}", tok["access_token"], cid).get("data") or []
            if not users:
                raise TwitchAuthError(f"No Twitch channel called {channel}.")
            broadcaster = users[0]["id"]
        return call(tok["access_token"], f"broadcaster_id={broadcaster}&moderator_id={tok['user_id']}", cid)

    try:
        try:
            return attempt(tokens)
        except PermissionError:
            return attempt(_refresh(tokens, cid))
    except PermissionError as exc:
        raise NeedsReconnect("Sign in to Twitch again to allow the mod bar (Settings → Stream).") from exc


def chat_modes(channel: str) -> dict:
    """{"slow_mode", "follower_mode", "subscriber_mode", "emote_mode", "shield": bool} for `channel`. Worker."""
    def call(token, where, cid):
        found = (_send("GET", f"/chat/settings?{where}", None, token, cid).get("data") or [{}])[0]
        out = {mode: bool(found.get(mode)) for mode in CHAT_MODES}
        try:
            shield = (_send("GET", f"/moderation/shield_mode?{where}", None, token, cid).get("data") or [{}])[0]
            out["shield"] = bool(shield.get("is_active"))
        except TwitchAuthError:
            out["shield"] = False
        return out

    return _as_moderator(channel, SETTINGS_SCOPE, call)


def set_chat_mode(channel: str, mode: str, on: bool) -> None:
    """Turn one of CHAT_MODES, or "shield", on or off in `channel`'s chat. Worker."""
    if mode == "shield":
        _as_moderator(channel, SHIELD_SCOPE, lambda token, where, cid: _send(
            "PUT", f"/moderation/shield_mode?{where}", {"is_active": bool(on)}, token, cid))
    elif mode in CHAT_MODES:
        body: dict = {mode: bool(on)}
        if mode == "slow_mode" and on:
            body["slow_mode_wait_time"] = SLOW_SECONDS
        _as_moderator(channel, SETTINGS_SCOPE, lambda token, where, cid: _send(
            "PATCH", f"/chat/settings?{where}", body, token, cid))
    else:
        raise ValueError(mode)
    log.info("twitch chat: %s %s", mode, "on" if on else "off")


def clear_chat(channel: str) -> None:
    """Remove every message in `channel`'s chat (Twitch's /clear). Worker."""
    _as_moderator(channel, MOD_SCOPES[1], lambda token, where, cid: _send(
        "DELETE", f"/moderation/chat?{where}", None, token, cid))
    log.info("twitch chat: cleared")
