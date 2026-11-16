"""The Chat panel: one place for chat, whatever it comes from.

It shows one of two things, and switching never adds another panel:

* **Your chats, mixed**: type your channel name for Twitch, YouTube and Kick ("Chat from"), and
  SprigCast reads each one's public chat as a guest (chat/twitch.py, youtube.py, kick.py) into one
  list, each line marked with its platform's colour. No sign-ins or keys, so it works for anyone.
  Connecting Twitch fills in the Twitch name by itself.
* **A chat link**: any web page that shows chat (a pop-out chat, StreamBridge, Streamer.bot,
  a multi-chat overlay), shown in the panel itself.

Pasting a twitch.tv, youtube.com, kick.com or tiktok.com link reads that chat natively instead.
TikTok is unofficial (TikTok has no public chat API): TikFinity is used when it's running.

    ┌ Chat ───────────────────── ↗  × ┐
    │ ● Twitch  ● YouTube  ● Kick   ⋯ │
    │ ▍mossy  hi chat!                │
    │ ▍fern   that leaf tho 🌿        │
    └─────────────────────────────────┘
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections import deque

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QTextDocument
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.chat import PLATFORMS, ChatMessage, name_for, readable_colour

log = logging.getLogger(__name__)
KEY = "chat"  # the panel's key in app/panels.py
SOURCE = "chat/source"  # "chat" (your channels, mixed) | "link" | "" (nothing picked yet); "twitch" = old "chat"
LINK = "chat/link"
NAMES = "chat/names"  # {"twitch": "", "youtube": "@handle", "kick": "name"}; an empty Twitch name = your account
OLD_CHANNEL = "chat/twitch_channel"  # before YouTube and Kick: a Twitch channel picked by hand
HINTS = {"twitch": "your Twitch name", "youtube": "@your YouTube handle", "kick": "your Kick name",
         "tiktok": "@your TikTok handle"}
KEEP = 300  # messages kept on screen
EMOTE_CACHE = 600  # distinct emote pictures kept before the cache starts over (long streams stay flat)
EMOTE_URL = "https://static-cdn.jtvnw.net/emoticons/v2/{id}/default/dark/1.0"
EMOTE_SIZE = 22

URL_RE = re.compile(r"(https?://[^\s<]+)")

CHIP_STYLE = (
    f"QPushButton {{ background: {theme.RAISED}; color: {theme.TEXT}; border: none; border-radius: 15px;"
    f" padding: 7px 16px; font-weight: 600; }}"
    f"QPushButton:hover {{ background: {theme.RAISED_HOVER}; }}"
    "QPushButton::menu-indicator { width: 0; image: none; }"
)
FIELD_STYLE = (
    f"QLineEdit {{ background: {theme.CHROME}; border: none; border-radius: 14px; padding: 8px 12px;"
    f" color: {theme.TEXT}; }}"
)
GO_STYLE = (
    f"QPushButton {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; border: none; border-radius: 15px;"
    f" padding: 7px 16px; font-weight: 700; }}"
    f"QPushButton:hover {{ background: {theme.ACCENT_HOVER}; }}"
)


def mentions(text: str, names: frozenset[str]) -> bool:
    """Does the line say one of your names ("@surakage", "Surakage")?"""
    if not names:
        return False
    words = {w.lstrip("@").strip(".-").lower() for w in re.findall(r"@?[\w.-]+", text)}
    return not words.isdisjoint(names)


def message_html(message: ChatMessage, mark: bool = False, names: frozenset[str] = frozenset()) -> str:
    """One chat line as rich text: coloured name, then the words with emotes and links.
    `mark`: start with the platform's colour, for when several chats are mixed.
    `names`: your own names, so lines that mention you stand out."""
    colour = readable_colour(message.color, message.user)
    start = ""
    if mark and message.platform in PLATFORMS:
        start = f"<span style='color:{PLATFORMS[message.platform][1]}; font-weight:700'>▍</span>"
    if message.notice:
        return (f"<p style='margin:6px 0'>{start}<span style='color:{theme.SELECT_TEXT}; background:{theme.SELECT_BG};'>"
                f"&nbsp;✿ {_body(message)}&nbsp;</span></p>")
    name = f"<span style='color:{colour}; font-weight:600'>{html.escape(message.user)}</span>"
    marks = ""
    if "broadcaster" in message.badges:
        marks = f"<span style='color:{theme.LIVE_TEXT}'>● </span>"
    elif "moderator" in message.badges:
        marks = f"<span style='color:{theme.ACCENT}'>● </span>"
    elif "vip" in message.badges:
        marks = f"<span style='color:{theme.INFO}'>● </span>"
    elif "member" in message.badges or "subscriber" in message.badges or "og" in message.badges:
        marks = ""
    body = _body(message)
    if message.action:
        body = f"<span style='color:{colour}'><i>{body}</i></span>"
    if mentions(message.text, names):  # ChatView also washes the whole line soft green
        body = f"<span style='color:{theme.SELECT_TEXT}'>{body}</span>"
    return f"<p style='margin:3px 0'>{start}{marks}{name}&nbsp; {body}</p>"


def _body(message: ChatMessage) -> str:
    text, out, at = message.text, [], 0
    for start, end, emote_id in message.emotes:
        if start < at or end >= len(text):
            continue
        out.append(_words(text[at:start]))
        out.append(f"<img src='emote:{html.escape(emote_id)}' width={EMOTE_SIZE} height={EMOTE_SIZE}"
                   f" title='{html.escape(text[start:end + 1])}' style='vertical-align: middle'>")
        at = end + 1
    out.append(_words(text[at:]))
    return "".join(out)


def _words(text: str) -> str:
    parts = URL_RE.split(text)
    return "".join(
        f"<a href='{html.escape(p)}' style='color:{theme.ACCENT}'>{html.escape(p)}</a>" if i % 2 else html.escape(p)
        for i, p in enumerate(parts)
    )


class ChatView(QTextBrowser):
    """The native chat list. Keeps the last KEEP lines; emote pictures load once each.

    Scrolled up to read? New lines don't yank you down: a "New messages" pill appears instead."""

    def __init__(self):
        super().__init__()
        self.setOpenExternalLinks(True)
        self.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.setStyleSheet(f"QTextBrowser {{ background: transparent; color: {theme.TEXT}; border: none; }}")
        self.document().setDocumentMargin(2)
        self.document().setMaximumBlockCount(KEEP)  # the oldest line leaves as a new one comes
        self.messages: deque[ChatMessage] = deque(maxlen=KEEP)
        self.mixed = False  # several chats at once: mark each line with its platform
        self.names: frozenset[str] = frozenset()  # your channel names, lowercase, for mention highlights
        self._urls: dict[str, str] = {}  # emote id -> picture, for platforms that send their own
        self._net = None
        self._asked: set[str] = set()
        self._relayout = QTimer(self)  # pictures arriving together cost one re-layout, not one each
        self._relayout.setSingleShot(True)
        self._relayout.setInterval(120)
        self._relayout.timeout.connect(self._pictures_arrived)
        self.jump = QPushButton("New messages ↓", self)
        self.jump.setStyleSheet(GO_STYLE.replace("padding: 7px 16px", "padding: 5px 14px")
                                .replace("border-radius: 15px", "border-radius: 13px"))
        self.jump.setCursor(Qt.CursorShape.PointingHandCursor)
        self.jump.clicked.connect(self.to_bottom)
        self.jump.hide()
        # "Twitch chat reconnecting…": a chat that dropped shouldn't look like a quiet one
        self.notice = QLabel(self)
        self.notice.setStyleSheet(f"background: {theme.PANEL}; color: {theme.WARN}; border-radius: 12px;"
                                  " padding: 4px 12px; font-size: 12px;")
        self.notice.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.notice.hide()
        self.verticalScrollBar().valueChanged.connect(self._scrolled)

    def set_notice(self, text: str) -> None:
        if not text:
            self.notice.hide()
            return
        self.notice.setText(text)
        self.notice.show()
        self._place_notice()

    def _place_notice(self) -> None:
        self.notice.adjustSize()
        self.notice.move((self.width() - self.notice.width()) // 2, 10)
        self.notice.raise_()

    def at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def add(self, message: ChatMessage) -> None:
        stay = self.at_bottom()
        if len(self._asked) > EMOTE_CACHE:
            self._fresh_cache()
        self.messages.append(message)
        self._urls.update(message.images)
        self.append(message_html(message, self.mixed, self.names))
        self._wash(self.document().lastBlock(), mentions(message.text, self.names))
        if stay:
            self.to_bottom()
        else:
            self.jump.show()
            self._place_jump()

    def to_bottom(self) -> None:
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        self.jump.hide()

    def _scrolled(self) -> None:
        if self.jump.isVisible() and self.at_bottom():
            self.jump.hide()

    def _place_jump(self) -> None:
        self.jump.adjustSize()
        self.jump.move((self.width() - self.jump.width()) // 2, self.height() - self.jump.height() - 10)
        self.jump.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().resizeEvent(event)
        if self.jump.isVisible():
            self._place_jump()
        if self.notice.isVisible():
            self._place_notice()

    def _fresh_cache(self) -> None:
        """Forget emote pictures (the document keeps every one it was given), then redraw what's on screen."""
        self._asked.clear()
        self._urls = {k: v for m in self.messages for k, v in m.images.items()}
        self.document().clear()  # also drops the pictures it holds
        self.redraw()

    def remove(self, *, message_id: str = "", user_id: str = "", platform: str = "", everything: bool = False) -> None:
        """A moderator deleted a message, timed someone out, or cleared chat (on one platform)."""
        def goes(m: ChatMessage) -> bool:
            if platform and m.platform != platform:
                return False
            return everything or bool(message_id and m.id == message_id) or bool(user_id and m.user_id == user_id)

        kept = [m for m in self.messages if not goes(m)]
        if len(kept) != len(self.messages):
            self.messages = deque(kept, maxlen=KEEP)
            self.redraw()

    def redraw(self) -> None:
        self.setHtml("".join(message_html(m, self.mixed, self.names) for m in self.messages))
        block = self.document().firstBlock()
        for message in self.messages:
            self._wash(block, mentions(message.text, self.names))
            block = block.next()
        self.to_bottom()

    @staticmethod
    def _wash(block, on: bool) -> None:
        """Lines that mention you get a soft green background across the whole width (a new line
        copies the one before it, so every line sets it either way)."""
        from PySide6.QtGui import QTextCursor

        cursor = QTextCursor(block)
        fmt = cursor.blockFormat()
        if on:
            fmt.setBackground(QColor(theme.SELECT_BG))
        elif fmt.hasProperty(fmt.Property.BackgroundBrush):
            fmt.clearBackground()
        else:
            return
        cursor.setBlockFormat(fmt)

    def clear_chat(self) -> None:
        self.messages.clear()
        self.clear()
        self.jump.hide()

    def loadResource(self, kind, url: QUrl):  # noqa: N802 (Qt override)
        if url.scheme() == "emote" and kind == QTextDocument.ResourceType.ImageResource:
            self._fetch(url)
            return None  # drawn as a blank box until the picture arrives
        return super().loadResource(kind, url)

    def _fetch(self, url: QUrl) -> None:
        emote_id = url.path()
        if emote_id in self._asked or not re.fullmatch(r"[\w-]{1,64}", emote_id):
            return
        self._asked.add(emote_id)
        from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest

        if self._net is None:
            self._net = QNetworkAccessManager(self)
        address = self._urls.get(emote_id) or EMOTE_URL.format(id=emote_id)
        if not address.startswith("https://"):
            return
        reply = self._net.get(QNetworkRequest(QUrl(address)))
        reply.finished.connect(lambda: self._fetched(reply, url))

    def _fetched(self, reply, url: QUrl) -> None:
        from PySide6.QtGui import QImage

        image = QImage()
        if image.loadFromData(reply.readAll().data()):
            self.document().addResource(QTextDocument.ResourceType.ImageResource, url, image)
            self._relayout.start()
        reply.deleteLater()

    def _pictures_arrived(self) -> None:
        stay = self.at_bottom()
        self.document().markContentsDirty(0, self.document().characterCount())
        self.viewport().update()
        if stay:
            self.to_bottom()


def saved_names() -> dict[str, str]:
    """The channel names typed in "Chat from", by platform (older Twitch-only settings carried over)."""
    settings = QSettings()
    try:
        names = json.loads(str(settings.value(NAMES, "") or "{}"))
    except ValueError:
        names = {}
    names = {k: str(v) for k, v in names.items() if k in PLATFORMS and v} if isinstance(names, dict) else {}
    old = str(settings.value(OLD_CHANNEL, "") or "")
    if old and "twitch" not in names:
        names["twitch"] = old
    return names


def save_names(names: dict[str, str]) -> None:
    QSettings().setValue(NAMES, json.dumps({k: v for k, v in names.items() if v}))
    QSettings().remove(OLD_CHANNEL)


class ChatPanel(QWidget):
    """The Chat panel's body. Nothing connects or loads until the panel is first shown (activate)."""

    def __init__(self, connect_twitch=None):
        from sprigcast.chat.hub import ChatHub

        super().__init__()
        self.connect_twitch = connect_twitch  # opens Connect Twitch; the panel hears the result itself
        # The readers live in the hub, shared with chat sources on your stream (sources/chat.py).
        self.hub = ChatHub(self.reading, self)
        self._was_live: set[tuple[str, str]] = set()  # (platform, channel) chats that have read since starting
        self.web = None
        self.active = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.where = QLabel("")
        self.where.setStyleSheet(f"color: {theme.MUTED}")
        top.addWidget(self.where, 1)
        self.more = QPushButton("⋯")
        self.more.setToolTip("Change where chat comes from")
        self.more.setStyleSheet(CHIP_STYLE.replace("padding: 7px 16px", "padding: 3px 12px")
                                .replace("border-radius: 15px", "border-radius: 10px"))
        self.more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more.clicked.connect(self._open_menu)
        top.addWidget(self.more)
        root.addLayout(top)

        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.empty = self._empty_page()
        self.link_page = self._link_page()
        self.names_page = self._names_page()
        self.view = ChatView()
        self.web_holder = QWidget()
        QVBoxLayout(self.web_holder).setContentsMargins(0, 0, 0, 0)
        for page in (self.empty, self.link_page, self.names_page, self.view, self.web_holder):
            self.pages.addWidget(page)
        root.addWidget(self._say_row())
        self.pages.currentChanged.connect(lambda _: self._show_say())

        from sprigcast.output import twitch_auth

        twitch_auth.account_events().changed.connect(self._account_changed)
        self.hub.message.connect(self.view.add)
        self.hub.deleted.connect(lambda platform, mid: self.view.remove(platform=platform, message_id=mid))
        self.hub.cleared.connect(lambda platform, uid: self.view.remove(platform=platform, user_id=uid,
                                                                        everything=not uid))
        self.hub.changed.connect(self._hub_changed)

    # Chatting back -----------------------------------------------------------------------

    sent = Signal(str)  # "" when sent, else why not (from the sending thread)

    def _say_row(self) -> QWidget:
        """A box under the chat to say something on Twitch as your connected account."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.say = QLineEdit()
        self.say.setStyleSheet(FIELD_STYLE)
        self.say.setMaxLength(500)
        self.say.returnPressed.connect(self.send)
        layout.addWidget(self.say, 1)
        self.say_button = QPushButton("Send")
        self.say_button.setStyleSheet(GO_STYLE)
        self.say_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.say_button.clicked.connect(self.send)
        layout.addWidget(self.say_button)
        self.sent.connect(self._sent)
        self._sending = False
        row.hide()
        self.say_row = row
        return row

    def _say_target(self) -> str | None:
        """The Twitch channel a message would go to, or None when there's nowhere to send."""
        return self.hub.names().get("twitch") if self.pages.currentWidget() is self.view else None

    def _show_say(self) -> None:
        channel = self._say_target()
        self.say_row.setVisible(channel is not None)
        if channel is None:
            return
        mine = self._account()
        if not mine:
            self.say.setPlaceholderText("Connect Twitch to chat back")
            self.say_button.setText("Connect")
        else:
            self.say.setPlaceholderText("Chat on Twitch…")
            self.say.setToolTip(f"Sends to {channel}'s Twitch chat as you")
            self.say_button.setText("Send")

    def send(self) -> None:
        from sprigcast.output import twitch_auth

        channel = self._say_target()
        if channel is None or self._sending:
            return
        if not self._account() or not twitch_auth.can_send_chat():
            self.say.setToolTip("SprigCast needs your OK once to send chat: connect Twitch again.")
            self._connect()
            return
        text = self.say.text().strip()
        if not text:
            return
        self._sending = True
        self.say.setEnabled(False)

        def work():
            try:
                twitch_auth.send_chat(channel, text)
                self.sent.emit("")
            except Exception as exc:  # worded for you by twitch_auth, or a surprise
                self.sent.emit(str(exc) or "Couldn't send")

        from sprigcast.core import workers

        workers.run(work, name="chat-send")

    def _sent(self, problem: str) -> None:
        self._sending = False
        self.say.setEnabled(True)
        if problem:
            self.say.setToolTip(problem)
            self.where.setText(f"<span style='color:{theme.BAD}'>●</span> {html.escape(problem)}")
            QTimer.singleShot(5000, self._show_states)
        else:
            self.say.clear()
            self.say.setToolTip("")
        self.say.setFocus()

    @property
    def readers(self) -> dict[str, object]:
        return self.hub.readers

    @property
    def states(self) -> dict[str, str]:
        return self.hub.states

    # Pages ------------------------------------------------------------------------------

    def _buttons(self, *specs) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        for text, style, slot in specs:
            button = QPushButton(text)
            button.setStyleSheet(style)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            row.addWidget(button)
        return row

    def _empty_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)
        layout.addStretch(1)
        from sprigcast.app.empty import NappingSloth

        layout.addWidget(NappingSloth(0.8), 0, Qt.AlignmentFlag.AlignHCenter)
        title = QLabel("Chat shows up here")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 15px; font-weight: 600")
        hint = QLabel("Add your channel names, or paste any chat link.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED}")
        layout.addWidget(title)
        layout.addWidget(hint)
        row = self._buttons(("Add channels", GO_STYLE, self.ask_names), ("Paste a link", CHIP_STYLE, self.ask_link))
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(2)
        return page

    def _link_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)
        layout.addStretch(1)
        label = QLabel("Chat link")
        label.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600")
        layout.addWidget(label)
        self.link_field = QLineEdit()
        self.link_field.setPlaceholderText("https://…")
        self.link_field.setStyleSheet(FIELD_STYLE)
        self.link_field.returnPressed.connect(self._use_link)
        layout.addWidget(self.link_field)
        hint = QLabel("Twitch, YouTube and Kick links read chat natively. Any other page shows here as it is.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED}")
        layout.addWidget(hint)
        layout.addLayout(self._buttons(("Cancel", CHIP_STYLE, self.refresh), ("Use", GO_STYLE, self._use_link)))
        layout.addStretch(2)
        return page

    def _names_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)
        layout.addStretch(1)
        label = QLabel("Chat from")
        label.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600")
        layout.addWidget(label)
        self.name_fields: dict[str, QLineEdit] = {}
        for platform, (title, colour) in PLATFORMS.items():
            row = QHBoxLayout()
            row.setSpacing(8)
            tag = QLabel(f"<span style='color:{colour}'>●</span> {title}")
            tag.setFixedWidth(78)
            row.addWidget(tag)
            field = QLineEdit()
            field.setStyleSheet(FIELD_STYLE)
            field.setPlaceholderText(HINTS[platform])
            field.returnPressed.connect(self._use_names)
            row.addWidget(field, 1)
            self.name_fields[platform] = field
            layout.addLayout(row)
        hint = QLabel("Just names: SprigCast reads public chat, no sign-in. Leave any empty. "
                      "TikTok is unofficial and uses TikFinity when it's running.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED}")
        layout.addWidget(hint)
        layout.addLayout(self._buttons(("Cancel", CHIP_STYLE, self.refresh), ("Use", GO_STYLE, self._use_names)))
        layout.addStretch(2)
        return page

    # Choosing a source --------------------------------------------------------------------

    def activate(self) -> None:
        """The panel is on screen: start reading (only the first time does anything)."""
        if self.active:
            return
        self.active = True
        self.refresh()

    def sleep(self) -> None:
        """The panel was taken off every screen: stop reading and free the page until it's shown again."""
        if not self.active:
            return
        self.active = False
        self.hub.want("panel", False)  # chat sources on your stream may still want it
        if self.web is not None:
            self.web.set_url("about:blank")

    def ask_link(self) -> None:
        self.link_field.setText(str(QSettings().value(LINK, "") or ""))
        self.pages.setCurrentWidget(self.link_page)
        self.where.setText("Pick a chat")
        self.link_field.setFocus()
        self.link_field.selectAll()

    def ask_names(self) -> None:
        names = saved_names()
        mine = self._account()
        for platform, field in self.name_fields.items():
            field.setText(names.get(platform, ""))
        if mine:
            self.name_fields["twitch"].setPlaceholderText(f"{mine} (connected)")
        self.pages.setCurrentWidget(self.names_page)
        self.where.setText("Your channels")
        self.name_fields["twitch" if not mine else "youtube"].setFocus()

    def _use_names(self) -> None:
        names = {}
        for platform, field in self.name_fields.items():
            text = field.text().strip()
            if not text:
                continue
            name = name_for(platform, text)
            if name is None:
                field.setFocus()
                field.selectAll()
                field.setToolTip("That doesn't look like a channel name")
                return
            names[platform] = name
        if names.get("twitch") == self._account():
            names["twitch"] = ""  # follow the connected account, even if it's renamed
        save_names(names)
        QSettings().setValue(SOURCE, "chat")
        self.refresh()

    def _use_link(self) -> None:
        from sprigcast.app.custom_docks import normalise_url

        text = self.link_field.text().strip()
        if not text:
            return
        lower = text.lower()
        is_link = "://" in text or "." in text or "/" in text
        platform = next((p for p, host in (("twitch", "twitch.tv"), ("youtube", "youtu"), ("kick", "kick.com"),
                                                  ("tiktok", "tiktok.com"))
                         if host in lower), None) if is_link else "twitch"
        name = name_for(platform, text) if platform else None
        if name:
            names = saved_names()
            names[platform] = "" if platform == "twitch" and name == self._account() else name
            save_names(names)
            QSettings().setValue(SOURCE, "chat")
        else:
            QSettings().setValue(LINK, normalise_url(text))
            QSettings().setValue(SOURCE, "link")
        self.refresh()

    def use_chats(self) -> None:
        QSettings().setValue(SOURCE, "chat")
        if not self.reading():
            self.ask_names()
            return
        self.refresh()

    def use_link(self) -> None:
        if QSettings().value(LINK, ""):
            QSettings().setValue(SOURCE, "link")
            self.refresh()
        else:
            self.ask_link()

    def _connect(self) -> None:
        if self.connect_twitch is not None:
            self.connect_twitch()

    def _account_changed(self, login: str) -> None:
        """Connecting Twitch (here or in Settings) switches the panel to your chats; your channel joins them."""
        if login:
            names = saved_names()
            if names.get("twitch") == login.lower():
                names["twitch"] = ""
                save_names(names)
            QSettings().setValue(SOURCE, "chat")
        if self.active:
            self.refresh()

    def _account(self) -> str | None:
        from sprigcast.output import twitch_auth

        login = twitch_auth.remembered_login()
        if login is None:  # connected before SprigCast remembered it: ask the credential store once
            login = twitch_auth.connected_account() or ""
            QSettings().setValue(twitch_auth.LOGIN_SETTING, login)
        return login.lower() or None

    def reading(self) -> dict[str, str]:
        """Platform -> name for every chat to read (Twitch falls back to your connected account)."""
        names = {p: n for p, n in saved_names().items() if n}
        if "twitch" not in names:
            mine = self._account()
            if mine:
                names["twitch"] = mine
        return {p: names[p] for p in PLATFORMS if p in names}

    def source(self) -> tuple[str, object]:
        """("chat", {platform: name}) | ("link", url) | ("", "")."""
        settings = QSettings()
        kind = str(settings.value(SOURCE, "") or "")
        link = str(settings.value(LINK, "") or "")
        if kind == "link" and link:
            return "link", link
        names = self.reading()
        if names:
            return "chat", names
        if link:
            return "link", link
        return "", ""

    def refresh(self) -> None:
        if not self.active:
            return
        kind, value = self.source()
        if kind != "link" and self.web is not None:
            self.web.set_url("about:blank")  # free the page's memory; the browser itself stays for later
        self.hub.want("panel", kind == "chat")
        self.hub.refresh()  # names may have changed
        if kind == "chat":
            self._read(value)
        elif kind == "link":
            self._show_link(value)
        else:
            self.where.setText("No chat yet")
            self.where.setToolTip("")
            self.pages.setCurrentWidget(self.empty)

    def _read(self, names: dict[str, str]) -> None:
        mixed = len(names) > 1
        mine = frozenset(n.lstrip("@").lower() for n in names.values() if not n.startswith(("v:", "channel/")))
        if mixed != self.view.mixed or mine != self.view.names:
            self.view.mixed, self.view.names = mixed, mine
            self.view.redraw()  # with or without platform marks and mention highlights
        self.pages.setCurrentWidget(self.view)
        self._show_states()

    def reconnecting(self) -> list[str]:
        """Platforms whose chat was reading and has dropped (not ones simply waiting for a stream)."""
        out = []
        for platform, reader in self.hub.readers.items():
            key = (platform, reader.channel)
            state = self.hub.states.get(platform, "connecting")
            if state == "live":
                self._was_live.add(key)
            elif key in self._was_live and not state.startswith("waiting"):
                out.append(platform)
            elif state.startswith("waiting"):
                self._was_live.discard(key)  # the stream ended: nothing to reconnect to
        self._was_live &= {(p, r.channel) for p, r in self.hub.readers.items()}
        return out

    def _hub_changed(self) -> None:
        dropped = self.reconnecting()
        if dropped:
            titles = " and ".join(PLATFORMS[p][0] for p in dropped)
            self.view.set_notice(f"● {titles} chat reconnecting…")
        else:
            self.view.set_notice("")
        if self.active and self.pages.currentWidget() is self.view:
            self._show_states()
        self._show_say()

    def _show_states(self) -> None:
        parts, tips = [], []
        dropped = self.reconnecting()
        single = len(self.readers) == 1
        for platform, reader in self.readers.items():
            state = self.states.get(platform, "connecting")
            dot = theme.ACCENT if state == "live" else theme.WARN if state in ("connecting",) or \
                state.startswith("waiting") else theme.BAD
            title = PLATFORMS[platform][0]
            name = html.escape(reader.channel.removeprefix("v:"))
            label = f"{title} · {name}" if single else title
            parts.append(f"<span style='color:{dot}'>●</span> {label}")
            if state != "live" and platform in dropped:
                dot, state = theme.BAD, "reconnecting"
            words = {"live": "reading chat", "connecting": "connecting…"}.get(state, state.split(":", 1)[-1].strip())
            via = {"tikfinity": " via TikFinity", "direct": " straight from TikTok"}.get(getattr(reader, "via", ""), "")
            tips.append(f"{title} ({reader.channel}): {words}{via}")
        self.where.setText("&nbsp;&nbsp;".join(parts))
        self.where.setToolTip("\n".join(tips))

    def _show_link(self, url: str) -> None:
        from sprigcast.app.custom_docks import WebDockBody

        if self.web is None:
            self.web = WebDockBody(url)
            self.web_holder.layout().addWidget(self.web)
        elif self.web.url != url:
            self.web.set_url(url)
        elif self.web.view is not None and self.web.view.url().toString() == "about:blank":
            self.web.set_url(url)
        self.web.ensure_loaded()
        self.pages.setCurrentWidget(self.web_holder)
        host = QUrl(url).host() or url
        port = QUrl(url).port()
        shown = f"{host}:{port}" if port > 0 else host
        self.where.setText(f"<span style='color:{theme.INFO}'>●</span> Link · {html.escape(shown)}")
        self.where.setToolTip(url)

    # The ⋯ menu -------------------------------------------------------------------------

    def _open_menu(self) -> None:
        self.build_menu().exec(self.more.mapToGlobal(self.more.rect().bottomLeft()))

    def build_menu(self) -> QMenu:
        menu = QMenu(self)
        kind, _ = self.source()
        if self.reading():
            chats = menu.addAction("Your channels")
            chats.setCheckable(True)
            chats.setChecked(kind == "chat")
            chats.triggered.connect(self.use_chats)
        saved = str(QSettings().value(LINK, "") or "")
        if saved:
            link = menu.addAction("Chat link")
            link.setCheckable(True)
            link.setChecked(kind == "link")
            link.triggered.connect(self.use_link)
        if menu.actions():
            menu.addSeparator()
        menu.addAction("Channels…").triggered.connect(self.ask_names)
        menu.addAction("Change link…" if saved else "Paste a link…").triggered.connect(self.ask_link)
        if not self._account():
            menu.addAction("Connect Twitch…").triggered.connect(self._connect)
        if "tiktok" in self.reading():
            menu.addAction("TikTok key…").triggered.connect(self._tiktok_key)
        menu.addSeparator()
        if kind == "chat":
            menu.addAction("Clear").triggered.connect(self.view.clear_chat)
        elif kind == "link" and self.web is not None:
            menu.addAction("Reload").triggered.connect(self.web.reload)
        return menu

    def _tiktok_key(self) -> None:
        """An optional Euler Stream key: steadier TikTok reading when TikFinity isn't running."""
        from PySide6.QtWidgets import QInputDialog

        from sprigcast.chat.tiktok import KEY_SETTING

        key, ok = QInputDialog.getText(
            self, "TikTok key", "Optional. Without TikFinity, TikTok chat goes through a free signing service "
            "(eulerstream.com) with shared limits. Your own free key from there lifts them.",
            QLineEdit.EchoMode.Password, str(QSettings().value(KEY_SETTING, "") or ""))
        if ok:
            QSettings().setValue(KEY_SETTING, key.strip())

    def shutdown(self) -> None:
        self.hub.stop()
