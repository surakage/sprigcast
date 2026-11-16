"""Sign in to YouTube or Kick once to fetch the stream key (and Kick's server), so nothing is pasted.

Both use a browser sign-in (output/oauth_loopback.py). SprigCast only reads the key: it's saved like a
pasted key and the sign-in tokens are thrown away, so there's nothing to refresh or expire later. Sign in
again to fetch a new key.

Each service needs SprigCast registered with it once (a Client ID, and for Kick a Client Secret, which a
desktop app can't truly keep secret). They're set below or in QSettings ("youtube/client_id",
"kick/client_id", "kick/client_secret", ...). Until then the sign-in isn't offered and the key is pasted.

YouTube: Google Cloud project, YouTube Data API v3 on, OAuth client of type "Desktop app"; scope
`youtube` (listing and, when there is none, making a reusable stream key). Kick: kick.com → Settings →
Developer → Create app, redirect URL exactly REDIRECT below; scopes user:read channel:read streamkey:read.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

from sprigcast.output.oauth_loopback import SignInError, Waiter, free_port, get_json, pkce_pair, post_form

WAIT_SECONDS = 300

# Filled in once SprigCast is registered (see the module notes); QSettings overrides them.
DEFAULTS = {
    "youtube/client_id": "",
    "youtube/client_secret": "",  # Google "Desktop app" clients: not confidential, sent when set
    "kick/client_id": "",
    "kick/client_secret": "",  # Kick's token endpoint needs it even with PKCE
}

YOUTUBE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
YOUTUBE_TOKEN = "https://oauth2.googleapis.com/token"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"

KICK_AUTH = "https://id.kick.com/oauth/authorize"
KICK_TOKEN = "https://id.kick.com/oauth/token"
KICK_API = "https://api.kick.com/public/v1"
KICK_SCOPES = "user:read channel:read streamkey:read"
KICK_PORT = 51587  # Kick matches the redirect URL exactly, so it's always this one
KICK_REDIRECT = f"http://localhost:{KICK_PORT}/callback"


@dataclass
class Fetched:
    account: str
    key: str
    server: str = ""  # empty: the service's usual server


def setting(name: str) -> str:
    try:
        from PySide6.QtCore import QSettings

        value = str(QSettings().value(name, "") or "").strip()
    except Exception:  # noqa: BLE001
        value = ""
    return value or DEFAULTS.get(name, "")


def available(service: str) -> bool:
    """Is sign-in offered for this service (SprigCast registered with it)?"""
    if service == "youtube":
        return bool(setting("youtube/client_id"))
    if service == "kick":
        return bool(setting("kick/client_id") and setting("kick/client_secret"))
    return False


def _browser_code(auth_url: str, params: dict, waiter: Waiter, cancelled: threading.Event | None,
                  open_url: Callable[[str], object]) -> str:
    waiter.start()
    open_url(f"{auth_url}?{urllib.parse.urlencode(params)}")
    return waiter.wait(WAIT_SECONDS, cancelled)


def youtube(cancelled: threading.Event | None = None, open_url: Callable[[str], object] = webbrowser.open) -> Fetched:
    """Sign in with Google: your channel name and a reusable stream key (one is made if there's none)."""
    cid, secret = setting("youtube/client_id"), setting("youtube/client_secret")
    if not cid:
        raise SignInError("YouTube sign-in isn't set up in this copy of SprigCast. Paste the key instead.")
    verifier, challenge = pkce_pair()
    waiter = Waiter(free_port())
    code = _browser_code(YOUTUBE_AUTH, {
        "client_id": cid, "redirect_uri": waiter.url, "response_type": "code", "scope": YOUTUBE_SCOPE,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": waiter.state,
        "prompt": "select_account"}, waiter, cancelled, open_url)
    fields = {"client_id": cid, "code": code, "code_verifier": verifier, "grant_type": "authorization_code",
              "redirect_uri": waiter.url}
    if secret:
        fields["client_secret"] = secret
    reply = post_form(YOUTUBE_TOKEN, fields, "Google")
    if "error" in reply or not reply.get("access_token"):
        raise SignInError(f"Google sign-in failed: {reply.get('error_description') or reply.get('error')}")
    token = reply["access_token"]
    channels = get_json(f"{YOUTUBE_API}/channels?part=snippet&mine=true", token, "YouTube").get("items") or []
    if not channels:
        raise SignInError("That Google account has no YouTube channel yet.")
    account = channels[0]["snippet"].get("title", "YouTube")
    key = _youtube_key(token)
    _revoke("https://oauth2.googleapis.com/revoke", {"token": token})
    return Fetched(account=account, key=key)


def _youtube_key(token: str) -> str:
    """The first reusable stream key, or a new one called SprigCast."""
    import json
    import urllib.request

    streams = get_json(f"{YOUTUBE_API}/liveStreams?part=cdn,snippet,contentDetails&mine=true&maxResults=50",
                       token, "YouTube").get("items") or []
    usable = [s for s in streams if (s.get("contentDetails") or {}).get("isReusable", True)
              and (s.get("cdn") or {}).get("ingestionType", "rtmp") == "rtmp"]
    if usable:
        return usable[0]["cdn"]["ingestionInfo"]["streamName"]
    body = {"snippet": {"title": "SprigCast"},
            "cdn": {"frameRate": "variable", "ingestionType": "rtmp", "resolution": "variable"},
            "contentDetails": {"isReusable": True}}
    request = urllib.request.Request(f"{YOUTUBE_API}/liveStreams?part=snippet,cdn,contentDetails",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            made = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        if "liveStreamingNotEnabled" in text or "livePermissionBlocked" in text:
            raise SignInError("Live streaming isn't on for this channel yet: turn it on in YouTube Studio "
                              "(it can take a day).") from exc
        raise SignInError(f"YouTube said {exc.code} making a stream key.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SignInError(f"Couldn't reach YouTube: {exc}") from exc
    return made["cdn"]["ingestionInfo"]["streamName"]


def kick(cancelled: threading.Event | None = None, open_url: Callable[[str], object] = webbrowser.open) -> Fetched:
    """Sign in with Kick: your username, stream URL and stream key."""
    cid, secret = setting("kick/client_id"), setting("kick/client_secret")
    if not (cid and secret):
        raise SignInError("Kick sign-in isn't set up in this copy of SprigCast. Paste the key instead.")
    verifier, challenge = pkce_pair()
    waiter = Waiter(KICK_PORT, host="localhost")
    try:
        code = _browser_code(KICK_AUTH, {
            "response_type": "code", "client_id": cid, "redirect_uri": waiter.url, "scope": KICK_SCOPES,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": waiter.state},
            waiter, cancelled, open_url)
    except OSError as exc:  # the port is taken
        raise SignInError(f"Port {KICK_PORT} is busy, so Kick sign-in can't finish. Close what uses it and "
                          "try again.") from exc
    reply = post_form(KICK_TOKEN, {"grant_type": "authorization_code", "client_id": cid, "client_secret": secret,
                                   "redirect_uri": waiter.url, "code_verifier": verifier, "code": code}, "Kick")
    if "error" in reply or not reply.get("access_token"):
        raise SignInError(f"Kick sign-in failed: {reply.get('error_description') or reply.get('error')}")
    token = reply["access_token"]
    channels = get_json(f"{KICK_API}/channels", token, "Kick").get("data") or []
    if not channels:
        raise SignInError("Kick didn't return your channel.")
    channel = channels[0]
    stream = channel.get("stream") or {}
    if not stream.get("key"):
        raise SignInError("Kick didn't share the stream key. Sign in again and allow it.")
    _revoke(f"https://id.kick.com/oauth/revoke?{urllib.parse.urlencode({'token': token})}", {})
    return Fetched(account=str(channel.get("slug") or "Kick"), key=stream["key"], server=str(stream.get("url") or ""))


SIGN_IN = {"youtube": youtube, "kick": kick}


def _revoke(url: str, fields: dict) -> None:
    """Throw the sign-in away: SprigCast kept only the key. Best effort."""
    try:
        post_form(url, fields, "the service")
    except SignInError:
        pass
