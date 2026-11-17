"""Sign in to YouTube or Kick once to fetch the stream key (and Kick's server), so nothing is pasted.

Both use a browser sign-in (output/oauth_loopback.py). SprigCast only reads the key: it's saved like a
pasted key and the sign-in tokens are thrown away, so there's nothing to refresh or expire later. Sign in
again to fetch a new key.

SprigCast is registered with each service (the public Client IDs below; QSettings "youtube/client_id" and
"kick/client_id" override them). The client secrets are NOT in the app: the last step of sign-in (code ->
token) goes through SprigCast's sign-in helper at signin.slothbloom.com (integrations/signin-helper), which
adds the secret. With no Client ID the sign-in isn't offered and the key is pasted.

YouTube: Google Cloud project, YouTube Data API v3 on, OAuth client of type "Desktop app"; scope
`youtube` (listing and, when there is none, making a reusable stream key). Kick: kick.com → Settings →
Developer → Create app, redirect URL exactly REDIRECT below; scopes user:read channel:read streamkey:read.
"""

from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.parse
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

from sprigcast.output.oauth_loopback import (
    HEADERS,
    Cancelled,
    SignInError,
    Waiter,
    free_port,
    get_json,
    pkce_pair,
    post_form,
    problem_text,
)

log = logging.getLogger(__name__)

WAIT_SECONDS = 300

# SprigCast's public Client IDs (see the module notes); QSettings overrides them. No secrets here.
DEFAULTS = {
    "youtube/client_id": "497399863565-mmjhhrvqkdqvhvucg38orn04sa0cnobp.apps.googleusercontent.com",
    "kick/client_id": "01M7JQPKVG11WQPN9YZDNZV8KW",
}
HELPER_URL = "https://signin.slothbloom.com/v1/token"  # SPRIGCAST_SIGNIN_URL overrides it for testing

YOUTUBE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"

KICK_AUTH = "https://id.kick.com/oauth/authorize"
KICK_API = "https://api.kick.com/public/v1"
KICK_SCOPES = "user:read channel:read streamkey:read"
KICK_PORT = 51587  # Kick matches the redirect URL exactly, so it's always this one
KICK_REDIRECT = f"http://localhost:{KICK_PORT}/callback"


LIVE_NOT_ON = ("Live streaming isn't on for this YouTube channel yet. Turn it on at youtube.com/features "
               "(it can take up to a day), then sign in again.")


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
    return service in ("youtube", "kick") and bool(setting(f"{service}/client_id"))


def helper_url() -> str:
    import os

    return os.environ.get("SPRIGCAST_SIGNIN_URL", "").strip() or HELPER_URL


def _swap(service: str, code: str, verifier: str, redirect: str, who: str) -> str:
    """The sign-in code for a token, through the helper (it holds the secret)."""
    reply = post_form(helper_url(), {"service": service, "code": code, "code_verifier": verifier,
                                     "redirect_uri": redirect}, "SprigCast's sign-in helper")
    if "error" in reply or not reply.get("access_token"):
        raise SignInError(f"{who} sign-in failed: {problem_text(reply).rstrip('.')}.")
    return reply["access_token"]


@dataclass
class Flow:
    """One sign-in. ``begin`` makes it on the UI thread (settings, the loopback server, the page to open);
    the browser is opened there too; ``finish`` then runs on a worker. It holds no Qt objects, so a worker
    that outlives its window never frees one off the UI thread."""

    service: str
    url: str  # the sign-in page to open in the browser
    waiter: Waiter
    verifier: str

    def finish(self, cancelled: threading.Event | None = None) -> Fetched:
        """Wait for the browser, then read the account and key. Raises SignInError / Cancelled."""
        who = {"youtube": "Google", "kick": "Kick"}[self.service]
        try:
            code = self.waiter.wait(WAIT_SECONDS, cancelled, who)
            fetched = (_youtube_rest if self.service == "youtube" else _kick_rest)(self, code)
        except Cancelled:
            log.info("sign-in: %s cancelled", self.service)
            raise
        except SignInError as exc:
            log.info("sign-in: %s failed: %s", self.service, exc)
            raise
        except Exception:
            log.exception("sign-in: %s failed", self.service)
            raise
        log.info("sign-in: %s done, key fetched for %s", self.service, fetched.account)
        return fetched


def begin(service: str) -> Flow:
    """Start a sign-in: read the Client ID and open the loopback server. Run it on the UI thread."""
    cid = setting(f"{service}/client_id")
    label = {"youtube": "YouTube", "kick": "Kick"}[service]
    if not cid:
        raise SignInError(f"{label} sign-in isn't set up in this copy of SprigCast. Paste the key instead.")
    verifier, challenge = pkce_pair()
    if service == "youtube":
        waiter = Waiter(free_port(), label="YouTube")
        params = {"client_id": cid, "redirect_uri": waiter.url, "response_type": "code", "scope": YOUTUBE_SCOPE,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": waiter.state,
                  "prompt": "select_account"}
        auth = YOUTUBE_AUTH
    else:
        waiter = Waiter(KICK_PORT, host="localhost", label="Kick")
        params = {"response_type": "code", "client_id": cid, "redirect_uri": waiter.url, "scope": KICK_SCOPES,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": waiter.state}
        auth = KICK_AUTH
    try:
        waiter.start()
    except OSError as exc:  # the port is taken
        log.info("sign-in: %s couldn't listen on port %d: %s", service, waiter.port, exc)
        raise SignInError(f"Port {waiter.port} is busy, so {label} sign-in can't finish. Close what uses it and "
                          "try again.") from exc
    log.info("sign-in: %s started, waiting on port %d", service, waiter.port)
    return Flow(service, f"{auth}?{urllib.parse.urlencode(params)}", waiter, verifier)


def _run(service: str, cancelled: threading.Event | None, open_url: Callable[[str], object]) -> Fetched:
    flow = begin(service)
    open_url(flow.url)
    return flow.finish(cancelled)


def youtube(cancelled: threading.Event | None = None, open_url: Callable[[str], object] = webbrowser.open) -> Fetched:
    """Sign in with Google: your channel name and a reusable stream key (one is made if there's none)."""
    return _run("youtube", cancelled, open_url)


def _youtube_rest(flow: Flow, code: str) -> Fetched:
    token = _swap("youtube", code, flow.verifier, flow.waiter.url, "Google")
    log.info("sign-in: youtube token received")
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

    try:
        streams = get_json(f"{YOUTUBE_API}/liveStreams?part=cdn,snippet,contentDetails&mine=true&maxResults=50",
                           token, "YouTube").get("items") or []
    except SignInError as exc:
        if "not enabled for live streaming" in str(exc).lower():
            raise SignInError(LIVE_NOT_ON) from exc
        raise
    usable = [s for s in streams if (s.get("contentDetails") or {}).get("isReusable", True)
              and (s.get("cdn") or {}).get("ingestionType", "rtmp") == "rtmp"]
    if usable:
        log.info("sign-in: youtube using an existing reusable stream key")
        return usable[0]["cdn"]["ingestionInfo"]["streamName"]
    log.info("sign-in: youtube has no reusable stream key, making one")
    body = {"snippet": {"title": "SprigCast"},
            "cdn": {"frameRate": "variable", "ingestionType": "rtmp", "resolution": "variable"},
            "contentDetails": {"isReusable": True}}
    request = urllib.request.Request(f"{YOUTUBE_API}/liveStreams?part=snippet,cdn,contentDetails",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                              **HEADERS})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            made = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")
        if "liveStreamingNotEnabled" in text or "livePermissionBlocked" in text:
            raise SignInError(LIVE_NOT_ON) from exc
        raise SignInError(f"YouTube said {exc.code} making a stream key.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SignInError(f"Couldn't reach YouTube: {exc}") from exc
    return made["cdn"]["ingestionInfo"]["streamName"]


def kick(cancelled: threading.Event | None = None, open_url: Callable[[str], object] = webbrowser.open) -> Fetched:
    """Sign in with Kick: your username, stream URL and stream key."""
    return _run("kick", cancelled, open_url)


def _kick_rest(flow: Flow, code: str) -> Fetched:
    token = _swap("kick", code, flow.verifier, flow.waiter.url, "Kick")
    log.info("sign-in: kick token received")
    try:
        channels = get_json(f"{KICK_API}/channels", token, "Kick").get("data") or []
    except SignInError as exc:
        if (" 403" in str(exc) or " 401" in str(exc)) and "Cloudflare" not in str(exc):
            raise SignInError(f"{exc}. Sign in again and allow every permission Kick asks for "
                              "(channel and stream key).") from exc
        raise
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
