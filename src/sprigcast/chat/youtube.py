"""Read a YouTube live stream's chat from just a channel @handle: no Google sign-in, no API key.

It does what the YouTube page itself does, so there's no daily quota and nothing to set up:

1. Open youtube.com/@handle/live. When the channel is live (or has a stream waiting), YouTube
   points that page at the stream's video. Otherwise we look again every minute.
2. Open the stream's pop-out chat page, which holds the first "continuation" token.
3. Ask youtubei/v1/live_chat/get_live_chat with that token, again and again: each answer brings
   the new messages and the next token, plus how long to wait before asking.

This is how chat tools have read YouTube for years, but it isn't an official API, so YouTube can
change it. Everything runs on Qt's event loop (QNetworkAccessManager), no thread.
"""

from __future__ import annotations

import json
import logging
import re

from PySide6.QtCore import QByteArray, QObject, QTimer, QUrl, Signal

from sprigcast.chat import ChatMessage

log = logging.getLogger(__name__)

BASE = "https://www.youtube.com"
AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/140.0.0.0 Safari/537.36")
FALLBACK_VERSION = "2.20251101.00.00"
NOT_LIVE_RETRY = 60  # seconds between "is it live yet?" checks (each reads a ~1 MB page)
NOT_LIVE_SLOW = 180  # after half an hour of not live, check less often
POLL_MIN, POLL_MAX = 1.5, 10.0  # seconds between chat polls, whatever YouTube suggests
BACKOFF = (5, 10, 20, 40, 60)

_VIDEO_ID = r"[A-Za-z0-9_-]{11}"


def handle_from_text(text: str) -> str | None:
    """'@name', 'youtube.com/@name', a /channel/UC… link, or a video link -> what to look up.

    Returns '@name', 'channel/UC…' or 'v:<video id>' (a specific stream), or None."""
    text = text.strip()
    if not text:
        return None
    m = re.search(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|live/|shorts/)|youtu\.be/)(" + _VIDEO_ID + ")", text)
    if m:
        return "v:" + m.group(1)
    m = re.search(r"youtube\.com/channel/(UC[A-Za-z0-9_-]{22})", text)
    if m:
        return "channel/" + m.group(1)
    m = re.search(r"youtube\.com/(@[\w.\-·]{3,30})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", text):
        return "channel/" + text
    if re.fullmatch(r"@?[\w.\-·]{3,30}", text):
        return "@" + text.lstrip("@")
    return None


def live_page_url(handle: str) -> str:
    return f"{BASE}/{handle}/live"


def video_from_live_page(page: str) -> str | None:
    """The live (or waiting) stream's video id from a channel's /live page, or None if not live."""
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=(' + _VIDEO_ID + ')"', page)
    if m:
        return m.group(1)
    m = re.search(r'"liveStreamabilityRenderer":\{"videoId":"(' + _VIDEO_ID + ')"', page)
    return m.group(1) if m else None


def _initial_data(page: str) -> dict:
    m = re.search(r"(?:window\[\"ytInitialData\"\]|var ytInitialData|ytInitialData)\s*=\s*(\{.+?\})\s*;\s*</script>",
                  page, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {}


def chat_setup(page: str) -> tuple[str, str, str | None, list]:
    """From the pop-out chat page: (api key, client version, first continuation, actions already there).

    Picks the "Live chat" view (every message) over "Top chat" (YouTube's filtered one)."""
    key = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', page)
    version = re.search(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"', page)
    renderer = _initial_data(page).get("contents", {}).get("liveChatRenderer", {})
    continuation = None
    try:
        items = (renderer["header"]["liveChatHeaderRenderer"]["viewSelector"]["sortFilterSubMenuRenderer"]
                 ["subMenuItems"])
        continuation = items[-1]["continuation"]["reloadContinuationData"]["continuation"]
    except (KeyError, IndexError, TypeError):
        pass
    if continuation is None:
        continuation, _ = _next(renderer.get("continuations", []))
    if continuation is None:
        m = re.search(r'"continuation"\s*:\s*"([^"]{20,})"', page)
        continuation = m.group(1) if m else None
    return (key.group(1) if key else "", version.group(1) if version else FALLBACK_VERSION, continuation,
            renderer.get("actions", []))


def _next(continuations: list) -> tuple[str | None, float]:
    for entry in continuations or []:
        for kind in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData",
                     "liveChatReplayContinuationData"):
            data = entry.get(kind)
            if data and data.get("continuation"):
                return data["continuation"], float(data.get("timeoutMs", 5000)) / 1000
    return None, 5.0


def parse_chat_response(data: dict) -> tuple[list, str | None, float]:
    """get_live_chat's answer -> (actions, next continuation or None when chat ended, seconds to wait)."""
    chat = data.get("continuationContents", {}).get("liveChatContinuation", {})
    continuation, wait = _next(chat.get("continuations", []))
    return chat.get("actions", []), continuation, min(max(wait, POLL_MIN), POLL_MAX)


def _runs(runs: list, emotes: list, images: dict) -> str:
    text = ""
    for run in runs or []:
        if "text" in run:
            text += run["text"]
            continue
        emoji = run.get("emoji")
        if not emoji:
            continue
        if emoji.get("isCustomEmoji"):
            label = (emoji.get("shortcuts") or [":" + emoji.get("emojiId", "emoji")[-8:] + ":"])[0]
            thumbs = emoji.get("image", {}).get("thumbnails") or []
            key = "yt-" + re.sub(r"[^\w-]", "", emoji.get("emojiId", ""))[-48:]
            if thumbs and key != "yt-":
                images[key] = thumbs[0].get("url", "")
                emotes.append((len(text), len(text) + len(label) - 1, key))
            text += label
        else:
            text += emoji.get("emojiId", "")  # a normal emoji is just the character
    return text


def _simple(value: dict | None) -> str:
    if not value:
        return ""
    if "simpleText" in value:
        return value["simpleText"]
    return "".join(r.get("text", "") for r in value.get("runs", []))


_BADGES = {"OWNER": "broadcaster", "MODERATOR": "moderator", "VERIFIED": "verified"}


def message_from_item(item: dict) -> ChatMessage | None:
    """One chat item (text, Super Chat, membership...) -> ChatMessage, or None for things we skip."""
    for kind, body in item.items():
        if kind not in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer",
                        "liveChatMembershipItemRenderer", "liveChatPaidStickerRenderer",
                        "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer"):
            continue
        emotes: list = []
        images: dict = {}
        text = _runs(body.get("message", {}).get("runs", []), emotes, images)
        user = _simple(body.get("authorName")).lstrip("@") or "someone"
        badges = []
        for badge in body.get("authorBadges", []):
            renderer = badge.get("liveChatAuthorBadgeRenderer", {})
            icon = renderer.get("icon", {}).get("iconType")
            if icon in _BADGES:
                badges.append(_BADGES[icon])
            elif renderer.get("customThumbnail"):
                badges.append("member")
        message = ChatMessage(platform="youtube", user=user, text=text, id=body.get("id", ""),
                              user_id=body.get("authorExternalChannelId", ""), badges=tuple(badges),
                              emotes=emotes, images=images)
        if kind in ("liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer"):
            amount = _simple(body.get("purchaseAmountText"))
            message.notice, message.emotes = True, []
            message.text = f"{user} sent {amount}" + (f": {text}" if text else "")
        elif kind == "liveChatMembershipItemRenderer":
            message.notice, message.emotes = True, []
            header = _simple(body.get("headerSubtext")) or "became a member"
            message.text = f"{user}: {header}" + (f"  {text}" if text else "")
        elif kind == "liveChatSponsorshipsGiftPurchaseAnnouncementRenderer":
            header = body.get("header", {}).get("liveChatSponsorshipsHeaderRenderer", {})
            message.user = _simple(header.get("authorName")).lstrip("@") or user
            message.notice, message.emotes = True, []
            message.text = f"{message.user} {_simple(header.get('primaryText'))}".strip()
        elif not text:
            return None
        return message
    return None


class YouTubeChat(QObject):
    """Follows one channel: waits for it to go live, reads chat while it is, then waits again."""

    message = Signal(object)
    deleted = Signal(str)
    cleared = Signal(str)
    status = Signal(str)
    platform = "youtube"

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.channel = ""
        self.video = ""
        self._key, self._version, self._continuation = "", FALLBACK_VERSION, None
        self._net = None
        self._reply = None
        self._wanted = False
        self._tries = 0
        self._seen: list[str] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._step)
        self._stage = "find"
        self._checks = 0  # "not live yet" checks in a row

    def start(self, handle: str) -> None:
        if self._wanted and handle == self.channel:
            return
        self.stop()
        self.channel, self._wanted, self._tries, self._stage = handle, True, 0, "find"
        self.status.emit("connecting")
        self._step()

    def stop(self) -> None:
        self._wanted = False
        self._timer.stop()
        if self._reply is not None:
            reply, self._reply = self._reply, None
            reply.finished.disconnect()
            reply.abort()
            reply.deleteLater()

    # The three stages ---------------------------------------------------------------------

    def _step(self) -> None:
        if not self._wanted:
            return
        if self._stage == "find":
            if self.channel.startswith("v:"):
                self.video = self.channel[2:]
                self._stage = "open"
                self._step()
                return
            self._get(live_page_url(self.channel), self._found)
        elif self._stage == "open":
            self._get(f"{BASE}/live_chat?is_popout=1&v={self.video}", self._opened)
        else:
            body = {"context": {"client": {"clientName": "WEB", "clientVersion": self._version, "hl": "en"}},
                    "continuation": self._continuation}
            url = f"{BASE}/youtubei/v1/live_chat/get_live_chat?prettyPrint=false"
            if self._key:
                url += f"&key={self._key}"
            self._post(url, body, self._polled)

    def _found(self, page: str) -> None:
        video = video_from_live_page(page)
        if not video:
            self._checks += 1
            self._wait("not live yet", NOT_LIVE_RETRY if self._checks < 30 else NOT_LIVE_SLOW)
            return
        self._checks = 0
        self.video, self._stage = video, "open"
        self._step()

    def _opened(self, page: str) -> None:
        self._key, self._version, self._continuation, actions = chat_setup(page)
        if not self._continuation:  # a video without live chat (ended, or chat turned off)
            self._stage = "find"
            self._wait("no live chat on this stream", NOT_LIVE_RETRY)
            return
        self._stage, self._tries = "poll", 0
        self.status.emit("live")
        self._handle(actions)
        self._timer.start(int(POLL_MIN * 1000))

    def _polled(self, page: str) -> None:
        try:
            data = json.loads(page)
        except ValueError:
            self._failed("YouTube sent something unexpected")
            return
        actions, continuation, wait = parse_chat_response(data)
        self._handle(actions)
        if continuation is None:  # the stream ended: look for the next one
            self._stage = "find"
            self._wait("stream ended", NOT_LIVE_RETRY)
            return
        self._continuation, self._tries = continuation, 0
        self.status.emit("live")
        self._timer.start(int(wait * 1000))

    def _handle(self, actions: list) -> None:
        for action in actions:
            if "addChatItemAction" in action:
                message = message_from_item(action["addChatItemAction"].get("item", {}))
                if message is not None and message.id not in self._seen:
                    self._seen = (self._seen + [message.id])[-500:]
                    self.message.emit(message)
            elif "markChatItemAsDeletedAction" in action:
                self.deleted.emit(action["markChatItemAsDeletedAction"].get("targetItemId", ""))
            elif "markChatItemsByAuthorAsDeletedAction" in action:
                channel = action["markChatItemsByAuthorAsDeletedAction"].get("externalChannelId", "")
                if channel:
                    self.cleared.emit(channel)

    def _wait(self, why: str, seconds: float) -> None:
        self.status.emit(f"waiting: {why}")
        self._timer.start(int(seconds * 1000))

    def _failed(self, why: str) -> None:
        wait = BACKOFF[min(self._tries, len(BACKOFF) - 1)]
        self._tries += 1
        log.info("youtube chat %s: %s; retrying in %d s", self.channel, why, wait)
        if self._tries > 3 and self._stage == "poll":
            self._stage = "open"  # the token may have gone stale: start chat over
        self.status.emit(f"offline: {why}")
        self._timer.start(wait * 1000)

    # Network --------------------------------------------------------------------------------

    def _request(self, url: str):
        from PySide6.QtNetwork import QNetworkRequest

        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"User-Agent", AGENT.encode())
        request.setRawHeader(b"Accept-Language", b"en-US,en;q=0.9")
        request.setRawHeader(b"Cookie", b"CONSENT=YES+1; SOCS=CAI")  # skip the EU cookie page
        request.setTransferTimeout(15000)
        return request

    def _manager(self):
        if self._net is None:
            from PySide6.QtNetwork import QNetworkAccessManager

            self._net = QNetworkAccessManager(self)
        return self._net

    def _get(self, url: str, done) -> None:
        self._watch(self._manager().get(self._request(url)), done)

    def _post(self, url: str, body: dict, done) -> None:
        from PySide6.QtNetwork import QNetworkRequest

        request = self._request(url)
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        self._watch(self._manager().post(request, QByteArray(json.dumps(body).encode())), done)

    def _watch(self, reply, done) -> None:
        self._reply = reply

        def finished():
            if self._reply is not reply:
                return
            self._reply = None
            reply.deleteLater()
            from PySide6.QtNetwork import QNetworkReply

            if reply.error() != QNetworkReply.NetworkError.NoError:
                self._failed(reply.errorString())
                return
            done(bytes(reply.readAll().data()).decode("utf-8", "replace"))

        reply.finished.connect(finished)
