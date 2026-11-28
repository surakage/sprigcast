"""Read a Kick channel's chat from just its name, through Kick's official developer API.

Kick's official API sends chat only to a web server, by webhook, which a desktop app can't receive. So
SprigCast's own small relay (integrations/kick-chat, at kickchat.slothbloom.com) takes Kick's signed
webhooks and passes each channel's chat down one WebSocket to whoever is reading it. Nobody signs in: the
relay subscribes with SprigCast's own Kick app. SprigCast never talks to kick.com itself (it sits behind
Cloudflare, which turned the old unofficial reader away), only to the relay.

Light and safe, like the other readers:
* it runs only while the hub wants Kick chat (the Chat panel on screen, or a chat source in a bloom);
* one WebSocket on Qt's event loop, no thread; a "ping" every few minutes keeps the link honest;
* a failure is a quiet status ("offline: <why>") and a slow retry, never a dialog, and never near the stream.

Relay messages (JSON text frames): {"type": "ready", "user_id", "slug"}, {"type": "error", "why"},
{"type": "event", "event": "<Kick event type>", "data": {<Kick's webhook payload>}}, and "pong".
"""

from __future__ import annotations

import json
import logging
import os
import re

from PySide6.QtCore import QObject, QSettings, QTimer, QUrl, Signal

from sprigcast.chat import ChatMessage

log = logging.getLogger(__name__)

RELAY = "wss://kickchat.slothbloom.com/v1/chat"
RELAY_SETTING = "kick/chat_relay"  # another relay (testing); SPRIGCAST_KICK_CHAT_URL overrides both
EMOTE_URL = "https://files.kick.com/emotes/{id}/fullsize"
BACKOFF = (5, 15, 30, 60, 120, 300)  # seconds between tries after a drop
QUICK_RETRY = 1  # the first try back after a working link drops
NOT_FOUND_RETRY = 300  # a name Kick doesn't know: look again now and then (it may be a typo being fixed)
PING_EVERY = 4 * 60 * 1000
QUIET_LIMIT = 9 * 60 * 1000  # nothing at all (not even a pong) for this long: the link is dead
_EMOTE = re.compile(r"\[emote:(\d+):([^\]]*)\]")
_SLUG = re.compile(r"^[A-Za-z0-9_-]{2,30}$")


def slug_from_text(text: str) -> str | None:
    """'kick.com/name', 'name' or '@name' -> 'name'."""
    text = text.strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?kick\.com/(?:popout/)?([A-Za-z0-9_-]{2,30})(?:[/?#].*)?$", text, re.I)
    if m:
        return m.group(1).lower()
    text = text.lstrip("@")
    return text.lower() if _SLUG.match(text) else None


def relay_url(slug: str) -> str:
    base = os.environ.get("SPRIGCAST_KICK_CHAT_URL") or str(QSettings().value(RELAY_SETTING, "") or "") or RELAY
    return f"{base}?channel={slug}"  # slugs are only letters, digits, - and _


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


def _name(person) -> str:
    return str((person or {}).get("username") or "someone") if isinstance(person, dict) else "someone"


def message_from_event(event: str, data: dict) -> ChatMessage | None:
    """One Kick webhook (its type and payload) -> a ChatMessage, or None for ones SprigCast doesn't show."""
    if event == "chat.message.sent":
        sender = data.get("sender") or {}
        identity = sender.get("identity") or {}
        text, emotes, images = _content(str(data.get("content", "")))
        badges = tuple(str(b.get("type", "")) for b in identity.get("badges") or [] if isinstance(b, dict) and b.get("type"))
        colour = str(identity.get("username_color") or "")
        return ChatMessage(platform="kick", user=_name(sender), text=text,
                           color=colour if re.fullmatch(r"#[0-9A-Fa-f]{6}", colour) else "",
                           id=str(data.get("message_id", "")), user_id=str(sender.get("user_id", "")),
                           badges=badges, emotes=emotes, images=images)
    if event == "channel.subscription.new":
        text = f"{_name(data.get('subscriber'))} subscribed"
    elif event == "channel.subscription.renewal":
        months = data.get("duration")
        text = f"{_name(data.get('subscriber'))} subscribed" + (f" for {months} months" if months else " again")
    elif event == "channel.subscription.gifts":
        gifted = data.get("giftees") or []
        who = "Someone" if (data.get("gifter") or {}).get("is_anonymous") else _name(data.get("gifter"))
        text = f"{who} gifted {len(gifted)} sub{'s' if len(gifted) != 1 else ''}"
    else:
        return None
    return ChatMessage(platform="kick", user="", text=text, notice=True)


class KickChat(QObject):
    message = Signal(object)
    deleted = Signal(str)
    cleared = Signal(str)  # a banned or timed-out user id
    status = Signal(str)
    platform = "kick"

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.channel = ""
        self.room = ""  # the channel's Kick id, once the relay has said it
        self.socket = None
        self._wanted = False
        self._tries = 0
        self._why = ""  # the last failure logged, so a long outage is one log line, not one a try
        self._wait_after_close = 0  # set by the relay's error: how long to wait before trying again
        self._seen: list[str] = []
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._open)
        self._ping = QTimer(self)
        self._ping.setInterval(PING_EVERY)
        self._ping.timeout.connect(self._send_ping)
        self._quiet = QTimer(self)
        self._quiet.setSingleShot(True)
        self._quiet.setInterval(QUIET_LIMIT)
        self._quiet.timeout.connect(lambda: self._failed("no word from the Kick chat relay"))

    def start(self, slug: str) -> None:
        if self._wanted and slug == self.channel:
            return
        self.stop()
        self.channel, self._wanted, self._tries, self._why, self.room = slug, True, 0, "", ""
        self._open()

    def stop(self) -> None:
        self._wanted = False
        self._retry.stop()
        self._close()

    def _close(self) -> None:
        self._ping.stop()
        self._quiet.stop()
        if self.socket is not None:
            socket, self.socket = self.socket, None
            for signal in (socket.connected, socket.disconnected, socket.textMessageReceived):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
            socket.abort()
            socket.deleteLater()

    def _open(self) -> None:
        from PySide6.QtWebSockets import QWebSocket

        if not self._wanted:
            return
        self.status.emit("connecting")
        self._wait_after_close = 0
        self.socket = QWebSocket()
        self.socket.connected.connect(self._connected)
        self.socket.disconnected.connect(self._disconnected)
        self.socket.textMessageReceived.connect(self.handle_text)
        self.socket.open(QUrl(relay_url(self.channel)))

    def _connected(self) -> None:
        self._ping.start()
        self._quiet.start()

    def _disconnected(self) -> None:
        # never connected (no network, relay down) or dropped later. A drop after reading fine is usually the
        # relay being moved or updated by Cloudflare (code 1001/1006, overnight soak 2026-11-19): the close code
        # goes in the log, and the first try back is quick so little chat is missed.
        was_reading = self._quiet.isActive()
        why = "Kick chat isn't reachable right now"
        if was_reading:
            raw = self.socket.closeCode() if self.socket is not None else 0
            code = int(getattr(raw, "value", raw) or 0)
            reason = self.socket.closeReason() if self.socket is not None else ""
            why = f"disconnected (code {code}{': ' + reason if reason else ''})" if code else "disconnected"
        wait = self._wait_after_close or (QUICK_RETRY if was_reading and self._tries == 0 else 0)
        self._failed(why, wait)

    def _send_ping(self) -> None:
        if self.socket is not None:
            self.socket.sendTextMessage("ping")

    def handle_text(self, payload: str) -> None:
        self._quiet.start()
        if payload == "pong":
            return
        try:
            frame = json.loads(payload)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        kind = frame.get("type")
        if kind == "ready":
            self._tries, self._why = 0, ""
            self.room = str(frame.get("user_id") or "")  # the channel's id: its 7TV emotes
            self.status.emit("live")
        elif kind == "error":
            why = str(frame.get("why") or "Kick chat isn't available")[:120]
            self._wait_after_close = NOT_FOUND_RETRY if "no kick channel" in why.lower() else 0
            self._failed(why, self._wait_after_close)
        elif kind == "event":
            data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
            event = str(frame.get("event") or "")
            if event == "moderation.banned":
                user_id = (data.get("banned_user") or {}).get("user_id")
                if user_id:
                    self.cleared.emit(str(user_id))
                return
            message = message_from_event(event, data)
            if message is None:
                return
            if message.id:
                if message.id in self._seen:
                    return
                self._seen = (self._seen + [message.id])[-300:]
            self.message.emit(message)

    def _failed(self, why: str, wait: int = 0) -> None:
        self._close()
        if not self._wanted or self._retry.isActive():
            return
        if not wait:
            wait = BACKOFF[min(self._tries, len(BACKOFF) - 1)]
            self._tries += 1
        if why != self._why:
            log.info("kick chat: %s; trying again in %d s", why, wait)  # no names
            self._why = why
        self.status.emit(f"offline: {why}")
        self._retry.start(wait * 1000)
