"""Read a Twitch channel's chat, live, without signing in.

Twitch chat is IRC over a WebSocket (wss://irc-ws.chat.twitch.tv). Reading needs no account:
the "justinfan" nick is Twitch's built-in read-only guest. So the Chat panel can show any
channel's chat; Connect Twitch only tells it which channel is yours. Runs on Qt's event loop
(no thread), reconnects by itself, and costs next to nothing while chat is quiet.
"""

from __future__ import annotations

import logging
import random
import re

from PySide6.QtCore import QObject, QTimer, QUrl, Signal

from sprigcast.chat import ChatMessage

log = logging.getLogger(__name__)

URL = "wss://irc-ws.chat.twitch.tv:443"
BACKOFF = (2, 4, 8, 15, 30, 60)  # seconds between reconnect tries
CHANNEL_RE = re.compile(r"^[a-z0-9_]{2,25}$")
_TWITCH_URL = re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/(?:popout/|embed/)?([A-Za-z0-9_]{2,25})(?:[/?#].*)?$", re.I)


def channel_from_text(text: str) -> str | None:
    """'twitch.tv/name', a Twitch chat pop-out link, '#name' or 'name' -> 'name'; None if it isn't one."""
    text = text.strip()
    match = _TWITCH_URL.match(text)
    if match:
        name = match.group(1).lower()
        return None if name in ("popout", "embed", "directory", "videos", "settings") else name
    name = text.lstrip("#@").lower()
    return name if CHANNEL_RE.match(name) else None


# IRC lines -----------------------------------------------------------------------------

_TAG_ESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)?", lambda m: _TAG_ESCAPES.get(m.group(1) or "", m.group(1) or ""), value)


def parse_line(line: str) -> tuple[dict[str, str], str, str, list[str]]:
    """'@tags :prefix COMMAND a b :trailing' -> (tags, prefix, command, params)."""
    tags: dict[str, str] = {}
    if line.startswith("@"):
        raw, _, line = line[1:].partition(" ")
        for part in raw.split(";"):
            key, _, value = part.partition("=")
            tags[key] = _unescape(value)
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    head, sep, trailing = line.partition(" :")
    params = head.split()
    command = params.pop(0) if params else ""
    if sep:
        params.append(trailing)
    return tags, prefix, command, params


def _emotes(tag: str) -> list[tuple[int, int, str]]:
    """'25:0-4,12-16/1902:6-10' -> [(0, 4, '25'), (6, 10, '1902'), (12, 16, '25')]"""
    found = []
    for group in filter(None, tag.split("/")):
        emote_id, _, spans = group.partition(":")
        for span in filter(None, spans.split(",")):
            start, _, end = span.partition("-")
            try:
                found.append((int(start), int(end), emote_id))
            except ValueError:
                continue
    return sorted(found)


def message_from_line(line: str) -> ChatMessage | None:
    """A chat message or announced event from one IRC line, or None for everything else."""
    tags, prefix, command, params = parse_line(line)
    if command not in ("PRIVMSG", "USERNOTICE"):
        return None
    text = params[1] if len(params) > 1 else ""
    login = prefix.split("!", 1)[0]
    user = tags.get("display-name") or tags.get("login") or login
    badges = tuple(b.split("/", 1)[0] for b in tags.get("badges", "").split(",") if b)
    action = text.startswith("\x01ACTION ") and text.endswith("\x01")
    if action:
        text = text[len("\x01ACTION "):-1]
    message = ChatMessage(
        platform="twitch", user=user, text=text, color=tags.get("color", ""), id=tags.get("id", ""),
        user_id=tags.get("user-id", ""), badges=badges, emotes=[] if action else _emotes(tags.get("emotes", "")),
        action=action,
    )
    if command == "USERNOTICE":  # subs, gift subs, raids: Twitch writes the sentence for us
        message.notice = True
        message.emotes = []
        system = tags.get("system-msg", "")
        message.text = f"{system}  {text}".strip() if text else system
        if not message.text:
            return None
    return message


# The reader ---------------------------------------------------------------------------


class TwitchChat(QObject):
    """Joins one channel and emits each message. `start(channel)` / `stop()`."""

    message = Signal(object)  # ChatMessage
    deleted = Signal(str)  # a message id a moderator removed
    cleared = Signal(str)  # a user id whose messages were cleared (timeout/ban), "" = whole chat
    status = Signal(str)  # "connecting", "live", "offline: why"
    platform = "twitch"

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.channel = ""
        self.socket = None
        self._tries = 0
        self._wanted = False
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._open)
        self._quiet = QTimer(self)  # Twitch PINGs about every 5 minutes; silence for 7 means a dead link
        self._quiet.setSingleShot(True)
        self._quiet.setInterval(7 * 60 * 1000)
        self._quiet.timeout.connect(lambda: self._dropped("no word from Twitch"))

    def start(self, channel: str) -> None:
        channel = channel.lower().lstrip("#")
        if self._wanted and channel == self.channel:
            return
        self.stop()
        self.channel, self._wanted, self._tries = channel, True, 0
        self._open()

    def stop(self) -> None:
        self._wanted = False
        self._retry.stop()
        self._quiet.stop()
        if self.socket is not None:
            socket, self.socket = self.socket, None
            try:
                socket.disconnected.disconnect()
            except (RuntimeError, TypeError):
                pass
            socket.close()
            socket.deleteLater()

    def _open(self) -> None:
        from PySide6.QtWebSockets import QWebSocket

        if not self._wanted:
            return
        self.status.emit("connecting")
        self.socket = QWebSocket()
        self.socket.connected.connect(self._on_connected)
        self.socket.disconnected.connect(lambda: self._dropped("disconnected"))
        self.socket.textMessageReceived.connect(self._on_text)
        self.socket.open(QUrl(URL))

    def _on_connected(self) -> None:
        nick = f"justinfan{random.randint(10000, 99999)}"
        for line in ("CAP REQ :twitch.tv/tags twitch.tv/commands", "PASS SCHMOOPIIE", f"NICK {nick}",
                     f"JOIN #{self.channel}"):
            self.socket.sendTextMessage(line)
        self._quiet.start()

    def _dropped(self, why: str) -> None:
        if self.socket is not None:
            socket, self.socket = self.socket, None
            try:
                socket.disconnected.disconnect()
            except (RuntimeError, TypeError):
                pass
            socket.abort()
            socket.deleteLater()
        self._quiet.stop()
        if not self._wanted:
            return
        wait = BACKOFF[min(self._tries, len(BACKOFF) - 1)]
        self._tries += 1
        log.info("twitch chat %s (%s); retrying in %d s", self.channel, why, wait)
        self.status.emit(f"offline: {why}")
        self._retry.start(wait * 1000)

    def _on_text(self, payload: str) -> None:
        self._quiet.start()
        for line in payload.split("\r\n"):
            if line:
                self.handle_line(line)

    def handle_line(self, line: str) -> None:
        tags, _, command, params = parse_line(line)
        if command == "PING":
            if self.socket is not None:
                self.socket.sendTextMessage("PONG :" + (params[-1] if params else "tmi.twitch.tv"))
        elif command == "RECONNECT":  # Twitch is restarting a server: hop to another one now
            self._tries = 0
            self._dropped("Twitch asked to reconnect")
        elif command == "ROOMSTATE" or command == "JOIN":
            self._tries = 0
            self.status.emit("live")
        elif command == "CLEARMSG":
            if tags.get("target-msg-id"):
                self.deleted.emit(tags["target-msg-id"])
        elif command == "CLEARCHAT":
            self.cleared.emit(tags.get("target-user-id", ""))
        elif command == "NOTICE" and "msg_channel_suspended" in tags.get("msg-id", ""):
            self.status.emit("offline: channel not found")
        else:
            message = message_from_line(line)
            if message is not None:
                self.message.emit(message)
