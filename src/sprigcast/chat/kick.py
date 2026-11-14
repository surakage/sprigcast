"""Read a Kick channel's chat from just its name: no sign-in, no key.

1. Ask kick.com for the channel's chatroom number (kick.com/api/v2/channels/<name>).
2. Listen on Kick's public chat feed (a Pusher WebSocket) for that chatroom.

Kick's own website reads chat the same way. It isn't Kick's official developer API (that one only
sends chat to a web server by webhook, which a desktop app can't receive), so Kick could change it.
Runs on Qt's event loop, no thread.
"""

from __future__ import annotations

import json
import logging
import re

from PySide6.QtCore import QObject, QSettings, QTimer, QUrl, Signal

from sprigcast.chat import ChatMessage

log = logging.getLogger(__name__)

CHANNEL_API = "https://kick.com/api/v2/channels/{slug}"
PUSHER_KEY = "32cbd69e4b950bf97679"  # Kick's public chat key; QSettings "kick/pusher_key" overrides it
PUSHER_URL = "wss://ws-us2.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"
AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/140.0.0.0 Safari/537.36")
EMOTE_URL = "https://files.kick.com/emotes/{id}/fullsize"
BACKOFF = (3, 6, 12, 30, 60)
_EMOTE = re.compile(r"\[emote:(\d+):([^\]]*)\]")
_SLUG = re.compile(r"^[A-Za-z0-9_-]{2,30}$")


def slug_from_text(text: str) -> str | None:
    """'kick.com/name', 'name' or '@name' -> 'name'."""
    text = text.strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?kick\.com/(?:popout/)?([A-Za-z0-9_-]{2,30})", text, re.I)
    if m:
        return m.group(1).lower()
    text = text.lstrip("@")
    return text.lower() if _SLUG.match(text) else None


def chatroom_from_channel(data: dict) -> int | None:
    try:
        return int(data["chatroom"]["id"])
    except (KeyError, TypeError, ValueError):
        return None


def _content(text: str) -> tuple[str, list, dict]:
    """'hi [emote:37226:KEKW]' -> ('hi KEKW', [(3, 6, 'kick-37226')], {'kick-37226': url})."""
    out, emotes, images, at = "", [], {}, 0
    for m in _EMOTE.finditer(text):
        out += text[at:m.start()]
        name = m.group(2) or "emote"
        key = f"kick-{m.group(1)}"
        emotes.append((len(out), len(out) + len(name) - 1, key))
        images[key] = EMOTE_URL.format(id=m.group(1))
        out += name
        at = m.end()
    return out + text[at:], emotes, images


def message_from_event(event: str, data: dict) -> ChatMessage | None:
    """One Pusher event's data -> ChatMessage (chat lines, subs, gifts, hosts)."""
    if event == "App\\Events\\ChatMessageEvent":
        sender = data.get("sender") or {}
        identity = sender.get("identity") or {}
        text, emotes, images = _content(str(data.get("content", "")))
        badges = tuple(str(b.get("type", "")) for b in identity.get("badges") or [] if b.get("type"))
        return ChatMessage(platform="kick", user=sender.get("username") or "someone", text=text,
                           color=identity.get("color", ""), id=str(data.get("id", "")),
                           user_id=str(sender.get("id", "")), badges=badges, emotes=emotes, images=images)
    if event == "App\\Events\\SubscriptionEvent":
        months = data.get("months")
        text = f"{data.get('username', 'someone')} subscribed" + (f" for {months} months" if months else "")
    elif event == "App\\Events\\GiftedSubscriptionsEvent":
        gifted = data.get("gifted_usernames") or []
        text = f"{data.get('gifter_username', 'someone')} gifted {len(gifted)} sub{'s' if len(gifted) != 1 else ''}"
    elif event == "App\\Events\\StreamHostEvent":
        text = f"{data.get('host_username', 'someone')} hosted with {data.get('number_viewers', 0)} viewers"
    else:
        return None
    return ChatMessage(platform="kick", user="", text=text, notice=True)


class KickChat(QObject):
    message = Signal(object)
    deleted = Signal(str)
    cleared = Signal(str)
    status = Signal(str)
    platform = "kick"

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.channel = ""
        self.chatroom: int | None = None
        self.socket = None
        self._net = None
        self._reply = None
        self._wanted = False
        self._tries = 0
        self._seen: list[str] = []
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._open)
        self._ping = QTimer(self)  # Pusher drops a quiet link after ~2 minutes without a ping
        self._ping.setInterval(60_000)
        self._ping.timeout.connect(lambda: self._send({"event": "pusher:ping", "data": {}}))

    def start(self, slug: str) -> None:
        if self._wanted and slug == self.channel:
            return
        self.stop()
        self.channel, self.chatroom, self._wanted, self._tries = slug, None, True, 0
        self._open()

    def stop(self) -> None:
        self._wanted = False
        self._retry.stop()
        self._ping.stop()
        if self._reply is not None:
            reply, self._reply = self._reply, None
            reply.finished.disconnect()
            reply.abort()
            reply.deleteLater()
        self._close_socket()

    def _close_socket(self) -> None:
        if self.socket is not None:
            socket, self.socket = self.socket, None
            try:
                socket.disconnected.disconnect()
            except (RuntimeError, TypeError):
                pass
            socket.abort()
            socket.deleteLater()

    def _open(self) -> None:
        if not self._wanted:
            return
        self.status.emit("connecting")
        if self.chatroom is None:
            self._lookup()
        else:
            self._connect()

    # 1. Which chatroom -----------------------------------------------------------------------

    def _lookup(self) -> None:
        from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

        if self._net is None:
            self._net = QNetworkAccessManager(self)
        request = QNetworkRequest(QUrl(CHANNEL_API.format(slug=self.channel)))
        request.setRawHeader(b"User-Agent", AGENT.encode())
        request.setRawHeader(b"Accept", b"application/json")
        request.setTransferTimeout(15000)
        reply = self._reply = self._net.get(request)

        def finished():
            if self._reply is not reply:
                return
            self._reply = None
            reply.deleteLater()
            code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            if code == 404:
                self._failed("no Kick channel by that name")
                return
            if reply.error() != QNetworkReply.NetworkError.NoError:
                self._failed("Kick blocked the lookup" if code == 403 else reply.errorString())
                return
            try:
                chatroom = chatroom_from_channel(json.loads(bytes(reply.readAll().data())))
            except ValueError:
                chatroom = None
            if chatroom is None:
                self._failed("Kick sent something unexpected")
                return
            self.chatroom = chatroom
            self._connect()

        reply.finished.connect(finished)

    # 2. The chat feed -------------------------------------------------------------------------

    def _connect(self) -> None:
        from PySide6.QtWebSockets import QWebSocket

        key = str(QSettings().value("kick/pusher_key", "") or "") or PUSHER_KEY
        self.socket = QWebSocket()
        self.socket.disconnected.connect(lambda: self._failed("disconnected"))
        self.socket.textMessageReceived.connect(self.handle_text)
        self.socket.open(QUrl(PUSHER_URL.format(key=key)))

    def _send(self, payload: dict) -> None:
        if self.socket is not None:
            self.socket.sendTextMessage(json.dumps(payload))

    def handle_text(self, payload: str) -> None:
        try:
            frame = json.loads(payload)
            data = frame.get("data")
            data = json.loads(data) if isinstance(data, str) and data else (data or {})
        except ValueError:
            return
        event = frame.get("event", "")
        if event == "pusher:connection_established":
            for channel in (f"chatrooms.{self.chatroom}.v2", f"chatroom_{self.chatroom}"):
                self._send({"event": "pusher:subscribe", "data": {"auth": "", "channel": channel}})
            self._ping.start()
        elif event == "pusher_internal:subscription_succeeded":
            self._tries = 0
            self.status.emit("live")
        elif event == "pusher:ping":
            self._send({"event": "pusher:pong", "data": {}})
        elif event == "pusher:error":
            log.info("kick chat %s: pusher error %s", self.channel, data)
        elif event == "App\\Events\\MessageDeletedEvent":
            message_id = (data.get("message") or {}).get("id")
            if message_id:
                self.deleted.emit(str(message_id))
        elif event == "App\\Events\\UserBannedEvent":
            user_id = (data.get("user") or {}).get("id")
            if user_id:
                self.cleared.emit(str(user_id))
        elif event == "App\\Events\\ChatroomClearEvent":
            self.cleared.emit("")
        else:
            message = message_from_event(event, data if isinstance(data, dict) else {})
            if message is None:
                return
            if message.id:  # the same line can arrive on both subscriptions
                if message.id in self._seen:
                    return
                self._seen = (self._seen + [message.id])[-500:]
            self.message.emit(message)

    def _failed(self, why: str) -> None:
        self._close_socket()
        self._ping.stop()
        if not self._wanted:
            return
        wait = BACKOFF[min(self._tries, len(BACKOFF) - 1)]
        self._tries += 1
        log.info("kick chat %s: %s; retrying in %d s", self.channel, why, wait)
        self.status.emit(f"offline: {why}")
        self._retry.start(wait * 1000)
