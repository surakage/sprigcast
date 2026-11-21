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
Developer → Create app, redirect URL exactly REDIRECT below; scopes user:read channel:read channel:write streamkey:read.
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
    HELPER_TIMEOUT,
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
# One Kick sign-in serves the destination (stream key) and Stream details (title, category, tags).
KICK_SCOPES = "user:read channel:read channel:write streamkey:read"
KICK_DETAILS_SCOPES = KICK_SCOPES
# Chat back and moderation on Kick are asked for only once you choose them in the Chat panel (KICK_POWERS on):
# a sign-in that asks for more than the Kick app allows fails, and the stream key must never depend on chat.
KICK_CHAT_SCOPES = "chat:write moderation:ban"
KICK_POWERS = "kick/chat_powers"  # QSettings


def kick_scopes() -> str:
    from PySide6.QtCore import QSettings

    on = QSettings().value(KICK_POWERS, False, type=bool)
    return f"{KICK_SCOPES} {KICK_CHAT_SCOPES}" if on else KICK_SCOPES


def _powers_refused(service: str) -> None:
    """A Kick sign-in that asked for chat and moderation didn't finish: the next one asks for the usual only."""
    from PySide6.QtCore import QSettings

    if service == "kick" and QSettings().value(KICK_POWERS, False, type=bool):
        QSettings().setValue(KICK_POWERS, False)
        log.info("sign-in: kick didn't finish with chat and moderation asked for; next sign-in asks without them")
CHAT_NAMES = "chat/names"  # QSettings, as app/chat_panel.NAMES: the channels the Chat panel reads
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


def _swap(service: str, code: str, verifier: str, redirect: str, who: str, keep: bool = False) -> dict:
    """The sign-in code for a token, through the helper (it holds the secret). keep: a refresh token too."""
    fields = {"service": service, "code": code, "code_verifier": verifier, "redirect_uri": redirect}
    if keep:
        fields["keep"] = "1"
    reply = post_form(helper_url(), fields, "SprigCast's sign-in helper", timeout=HELPER_TIMEOUT, tries=2)
    if "error" in reply or not reply.get("access_token"):
        raise SignInError(f"{who} sign-in failed: {problem_text(reply).rstrip('.')}.")
    return reply


@dataclass
class Flow:
    """One sign-in. ``begin`` makes it on the UI thread (settings, the loopback server, the page to open);
    the browser is opened there too; ``finish`` then runs on a worker. It holds no Qt objects, so a worker
    that outlives its window never frees one off the UI thread."""

    service: str
    url: str  # the sign-in page to open in the browser
    waiter: Waiter
    verifier: str
    keep: bool = False  # Stream details: stay signed in instead of fetching the key
    vertical: bool = False  # YouTube: the key for a vertical destination (Dual stream needs a second key)

    def finish(self, cancelled: threading.Event | None = None) -> Fetched:
        """Wait for the browser, then read the account and key. Raises SignInError / Cancelled."""
        who = {"youtube": "Google", "kick": "Kick"}[self.service]
        try:
            code = self.waiter.wait(WAIT_SECONDS, cancelled, who)
            if self.keep:
                fetched = _keep_rest(self, code)
            else:
                fetched = (_youtube_rest if self.service == "youtube" else _kick_rest)(self, code)
        except Cancelled:
            log.info("sign-in: %s canceled", self.service)
            _powers_refused(self.service)
            raise
        except SignInError as exc:
            log.info("sign-in: %s failed: %s", self.service, exc)
            _powers_refused(self.service)
            raise
        except Exception:
            log.exception("sign-in: %s failed", self.service)
            raise
        log.info("sign-in: %s done, %s", self.service, "kept for stream details" if self.keep else "key fetched")
        return fetched


def begin(service: str, keep: bool = False, vertical: bool = False) -> Flow:
    """Start a sign-in: read the Client ID and open the loopback server. Run it on the UI thread.

    Every sign-in stays signed in for Stream details and names your channel for chat; keep: Stream details
    only (no stream key is read)."""
    cid = setting(f"{service}/client_id")
    label = {"youtube": "YouTube", "kick": "Kick"}[service]
    if not cid:
        raise SignInError(f"{label} sign-in isn't set up in this copy of SprigCast. Paste the key instead.")
    verifier, challenge = pkce_pair()
    if service == "youtube":
        waiter = Waiter(free_port(), label="YouTube")
        params = {"client_id": cid, "redirect_uri": waiter.url, "response_type": "code", "scope": YOUTUBE_SCOPE,
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": waiter.state,
                  # One sign-in for the key and Stream details: Google hands a refresh token only to an
                  # offline sign-in, and only with consent shown.
                  "access_type": "offline", "prompt": "consent select_account"}
        auth = YOUTUBE_AUTH
    else:
        waiter = Waiter(KICK_PORT, host="localhost", label="Kick")
        params = {"response_type": "code", "client_id": cid, "redirect_uri": waiter.url,
                  "scope": kick_scopes(),
                  "code_challenge": challenge, "code_challenge_method": "S256", "state": waiter.state}
        auth = KICK_AUTH
    try:
        waiter.start()
    except OSError as exc:  # the port is taken
        log.info("sign-in: %s couldn't listen on port %d: %s", service, waiter.port, exc)
        raise SignInError(f"Port {waiter.port} is busy, so {label} sign-in can't finish. Close what uses it and "
                          "try again.") from exc
    log.info("sign-in: %s started, waiting on port %d", service, waiter.port)
    return Flow(service, f"{auth}?{urllib.parse.urlencode(params)}", waiter, verifier, keep, vertical)


def _run(service: str, cancelled: threading.Event | None, open_url: Callable[[str], object]) -> Fetched:
    flow = begin(service)
    open_url(flow.url)
    return flow.finish(cancelled)


def youtube(cancelled: threading.Event | None = None, open_url: Callable[[str], object] = webbrowser.open) -> Fetched:
    """Sign in with Google: your channel name and a reusable stream key (one is made if there's none)."""
    return _run("youtube", cancelled, open_url)


def _keep_rest(flow: Flow, code: str) -> Fetched:
    """Stream details: keep the sign-in (in the credential store) and name the account. No key is read."""
    from sprigcast.output import stream_details

    who = {"youtube": "Google", "kick": "Kick"}[flow.service]
    reply = _swap(flow.service, code, flow.verifier, flow.waiter.url, who, keep=True)
    token = reply["access_token"]
    if flow.service == "youtube":
        snippet = _youtube_channel(token)
        account, chat = snippet.get("title", "YouTube"), str(snippet.get("customUrl") or "")
    else:
        channels = get_json(f"{KICK_API}/channels", token, "Kick").get("data") or []
        chat = str((channels[0] if channels else {}).get("slug") or "")
        account = chat or "Kick"
    try:
        stream_details.save_tokens(flow.service, reply, account)
    except stream_details.DetailsError as exc:
        raise SignInError(str(exc)) from exc
    _name_chat(flow.service, chat)
    return Fetched(account=account, key="")


def _youtube_channel(token: str) -> dict:
    channels = get_json(f"{YOUTUBE_API}/channels?part=snippet&mine=true", token, "YouTube").get("items") or []
    if not channels:
        raise SignInError("That Google account has no YouTube channel yet.")
    return channels[0].get("snippet") or {}


def _keep(service: str, reply: dict, account: str, chat: str) -> None:
    """A destination's sign-in signs Stream details in too and names your chat. Never fails the key."""
    from sprigcast.output import stream_details

    try:
        stream_details.save_tokens(service, reply, account)
    except Exception:  # noqa: BLE001 - the key is what this sign-in is for; Stream details can sign in itself
        log.info("sign-in: %s key fetched, but the sign-in wasn't kept for stream details", service, exc_info=True)
    _name_chat(service, chat)


def _name_chat(service: str, name: str) -> None:
    """Chat reads this channel from now on, unless you typed one for it yourself."""
    if not name:
        return
    try:
        import json

        from PySide6.QtCore import QSettings

        settings = QSettings()
        try:
            names = json.loads(str(settings.value(CHAT_NAMES, "") or "{}"))
        except ValueError:
            names = {}
        if not isinstance(names, dict) or names.get(service):
            return
        from sprigcast.core import privacy

        privacy.remember_name(name)  # never in the log
        names[service] = name
        settings.setValue(CHAT_NAMES, json.dumps(names))
        log.info("sign-in: %s chat follows the signed-in channel", service)
        from sprigcast.output import stream_details

        stream_details.announce(service)  # the Chat panel starts reading it now
    except Exception:  # noqa: BLE001
        log.info("sign-in: couldn't name the %s chat", service, exc_info=True)


CHAT_NAMED = "{}/chat_named"  # QSettings: chat was pointed at this sign-in's channel once (name_chat_from_sign_in)


def name_chat_from_sign_in(service: str) -> bool:
    """For a sign-in made before sign-ins named the chat (#603): point Chat at the signed-in channel, once.
    Worker thread (YouTube asks for the channel's @handle). True when chat got a name."""
    import json

    from PySide6.QtCore import QSettings

    from sprigcast.output import stream_details

    settings = QSettings()
    if service not in ("youtube", "kick") or settings.value(CHAT_NAMED.format(service), False, type=bool):
        return False
    account = stream_details.account(service)
    if not account:
        return False
    try:
        names = json.loads(str(settings.value(CHAT_NAMES, "") or "{}"))
    except ValueError:
        names = {}
    if isinstance(names, dict) and names.get(service):
        settings.setValue(CHAT_NAMED.format(service), True)  # already reading a channel there
        return False
    if service == "kick":
        name = account if account != "Kick" else ""
    else:
        try:
            token = stream_details.access_token("youtube")
            name = str(_youtube_channel(token).get("customUrl") or "")
        except stream_details.NeedsSignIn:
            return False  # signed out meanwhile: the next sign-in names the chat itself
        except Exception as exc:  # noqa: BLE001 - offline, quota: try again next run
            log.info("sign-in: couldn't look up the YouTube channel for chat (%s)", exc)
            return False
    settings.setValue(CHAT_NAMED.format(service), True)
    if not name:
        return False
    _name_chat(service, name)
    return True


def _youtube_rest(flow: Flow, code: str) -> Fetched:
    reply = _swap("youtube", code, flow.verifier, flow.waiter.url, "Google", keep=True)
    token = reply["access_token"]
    log.info("sign-in: youtube token received")
    snippet = _youtube_channel(token)
    account = snippet.get("title", "YouTube")
    key = _youtube_key(token, flow.vertical)
    _keep("youtube", reply, account, str(snippet.get("customUrl") or ""))
    return Fetched(account=account, key=key)


VERTICAL_KEY_NAME = "SprigCast Vertical"


def pick_youtube_key(streams: list, saved: str | None = None, waiting: tuple[str, ...] = ()) -> tuple[str, str]:
    """Which of the channel's stream keys to send to: (key, why), ("", "") when there is none.

    A channel can have several reusable keys and only one a stream is waiting on shows the stream. The first
    one listed was taken before, and a channel with an old key listed first sent to a dead one: YouTube closed
    the connection five times with nothing but "I/O error" (live 2026-11-21). waiting: the ids of the keys that
    YouTube's live and waiting streams are bound to, the live one and the soonest first. So: the saved key while
    a stream waits on it; else the first key a stream waits on; else the saved key while the channel still has
    it; else the first."""
    def key(stream) -> str:
        return str(((stream.get("cdn") or {}).get("ingestionInfo") or {}).get("streamName") or "")

    keys = [s for s in streams if key(s)]
    mine = next((s for s in keys if saved and key(s) == saved), None)
    if mine is not None and mine.get("id") in waiting:
        return saved, "the saved key, which a YouTube stream is waiting on"
    for stream_id in waiting:
        bound = next((s for s in keys if s.get("id") == stream_id), None)
        if bound is not None:
            return key(bound), "the key your next YouTube stream is waiting on"
    if mine is not None:
        return saved, "the saved key, still one of the channel's"
    return (key(keys[0]), "the channel's first reusable key") if keys else ("", "")


def _youtube_waiting(token: str) -> tuple[str, ...]:
    """The ids of the stream keys YouTube's live and waiting streams are bound to: live first, then the soonest.
    () when there are none or YouTube didn't say."""
    from sprigcast.output import stream_details

    part, found = "id,snippet,contentDetails", []
    try:
        for status in ("active", "upcoming"):
            items = stream_details._yt(token, "GET", f"liveBroadcasts?part={part}&broadcastStatus={status}"
                                                     "&broadcastType=all&maxResults=50").get("items") or []
            items.sort(key=lambda b: (b.get("snippet") or {}).get("scheduledStartTime") or "9999")
            found += [str((b.get("contentDetails") or {}).get("boundStreamId") or "") for b in items]
    except stream_details.DetailsError as exc:
        log.info("sign-in: couldn't look up the waiting YouTube streams (%s)", exc)
    return tuple(dict.fromkeys(i for i in found if i))


def _youtube_key(token: str, vertical: bool = False, saved: str | None = None) -> str:
    """The channel's stream key to send to (pick_youtube_key), or a new one called SprigCast. vertical: the key
    called SprigCast Vertical (made when there's none), never the horizontal one: YouTube's Dual stream takes the
    vertical feed on a key of its own, picked in Live Control Room. saved: the key in use now."""
    import json
    import urllib.request

    try:
        streams = get_json(f"{YOUTUBE_API}/liveStreams?part=id,cdn,snippet,contentDetails&mine=true&maxResults=50",
                           token, "YouTube").get("items") or []
    except SignInError as exc:
        if "not enabled for live streaming" in str(exc).lower():
            raise SignInError(LIVE_NOT_ON) from exc
        raise
    usable = [s for s in streams if (s.get("contentDetails") or {}).get("isReusable", True)
              and (s.get("cdn") or {}).get("ingestionType", "rtmp") == "rtmp"]
    named = [s for s in usable if (s.get("snippet") or {}).get("title") == VERTICAL_KEY_NAME]
    usable = named if vertical else [s for s in usable if s not in named]
    waiting = _youtube_waiting(token) if len(usable) > 1 else ()  # one key: nothing to choose
    key, why = pick_youtube_key(usable, saved, waiting)
    if key:
        log.info("sign-in: youtube using %s (%d reusable key%s)", why, len(usable), "" if len(usable) == 1 else "s")
        return key
    log.info("sign-in: youtube has no reusable stream key, making one")
    body = {"snippet": {"title": VERTICAL_KEY_NAME if vertical else "SprigCast"},
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
    reply = _swap("kick", code, flow.verifier, flow.waiter.url, "Kick", keep=True)
    token = reply["access_token"]
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
    slug = str(channel.get("slug") or "")
    _keep("kick", reply, slug or "Kick", slug)
    return Fetched(account=slug or "Kick", key=stream["key"], server=str(stream.get("url") or ""))


SIGN_IN = {"youtube": youtube, "kick": kick}


def key_from_account(service: str, vertical: bool = False, saved: str | None = None) -> Fetched:
    """The stream key (and Kick's server) through the account signed in on that platform: no new sign-in.
    Worker thread. Raises SignInError, worded for the person. saved: the key in use now (YouTube keeps it only
    while it is still the right one, pick_youtube_key)."""
    from sprigcast.output import stream_details

    def call(token: str) -> Fetched:
        if service == "youtube":
            account = _youtube_channel(token).get("title", "YouTube")
            return Fetched(account=account, key=_youtube_key(token, vertical, saved))
        channels = get_json(f"{KICK_API}/channels", token, "Kick").get("data") or []
        channel = channels[0] if channels else {}
        stream = channel.get("stream") or {}
        if not stream.get("key"):
            raise SignInError("Kick didn't share the stream key. Sign out in Settings → Stream and sign in again.")
        slug = str(channel.get("slug") or "Kick")
        return Fetched(account=slug, key=stream["key"], server=str(stream.get("url") or ""))

    try:
        fetched = stream_details._with_token(service, call)
    except stream_details.DetailsError as exc:
        raise SignInError(str(exc)) from exc
    log.info("sign-in: %s key read through the signed-in account", service)
    return fetched
