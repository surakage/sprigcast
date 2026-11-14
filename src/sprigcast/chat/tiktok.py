"""Read TikTok LIVE chat. TikTok has no public chat API (Streamlabs and Meld get it through private
partner deals), so this is unofficial and best effort. Two ways in, tried in this order:

1. **TikFinity**, if it's running on this PC: it shares the chat it reads on ws://localhost:21213.
   Nothing to set up, and it's the steadiest way since TikFinity keeps itself working.
2. **Straight from TikTok** by @handle, with the open-source TikTokLive library. Its requests are
   signed by a free community service (Euler Stream); a personal key (QSettings "tiktok/sign_key")
   lifts its limits. A TikTok change can break this for a while until the library catches up.

TikFinity runs on Qt's event loop. The direct way runs TikTokLive's asyncio client on one helper
thread and hands each message to the UI thread through a Qt signal.
"""

from __future__ import annotations

import json
import logging
import re
import threading

from PySide6.QtCore import QObject, QSettings, QTimer, QUrl, Signal

from sprigcast.chat import ChatMessage

log = logging.getLogger(__name__)

TIKFINITY_URL = "ws://localhost:21213/"
TIKFINITY_SETTING = "tiktok/use_tikfinity"  # on by default
KEY_SETTING = "tiktok/sign_key"  # optional Euler Stream key, for steadier direct reading
NOT_LIVE_RETRY = 60
BACKOFF = (5, 10, 20, 40, 60)
_HANDLE = re.compile(r"^[A-Za-z0-9_.]{2,24}$")


def handle_from_text(text: str) -> str | None:
    """'@name', 'name' or a tiktok.com/@name link -> 'name'."""
    text = text.strip()
    m = re.search(r"tiktok\.com/@([A-Za-z0-9_.]{2,24})", text, re.I)
    if m:
        return m.group(1).lower()
    text = text.lstrip("@")
    return text.lower() if _HANDLE.match(text) else None


# TikFinity's messages ---------------------------------------------------------------------------

def message_from_tikfinity(frame: dict) -> ChatMessage | None:
    """{"event": "chat" | "gift" | "follow" | "share" | "subscribe" | ..., "data": {...}} -> ChatMessage."""
    event, data = frame.get("event"), frame.get("data") or {}
    if not isinstance(data, dict):
        return None
    user = str(data.get("nickname") or data.get("uniqueId") or "someone")
    if event == "chat":
        badges = []
        if data.get("tikfinityUsername") and data.get("tikfinityUsername") == data.get("uniqueId"):
            badges.append("broadcaster")
        if data.get("isModerator"):
            badges.append("moderator")
        if data.get("isSubscriber"):
            badges.append("subscriber")
        text = str(data.get("comment", ""))
        emotes, images = [], {}
        for emote in data.get("emotes") or []:  # stickers placed in the comment
            try:
                at = int(emote.get("placeInComment"))
            except (TypeError, ValueError):
                continue
            url = str(emote.get("emoteImageUrl") or "")
            key = "tt-" + re.sub(r"[^\w-]", "", str(emote.get("emoteId", "")))[-40:]
            if url.startswith("https://") and key != "tt-" and 0 <= at <= len(text):
                text = text[:at] + "▢" + text[at:]
                emotes.append((at, at, key))
                images[key] = url
        return ChatMessage(platform="tiktok", user=user, text=text, id=str(data.get("msgId", "")),
                           user_id=str(data.get("userId", "")), badges=tuple(badges),
                           emotes=sorted(emotes), images=images)
    if event == "gift":
        if data.get("giftType") == 1 and not data.get("repeatEnd"):
            return None  # a streak still going: wait for its last count
        count = int(data.get("repeatCount") or 1)
        text = f"{user} sent {data.get('giftName', 'a gift')}" + (f" x{count}" if count > 1 else "")
    elif event == "follow":
        text = f"{user} followed"
    elif event == "subscribe":
        text = f"{user} subscribed"
    elif event == "share":
        text = f"{user} shared the LIVE"
    else:
        return None  # likes, joins and viewer counts would flood chat
    return ChatMessage(platform="tiktok", user="", text=text, id=str(data.get("msgId", "")), notice=True)


# The reader -------------------------------------------------------------------------------------

class _Bridge(QObject):
    """Carries messages from the helper thread to the UI thread."""

    message = Signal(object)
    status = Signal(str)
    ended = Signal(int, str, bool)  # run number, why, and whether it's just not live (check again later)


class TikTokChat(QObject):
    message = Signal(object)
    deleted = Signal(str)
    cleared = Signal(str)
    status = Signal(str)
    platform = "tiktok"

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.channel = ""
        self.via = ""  # "tikfinity" or "direct", for the panel's tooltip
        self.socket = None
        self._wanted = False
        self._tries = 0
        self._thread: threading.Thread | None = None
        self._loop = None
        self._client = None
        self._run = 0  # each direct run's number, so an old run ending can't disturb a new one
        self._tikfinity_was_up = False
        self._bridge = _Bridge(self)
        self._bridge.message.connect(self.message)
        self._bridge.status.connect(self.status)
        self._bridge.ended.connect(self._direct_ended)
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._open)

    def start(self, handle: str) -> None:
        if self._wanted and handle == self.channel:
            return
        self.stop()
        self.channel, self._wanted, self._tries = handle, True, 0
        self._open()

    def stop(self) -> None:
        self._wanted = False
        self._retry.stop()
        self._close_tikfinity()
        self._stop_direct()

    def _open(self) -> None:
        if not self._wanted:
            return
        self.status.emit("connecting")
        if QSettings().value(TIKFINITY_SETTING, True, type=bool):
            self._open_tikfinity()
        else:
            self._open_direct()

    # 1. TikFinity --------------------------------------------------------------------------

    def _open_tikfinity(self) -> None:
        from PySide6.QtWebSockets import QWebSocket

        self.via = "tikfinity"
        self.socket = QWebSocket()
        self.socket.connected.connect(self._tikfinity_up)
        self.socket.disconnected.connect(self._tikfinity_down)
        self.socket.textMessageReceived.connect(self.handle_tikfinity)
        self.socket.open(QUrl(TIKFINITY_URL))

    def _tikfinity_up(self) -> None:
        self._tries, self._tikfinity_was_up = 0, True
        self.status.emit("live")

    def _tikfinity_down(self) -> None:
        was_up, self._tikfinity_was_up = self._tikfinity_was_up, False
        self._close_tikfinity()
        if not self._wanted:
            return
        if was_up:
            self._failed("TikFinity closed")  # look for it again, then go direct if it's gone
        else:
            self._open_direct()  # TikFinity isn't running: go to TikTok directly

    def _close_tikfinity(self) -> None:
        if self.socket is not None:
            socket, self.socket = self.socket, None
            for signal in (socket.connected, socket.disconnected, socket.textMessageReceived):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            socket.abort()
            socket.deleteLater()

    def handle_tikfinity(self, payload: str) -> None:
        try:
            frame = json.loads(payload)
        except ValueError:
            return
        if isinstance(frame, dict):
            message = message_from_tikfinity(frame)
            if message is not None:
                self.message.emit(message)

    # 2. Straight from TikTok -----------------------------------------------------------------

    def _open_direct(self) -> None:
        try:
            import TikTokLive  # noqa: F401
        except ImportError:
            self._failed("start TikFinity, or install the TikTok add-on")
            return
        self.via = "direct"
        key = str(QSettings().value(KEY_SETTING, "") or "")
        self._run += 1
        self._thread = threading.Thread(target=self._run_direct, args=(self._run, self.channel, key),
                                        name="tiktok-chat", daemon=True)
        self._thread.start()

    def _run_direct(self, run: int, handle: str, key: str) -> None:
        """Runs on the helper thread until the stream ends, fails or stop() asks it to."""
        import asyncio

        from TikTokLive import TikTokLiveClient
        from TikTokLive.client.errors import UserNotFoundError, UserOfflineError
        from TikTokLive.client.web.web_settings import WebDefaults
        from TikTokLive.events import CommentEvent, ConnectEvent, FollowEvent, GiftEvent, ShareEvent

        if key:
            WebDefaults.tiktok_sign_api_key = key
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        client = TikTokLiveClient(unique_id=f"@{handle}")
        self._loop, self._client = loop, client
        bridge = self._bridge

        def name_of(user) -> str:
            return str(getattr(user, "nickname", "") or getattr(user, "unique_id", "") or "someone")

        async def on_connect(_event) -> None:
            bridge.status.emit("live")

        async def on_comment(event) -> None:
            user = getattr(event, "user", None)
            common = getattr(event, "common", None)
            bridge.message.emit(ChatMessage(
                platform="tiktok", user=name_of(user), text=str(getattr(event, "content", "") or ""),
                id=str(getattr(common, "msg_id", "") or ""), user_id=str(getattr(user, "id", "") or "")))

        async def on_gift(event) -> None:
            if getattr(event, "streaking", False):
                return
            gift = getattr(getattr(event, "gift", None), "name", "") or "a gift"
            count = int(getattr(event, "repeat_count", 1) or 1)
            text = f"{name_of(getattr(event, 'user', None))} sent {gift}" + (f" x{count}" if count > 1 else "")
            bridge.message.emit(ChatMessage(platform="tiktok", user="", text=text, notice=True))

        async def on_follow(event) -> None:
            bridge.message.emit(ChatMessage(platform="tiktok", user="", notice=True,
                                            text=f"{name_of(getattr(event, 'user', None))} followed"))

        async def on_share(event) -> None:
            bridge.message.emit(ChatMessage(platform="tiktok", user="", notice=True,
                                            text=f"{name_of(getattr(event, 'user', None))} shared the LIVE"))

        for kind, handler in ((ConnectEvent, on_connect), (CommentEvent, on_comment), (GiftEvent, on_gift),
                              (FollowEvent, on_follow), (ShareEvent, on_share)):
            client.add_listener(kind, handler)
        why, offline = "stream ended", True
        try:
            loop.run_until_complete(client.connect())
        except UserOfflineError:
            why = "not live yet"
        except UserNotFoundError:
            why, offline = "no TikTok account by that name", False
        except Exception as exc:  # signing limits, TikTok changes, network
            why, offline = (str(exc).splitlines()[0][:120] or type(exc).__name__), False
            log.info("tiktok chat %s: %s", handle, exc)
        finally:
            try:
                loop.run_until_complete(client.disconnect(close_client=True))
            except Exception:
                pass
            loop.close()
            self._loop = self._client = None
        bridge.ended.emit(run, why, offline)

    def _stop_direct(self) -> None:
        loop, client = self._loop, self._client
        if loop is not None and client is not None and loop.is_running():
            import asyncio

            try:
                asyncio.run_coroutine_threadsafe(client.disconnect(), loop)
            except RuntimeError:
                pass
        self._thread = None
        self._run += 1  # whatever the old run says when it ends is no longer news

    def _direct_ended(self, run: int, why: str, offline: bool) -> None:
        if not self._wanted or run != self._run:
            return
        if offline:
            self.status.emit(f"waiting: {why}")
            self._retry.start(NOT_LIVE_RETRY * 1000)
        else:
            self._failed(why)

    def _failed(self, why: str) -> None:
        if not self._wanted:
            return
        wait = BACKOFF[min(self._tries, len(BACKOFF) - 1)]
        self._tries += 1
        log.info("tiktok chat %s: %s; retrying in %d s", self.channel, why, wait)
        self.status.emit(f"offline: {why}")
        self._retry.start(wait * 1000)
