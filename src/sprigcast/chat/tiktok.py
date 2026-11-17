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

import asyncio
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
# "Is it live yet?" checks (each is a few requests to TikTok): every minute at first, then less often
# the longer you stay offline. Starting your own stream in SprigCast checks again straight away.
NOT_LIVE_STEPS = ((10, 60), (30, 120), (None, 300))  # (up to this many checks in a row, seconds apart)
QUIET_LOGGERS = ("httpx", "httpcore", "websockets")  # TikTokLive's web library logs every request
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


def not_live_wait(checks: int) -> int:
    """Seconds until the next "is it live yet?" check, after this many in a row."""
    for limit, seconds in NOT_LIVE_STEPS:
        if limit is None or checks <= limit:
            return seconds
    return NOT_LIVE_STEPS[-1][1]


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


class _LoopThread(threading.Thread):
    """One event loop for every direct TikTok connection, started on first use and kept.

    Each try used to start its own thread and loop (and the loop its own helper threads for name
    lookups), then throw them away: while waiting for a channel to go live that was a new set every
    check."""

    def __init__(self) -> None:
        super().__init__(name="tiktok-chat", daemon=True)
        # The plain select() loop, not Windows' default completion-port (Proactor) one: SprigCast died idle on
        # the PC (2026-11-16, access violation in native code) with this thread waiting in the Proactor's _poll.
        # Proactor hands buffers to Windows that must outlive every pending read, and its rough edges have
        # crashed Python before; a few chat sockets need none of what it adds.
        self.loop = asyncio.SelectorEventLoop()

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()


_loop_thread: _LoopThread | None = None
_loop_lock = threading.Lock()


def _shared_loop() -> _LoopThread:
    global _loop_thread
    with _loop_lock:
        if _loop_thread is None or not _loop_thread.is_alive():
            _loop_thread = _LoopThread()
            _loop_thread.start()
        return _loop_thread

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
        self._checks = 0  # "not live yet" checks in a row
        self._waiting_live = False  # the retry timer is only waiting for the stream to start
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
        self.channel, self._wanted, self._tries, self._checks = handle, True, 0, 0
        self.status.emit("connecting")
        self._open()

    def stop(self) -> None:
        self._wanted = False
        self._waiting_live = False
        self._retry.stop()
        self._close_tikfinity()
        self._stop_direct()

    def check_now(self) -> None:
        """You just went live: if this is only waiting for the stream to start, look right away."""
        if self._wanted and self._waiting_live:
            self._checks = 0
            self._retry.start(0)

    def _open(self) -> None:
        if not self._wanted:
            return
        self._waiting_live = False  # (no "connecting" here: a quiet recheck keeps its "waiting" state)
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
        import importlib.util

        # Only ask whether it's installed: importing TikTokLive builds hundreds of message classes
        # (~1.7 s), which _run_direct does on the helper thread so the window never freezes.
        if importlib.util.find_spec("TikTokLive") is None:
            self._failed("start TikFinity, or install the TikTok add-on")
            return
        self.via = "direct"
        for name in QUIET_LOGGERS:
            logging.getLogger(name).setLevel(max(logging.WARNING, logging.getLogger(name).level))
        key = str(QSettings().value(KEY_SETTING, "") or "")
        self._run += 1
        self._thread = _shared_loop()
        asyncio.run_coroutine_threadsafe(self._run_direct(self._run, self.channel, key), self._thread.loop)

    async def _run_direct(self, run: int, handle: str, key: str) -> None:
        """Runs on the shared TikTok loop until the stream ends, fails or stop() asks it to."""
        from TikTokLive import TikTokLiveClient
        from TikTokLive.client.errors import UserNotFoundError, UserOfflineError
        from TikTokLive.client.web.web_settings import WebDefaults
        from TikTokLive.events import CommentEvent, ConnectEvent, FollowEvent, GiftEvent, ShareEvent

        if key:
            WebDefaults.tiktok_sign_api_key = key
        loop = asyncio.get_running_loop()
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
            await client.connect()
        except UserOfflineError:
            why = "not live yet"
        except UserNotFoundError:
            why, offline = "no TikTok account by that name", False
        except Exception as exc:  # signing limits, TikTok changes, network
            why, offline = (str(exc).splitlines()[0][:120] or type(exc).__name__), False
            log.info("tiktok chat %s: %s", handle, exc)
        finally:
            try:
                await client.disconnect(close_client=True)
            except Exception:
                pass
            if self._client is client:
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
            if why == "stream ended":
                self._checks = 0  # it was live a moment ago: the next one may start soon
            self._checks += 1
            self._waiting_live = True
            self.status.emit(f"waiting: {why}")
            self._retry.start(not_live_wait(self._checks) * 1000)
        else:
            self._checks = 0
            self._failed(why)

    def _failed(self, why: str) -> None:
        if not self._wanted:
            return
        wait = BACKOFF[min(self._tries, len(BACKOFF) - 1)]
        self._tries += 1
        log.info("tiktok chat %s: %s; retrying in %d s", self.channel, why, wait)
        self.status.emit(f"offline: {why}")
        self._retry.start(wait * 1000)
