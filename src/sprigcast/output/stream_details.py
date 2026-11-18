"""Stream details: read and change your title, category and so on, straight on Twitch, YouTube and Kick.

Nothing here runs by itself. The Stream details pop-up (app/stream_details_dialog.py) calls these on a worker
when it opens, when you look up a category and when you press Save. Each call goes straight to the platform's
official API and comes back; a failure is a DetailsError worded for the person and never touches going live.

Sign-ins:
- Twitch reuses the connected account (output/twitch_auth.py). Editing needs `channel:manage:broadcast`, so a
  connection made before it was asked for reconnects once.
- YouTube and Kick sign in once more for this, through the same browser sign-in as the stream key
  (output/service_signin.py, with keep=True). This one stays signed in: the tokens sit in the OS credential
  store ("youtube/oauth", "kick/oauth"), never in scenes.json. A new access token comes through SprigCast's
  sign-in helper, which holds the client secret. Sign out throws them away (and asks the service to forget them).

What each platform takes:
- Twitch: title, category, tags (PATCH helix/channels).
- YouTube: title, description, privacy and thumbnail of the stream that's live now, else the next one waiting.
  With none waiting, Save makes one that starts by itself when you go live, on your SprigCast stream key.
- Kick: title and category (PATCH public/v1/channels).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from sprigcast.output.keys import get_key, set_key

log = logging.getLogger(__name__)

TIMEOUT = 10
SERVICES = ("twitch", "youtube", "kick")
LABELS = {"twitch": "Twitch", "youtube": "YouTube", "kick": "Kick"}
TITLE_MAX = {"twitch": 140, "youtube": 100, "kick": 100}
DESCRIPTION_MAX = 5000
PRIVACY = ("public", "unlisted", "private")
# YouTube's categories a video can be put in (the same everywhere). YouTube picks the game itself from what's
# played: its API has no way to name the game, so Gaming is as close as an app gets.
YOUTUBE_CATEGORIES = {"1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music", "15": "Pets & Animals",
                      "17": "Sports", "19": "Travel & Events", "20": "Gaming", "22": "People & Blogs",
                      "23": "Comedy", "24": "Entertainment", "25": "News & Politics", "26": "Howto & Style",
                      "27": "Education", "28": "Science & Technology", "29": "Nonprofits & Activism"}
THUMB_MAX_BYTES = 2 * 1024 * 1024  # YouTube's limit for a custom thumbnail
TWITCH_TAGS_MAX, TWITCH_TAG_LEN = 10, 25

TWITCH_SCOPE = "channel:manage:broadcast"
YOUTUBE_API = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UPLOAD = "https://www.googleapis.com/upload/youtube/v3"
KICK_API = "https://api.kick.com/public/v1"
KICK_API_V2 = "https://api.kick.com/public/v2"
TOKEN_REF = {"youtube": "youtube/oauth", "kick": "kick/oauth"}
ACCOUNT_SETTING = "{}/details_account"  # QSettings: who's signed in for Stream details ("" = nobody). Not secret.

_refresh_lock = threading.Lock()  # Kick hands out a new refresh token each time: one refresh at a time


class DetailsError(Exception):
    """Something went wrong, worded for the person."""


class NeedsSignIn(DetailsError):
    """Not signed in (or the sign-in ran out): the pop-up shows Sign in."""


@dataclass
class Details:
    title: str = ""
    category: str = ""  # its name (Twitch, Kick)
    category_id: str = ""
    tags: list[str] = field(default_factory=list)  # Twitch
    description: str = ""  # YouTube
    privacy: str = "public"  # YouTube
    broadcast_id: str = ""  # YouTube: the stream being edited, "" = none waiting (Save makes one)
    starts: str = ""  # YouTube: its scheduled start, which an update has to send back
    thumbnail: str = ""  # YouTube: the current thumbnail's web address, to show it
    video_category: str = ""  # YouTube: the category id (YOUTUBE_CATEGORIES); "" = leave it as it is
    note: str = ""  # after a save: what didn't go through although the rest did (the thumbnail)


# Who's signed in, without opening the credential store --------------------------------------------------


def account(service: str) -> str:
    """The signed-in account for Stream details, "" when none. Cheap: QSettings only."""
    if service == "twitch":
        from sprigcast.output import twitch_auth

        try:
            login = twitch_auth.remembered_login()
        except Exception:  # noqa: BLE001
            login = None
        return login or ""
    try:
        from PySide6.QtCore import QSettings

        return str(QSettings().value(ACCOUNT_SETTING.format(service), "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _remember(service: str, name: str) -> None:
    try:
        from PySide6.QtCore import QSettings

        QSettings().setValue(ACCOUNT_SETTING.format(service), name)
    except Exception:  # noqa: BLE001
        pass


def twitch_can_edit() -> bool:
    """Was Twitch connected with permission to change stream details? Reads the credential store: use a worker."""
    from sprigcast.output import twitch_auth

    tokens = twitch_auth._load()
    return bool(tokens) and TWITCH_SCOPE in (tokens.get("scope") or [])


# Tokens for YouTube and Kick ---------------------------------------------------------------------------


def save_tokens(service: str, reply: dict, name: str) -> None:
    """Keep a sign-in (the helper's answer) for Stream details. Raises DetailsError without a refresh token."""
    if not reply.get("refresh_token"):
        raise DetailsError(f"{LABELS[service]} didn't let SprigCast stay signed in. Sign in again.")
    tokens = {"access_token": reply["access_token"], "refresh_token": reply["refresh_token"],
              "expires_at": time.time() + int(reply.get("expires_in") or 3600) - 60, "account": name}
    set_key(TOKEN_REF[service], json.dumps(tokens))
    _remember(service, name)
    log.info("stream details: %s signed in", service)


def sign_out(service: str) -> None:
    """Forget the sign-in here and ask the service to forget it too (best effort). Worker thread."""
    if service == "twitch":
        return  # Twitch's connection is shared with the stream key and chat: it's undone in Settings → Stream
    raw = get_key(TOKEN_REF[service])
    try:
        set_key(TOKEN_REF[service], "")
    except Exception:  # noqa: BLE001
        log.warning("stream details: couldn't clear the %s sign-in", service, exc_info=True)
    _remember(service, "")
    if not raw:
        return
    try:
        token = json.loads(raw).get("refresh_token", "")
    except ValueError:
        return
    url = ("https://oauth2.googleapis.com/revoke" if service == "youtube"
           else f"https://id.kick.com/oauth/revoke?{urllib.parse.urlencode({'token': token})}")
    try:
        from sprigcast.output.oauth_loopback import post_form

        post_form(url, {"token": token} if service == "youtube" else {}, LABELS[service])
    except Exception:  # noqa: BLE001
        pass
    log.info("stream details: %s signed out", service)


def _tokens(service: str) -> dict:
    raw = get_key(TOKEN_REF[service])
    if not raw:
        _remember(service, "")
        raise NeedsSignIn(f"Sign in to {LABELS[service]} to change your stream details.")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise NeedsSignIn(f"Sign in to {LABELS[service]} again.") from exc


def _refresh(service: str, old: dict) -> dict:
    from sprigcast.output import service_signin
    from sprigcast.output.oauth_loopback import SignInError, post_form

    with _refresh_lock:
        latest = _tokens(service)
        if latest.get("access_token") != old.get("access_token") and time.time() < float(latest.get("expires_at", 0)):
            return latest  # another refresh finished while this one waited
        try:
            reply = post_form(service_signin.helper_url(), {"service": service, "grant_type": "refresh_token",
                                                             "refresh_token": latest.get("refresh_token", "")},
                              "SprigCast's sign-in helper")
        except SignInError as exc:
            raise DetailsError(str(exc)) from exc
        if "error" in reply or not reply.get("access_token"):
            status = reply.get("status")
            log.info("stream details: %s refresh refused (%s)", service, status)
            if status in (400, 401):  # the sign-in ran out or was taken back: sign in again
                set_key(TOKEN_REF[service], "")
                _remember(service, "")
                raise NeedsSignIn(f"Your {LABELS[service]} sign-in ran out. Sign in again.")
            raise DetailsError(f"Couldn't renew the {LABELS[service]} sign-in right now. Try again in a minute.")
        reply.setdefault("refresh_token", latest.get("refresh_token", ""))  # Google keeps the same one
        save_tokens(service, reply, latest.get("account", ""))
        return _tokens(service)


def _access(service: str) -> dict:
    tokens = _tokens(service)
    if time.time() >= float(tokens.get("expires_at", 0)):
        tokens = _refresh(service, tokens)
    return tokens


# Web calls ---------------------------------------------------------------------------------------------


def _reason(raw: bytes) -> str:
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):  # Google: {"error": {"message", "errors": [{"reason"}]}}
        reasons = [e.get("reason", "") for e in error.get("errors") or [] if isinstance(e, dict)]
        return " ".join(filter(None, [str(error.get("message", "")), *reasons]))
    return str(body.get("message") or body.get("error_description") or error or "")


def _call(method: str, url: str, token: str, body: dict | None = None, *, headers: dict | None = None,
          data: bytes | None = None, who: str = "") -> dict:
    """One API call. 401 raises PermissionError (refresh and try again); other trouble DetailsError."""
    from sprigcast.output.oauth_loopback import HEADERS

    head = {"Authorization": f"Bearer {token}", "Accept": "application/json", **HEADERS, **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode()
        head["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=head)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read()
            return json.loads(raw) if raw and raw.strip()[:1] in (b"{", b"[") else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("token rejected") from exc
        reason = _reason(exc.read() or b"")
        log.info("stream details: %s answered %d to %s %s: %s", who, exc.code, method,
                 url.split("?")[0], reason[:200])
        raise _worded(who, exc.code, reason) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DetailsError(f"Couldn't reach {who}. Check your internet and try again.") from exc


def _worded(who: str, code: int, reason: str) -> DetailsError:
    low = reason.lower()
    if "thumbnail" in low and ("permission" in low or "forbidden" in low or code == 403):
        return DetailsError("YouTube only takes custom thumbnails from verified channels (youtube.com/verify).")
    if "livestreamingnotenabled" in low or "not enabled for live streaming" in low:
        return DetailsError("Live streaming isn't on for this YouTube channel yet (youtube.com/features).")
    if "quotaexceeded" in low.replace(" ", ""):
        return DetailsError("YouTube's daily limit for SprigCast is used up. Try again tomorrow.")
    if code == 403:
        return NeedsSignIn(f"{who} said no. Sign in again and allow every permission it asks for.")
    if code == 429:
        return DetailsError(f"{who} asked SprigCast to slow down. Try again in a minute.")
    text = reason.strip().rstrip(".")
    return DetailsError(f"{who} said {code}" + (f": {text}." if text else "."))


def _with_token(service: str, call):
    """Run call(token) and, when the token was turned away, once more with a fresh one."""
    tokens = _access(service)
    try:
        return call(tokens["access_token"])
    except PermissionError:
        tokens = _refresh(service, tokens)
        try:
            return call(tokens["access_token"])
        except PermissionError as exc:
            raise NeedsSignIn(f"Sign in to {LABELS[service]} again.") from exc


# Twitch ------------------------------------------------------------------------------------------------


def _twitch(call):
    """call(token, client_id, user_id) with the connected Twitch account, refreshing once when needed."""
    from sprigcast.output import twitch_auth

    cid = twitch_auth.client_id()
    tokens = twitch_auth._load()
    if not tokens:
        raise NeedsSignIn("Connect Twitch to change your stream details.")
    if TWITCH_SCOPE not in (tokens.get("scope") or []):
        raise NeedsSignIn("Reconnect Twitch once so SprigCast may change your stream details.")
    try:
        if time.time() >= float(tokens.get("expires_at", 0)):
            tokens = twitch_auth._refresh(tokens, cid)
        try:
            return call(tokens["access_token"], cid, tokens["user_id"])
        except PermissionError:
            tokens = twitch_auth._refresh(tokens, cid)
            return call(tokens["access_token"], cid, tokens["user_id"])
    except twitch_auth.TwitchAuthError as exc:
        raise NeedsSignIn(str(exc)) from exc
    except PermissionError as exc:
        raise NeedsSignIn("Reconnect Twitch in Settings → Stream.") from exc


def _helix(method: str, path: str, token: str, cid: str, body: dict | None = None) -> dict:
    from sprigcast.output.twitch_auth import HELIX

    return _call(method, f"{HELIX}{path}", token, body, headers={"Client-Id": cid}, who="Twitch")


def clean_tags(text: str | list[str]) -> list[str]:
    """Twitch's tags from what was typed: split on commas or spaces, no #, no doubles, at most 10."""
    parts = text if isinstance(text, list) else text.replace(",", " ").split()
    tags: list[str] = []
    for part in parts:
        tag = "".join(ch for ch in str(part).strip().lstrip("#") if ch.isalnum())
        if tag and tag.lower() not in (t.lower() for t in tags):
            tags.append(tag)
    return tags


def tag_problem(tags: list[str]) -> str | None:
    if len(tags) > TWITCH_TAGS_MAX:
        return f"Twitch takes at most {TWITCH_TAGS_MAX} tags."
    long = next((t for t in tags if len(t) > TWITCH_TAG_LEN), None)
    if long:
        return f"Twitch tags are at most {TWITCH_TAG_LEN} letters ({long[:12]}… is longer)."
    return None


def _twitch_load() -> Details:
    def call(token, cid, user_id):
        data = (_helix("GET", f"/channels?broadcaster_id={user_id}", token, cid).get("data") or [{}])[0]
        return Details(title=data.get("title", ""), category=data.get("game_name", ""),
                       category_id=str(data.get("game_id") or ""), tags=list(data.get("tags") or []))

    return _twitch(call)


def _twitch_save(details: Details) -> None:
    tags = clean_tags(details.tags)
    problem = tag_problem(tags)
    if problem:
        raise DetailsError(problem)
    body = {"title": details.title.strip()[:TITLE_MAX["twitch"]], "tags": tags}
    if details.category_id:
        body["game_id"] = details.category_id
    elif not details.category.strip():
        body["game_id"] = "0"  # no category

    def call(token, cid, user_id):
        _helix("PATCH", f"/channels?broadcaster_id={user_id}", token, cid, body)

    _twitch(call)


def _twitch_search(text: str) -> list[tuple[str, str]]:
    query = urllib.parse.quote(text.strip())

    def call(token, cid, _user_id):
        found = _helix("GET", f"/search/categories?query={query}&first=12", token, cid).get("data") or []
        return [(str(c["id"]), c["name"]) for c in found if c.get("id") and c.get("name")]

    return _twitch(call)


# YouTube -----------------------------------------------------------------------------------------------


def _yt(token: str, method: str, path: str, body: dict | None = None) -> dict:
    return _call(method, f"{YOUTUBE_API}/{path}", token, body, who="YouTube")


def _youtube_broadcast(token: str) -> dict | None:
    """The stream that's live now, else the soonest one waiting, else None."""
    part = "id,snippet,status"
    live = _yt(token, "GET", f"liveBroadcasts?part={part}&broadcastStatus=active&broadcastType=all&maxResults=5")
    items = live.get("items") or []
    if items:
        return items[0]
    waiting = _yt(token, "GET", f"liveBroadcasts?part={part}&broadcastStatus=upcoming&broadcastType=all"
                                "&maxResults=25").get("items") or []
    waiting.sort(key=lambda b: (b.get("snippet") or {}).get("scheduledStartTime") or "9999")
    return waiting[0] if waiting else None


def _youtube_load() -> Details:
    def call(token):
        found = _youtube_broadcast(token)
        if found is None:
            return Details(privacy="public")
        snippet, status = found.get("snippet") or {}, found.get("status") or {}
        try:  # the category sits on the stream's video
            video = (_yt(token, "GET", f"videos?part=snippet&id={found.get('id', '')}").get("items") or [{}])[0]
        except DetailsError:
            video = {}
        thumbs = snippet.get("thumbnails") or {}
        thumb = next((thumbs[k].get("url", "") for k in ("medium", "high", "default") if k in thumbs), "")
        return Details(title=snippet.get("title", ""), description=snippet.get("description", ""),
                       privacy=status.get("privacyStatus", "public"), broadcast_id=found.get("id", ""),
                       starts=snippet.get("scheduledStartTime", ""), thumbnail=thumb,
                       video_category=str((video.get("snippet") or {}).get("categoryId") or ""))

    return _with_token("youtube", call)


def _youtube_stream_id(token: str, stream_key: str | None) -> str:
    """The YouTube stream that SprigCast's key belongs to, else the first reusable one."""
    streams = _yt(token, "GET", "liveStreams?part=id,cdn,contentDetails&mine=true&maxResults=50").get("items") or []
    if stream_key:
        same = next((s for s in streams
                     if ((s.get("cdn") or {}).get("ingestionInfo") or {}).get("streamName") == stream_key), None)
        if same:
            return same["id"]
    usable = [s for s in streams if (s.get("contentDetails") or {}).get("isReusable", True)]
    if not usable:
        raise DetailsError("Your YouTube channel has no stream key yet. Add YouTube under Destinations and "
                           "sign in there first.")
    return usable[0]["id"]


def _soon() -> str:
    return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _youtube_save(details: Details, thumbnail: bytes | None, stream_key: str | None) -> None:
    """Save, then the thumbnail. Fills in broadcast_id (made when there was none) and note."""
    title = details.title.strip()[:TITLE_MAX["youtube"]]
    if not title:
        raise DetailsError("YouTube needs a title.")
    privacy = details.privacy if details.privacy in PRIVACY else "public"
    snippet = {"title": title, "description": details.description[:DESCRIPTION_MAX],
               "scheduledStartTime": details.starts or _soon()}

    def call(token):
        broadcast_id = details.broadcast_id
        if broadcast_id:
            _yt(token, "PUT", "liveBroadcasts?part=snippet,status",
                {"id": broadcast_id, "snippet": snippet, "status": {"privacyStatus": privacy}})
        else:
            stream_id = _youtube_stream_id(token, stream_key)
            made = _yt(token, "POST", "liveBroadcasts?part=snippet,status,contentDetails",
                       {"snippet": snippet, "status": {"privacyStatus": privacy},
                        "contentDetails": {"enableAutoStart": True, "enableAutoStop": True}})
            broadcast_id = details.broadcast_id = made["id"]
            details.starts = (made.get("snippet") or {}).get("scheduledStartTime") or snippet["scheduledStartTime"]
            _yt(token, "POST", f"liveBroadcasts/bind?id={broadcast_id}&part=id&streamId={stream_id}")
            log.info("stream details: youtube made a stream waiting on the SprigCast key")

    _with_token("youtube", call)
    missed = []
    if details.video_category in YOUTUBE_CATEGORIES:
        def category(token):
            video = (_yt(token, "GET", f"videos?part=snippet&id={details.broadcast_id}").get("items") or [{}])[0]
            old = video.get("snippet") or {}
            if str(old.get("categoryId") or "") == details.video_category:
                return  # already there: no call spent
            keep = {k: old[k] for k in ("tags", "defaultLanguage", "defaultAudioLanguage") if old.get(k)}
            _yt(token, "PUT", "videos?part=snippet", {"id": details.broadcast_id, "snippet": {
                **keep, "title": title, "description": snippet["description"], "categoryId": details.video_category}})

        try:
            _with_token("youtube", category)
        except DetailsError as exc:
            log.info("stream details: youtube category not saved: %s", exc)
            missed.append(("category", ""))
    if thumbnail:
        def upload(token):
            _call("POST", f"{YOUTUBE_UPLOAD}/thumbnails/set?videoId={details.broadcast_id}", token, data=thumbnail,
                  headers={"Content-Type": "image/jpeg"}, who="YouTube")

        try:
            _with_token("youtube", upload)
        except DetailsError as exc:  # the rest is saved: say what didn't go
            missed.append(("thumbnail", str(exc)))
    if missed:
        what = " or ".join(name for name, _ in missed)
        details.note = " ".join([f"Saved, but not the {what}.", *(why for _, why in missed if why)])


def thumbnail_bytes(path: str) -> bytes:
    """A picture as a JPEG YouTube takes: 16:9 at most 1280×720, under 2 MB. QImage works off the UI thread."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QImage

    image = QImage(path)
    if image.isNull():
        raise DetailsError("That picture couldn't be opened.")
    if image.width() > 1280 or image.height() > 720:
        image = image.scaled(1280, 720, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    for quality in (90, 80, 70, 55):
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.convertToFormat(QImage.Format.Format_RGB888).save(buffer, "JPG", quality)
        buffer.close()
        if data.size() <= THUMB_MAX_BYTES:
            return bytes(data.data())
    raise DetailsError("That picture is too big for a thumbnail (2 MB at most).")


# Kick --------------------------------------------------------------------------------------------------


def _kick_load() -> Details:
    def call(token):
        channel = (_call("GET", f"{KICK_API}/channels", token, who="Kick").get("data") or [{}])[0]
        category = channel.get("category") or {}
        return Details(title=channel.get("stream_title", "") or "", category=category.get("name", "") or "",
                       category_id=str(category.get("id") or ""))

    return _with_token("kick", call)


def _kick_save(details: Details) -> None:
    body: dict = {"stream_title": details.title.strip()[:TITLE_MAX["kick"]]}
    if details.category_id:
        body["category_id"] = int(details.category_id)

    def call(token):
        _call("PATCH", f"{KICK_API}/channels", token, body, who="Kick")

    _with_token("kick", call)


def _kick_search(text: str) -> list[tuple[str, str]]:
    query = urllib.parse.quote(text.strip())

    def call(token):
        try:
            found = _call("GET", f"{KICK_API}/categories?q={query}", token, who="Kick").get("data") or []
        except DetailsError:  # the first search was retired: the newer one
            found = _call("GET", f"{KICK_API_V2}/categories?name={query}&limit=12", token, who="Kick")
            found = found.get("data") or []
        if isinstance(found, dict):
            found = found.get("categories") or found.get("data") or []
        return [(str(c["id"]), c["name"]) for c in found if isinstance(c, dict) and c.get("id") and c.get("name")]

    return _with_token("kick", call)[:12]


# What the pop-up calls (on a worker) -------------------------------------------------------------------


def load(service: str) -> Details:
    log.info("stream details: reading %s", service)
    return {"twitch": _twitch_load, "youtube": _youtube_load, "kick": _kick_load}[service]()


def save(service: str, details: Details, thumbnail_path: str = "", stream_key_ref: str | None = None) -> Details:
    """Send the details to the platform. Returns them as saved (YouTube: with the stream it made)."""
    log.info("stream details: saving %s", service)
    if service == "twitch":
        _twitch_save(details)
    elif service == "kick":
        _kick_save(details)
    else:
        picture = thumbnail_bytes(thumbnail_path) if thumbnail_path else None
        key = get_key(stream_key_ref) if stream_key_ref and not details.broadcast_id else None
        details.note = ""
        _youtube_save(details, picture, key)
    log.info("stream details: %s saved", service)
    return details


def search_categories(service: str, text: str) -> list[tuple[str, str]]:
    """(id, name) pairs matching what was typed."""
    if len(text.strip()) < 2:
        return []
    return {"twitch": _twitch_search, "kick": _kick_search}[service](text)
