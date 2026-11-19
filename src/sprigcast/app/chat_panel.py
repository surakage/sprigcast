"""The Chat panel: one place for chat, whatever it comes from.

It shows one of two things, and switching never adds another panel:

* **Your chats, mixed**: type your channel name for Twitch and YouTube ("Chat from"), and
  SprigCast reads each one's public chat as a guest (chat/twitch.py, youtube.py) into one
  list, each line marked with its platform's colour. No sign-ins or keys, so it works for anyone.
  Connecting Twitch fills in the Twitch name by itself.
* **A chat link**: any web page that shows chat (a pop-out chat, StreamBridge, Streamer.bot,
  a multi-chat overlay), shown in the panel itself.

Pasting a twitch.tv or youtube.com link reads that chat natively instead.

    ┌ Chat ───────────────────── ↗  × ┐
    │ ● Twitch  ● YouTube           ⋯ │
    │ ▍mossy  hi chat!                │
    │ ▍fern   that leaf tho 🌿        │
    └─────────────────────────────────┘
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections import OrderedDict, deque

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QTextDocument
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.chat import PLATFORMS, ChatMessage, hidden, name_for, readable_colour

log = logging.getLogger(__name__)
KEY = "chat"  # the panel's key in app/panels.py
SOURCE = "chat/source"  # "chat" (your channels, mixed) | "link" | "" (nothing picked yet); "twitch" = old "chat"
LINK = "chat/link"
NAMES = "chat/names"  # {"twitch": "", "youtube": "@handle"}; an empty Twitch name = your account
OLD_CHANNEL = "chat/twitch_channel"  # before YouTube: a Twitch channel picked by hand
HINTS = {"twitch": "your Twitch name", "youtube": "@your YouTube handle", "kick": "your Kick name"}
KEEP = 300  # messages kept on screen
FLUSH_MS = 100  # new lines are drawn together this often
MENU_READY_MS = 5000  # after the panel is made: its ⋯ menu made ready (ChatPanel._ready_menu)
EMOTE_CACHE = 600  # distinct emote pictures handed to the page before it starts over (long streams stay flat)
EMOTE_KEEP = 400  # pictures kept in memory across a start-over, newest used first, so it fetches nothing again
EMOTE_FETCHES = 4  # pictures downloading at once; the rest wait their turn (one request each is UI-thread work)
EMOTE_URL = "https://static-cdn.jtvnw.net/emoticons/v2/{id}/default/dark/1.0"
EMOTE_SIZE = 22

URL_RE = re.compile(r"(https?://[^\s<]+)")

CHIP_STYLE = theme.button_style(15, "7px 16px")
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
    reply_asked = Signal(object)  # a ChatMessage: start a reply to its sender
    hide_asked = Signal(str)  # a chatter's name: never show them again
    moderate_asked = Signal(object, str, int)  # a ChatMessage, "timeout" / "ban" / "delete" / "block", seconds

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
        self.can_reply = lambda message: False  # the panel says which lines can be answered from here
        self.powers = lambda message: ()  # ...and what you may do to a line's sender (chat/moderation.POWERS)
        self.mixed = False  # several chats at once: mark each line with its platform
        self.names: frozenset[str] = frozenset()  # your channel names, lowercase, for mention highlights
        self._urls: dict[str, str] = {}  # emote id -> picture, for platforms that send their own
        self._net = None
        self._asked: set[str] = set()  # emote ids waiting, downloading, or that failed
        self._pictures: OrderedDict[str, object] = OrderedDict()  # emote id -> QImage, newest used last
        self._given = 0  # pictures handed to the page since it last started over
        self._queue: deque[QUrl] = deque()
        self._loading = 0
        self._relayout = QTimer(self)  # pictures arriving together cost one re-layout, not one each
        self._relayout.setSingleShot(True)
        self._relayout.setInterval(250)
        self._relayout.timeout.connect(self._pictures_arrived)
        # A busy chat (xqc's: dozens of lines a second) is drawn in batches: one document edit, one layout and
        # one scroll per FLUSH_MS, not one each (a line at a time froze the window for 400 ms on 2026-11-18).
        self._pending: list[ChatMessage] = []
        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(FLUSH_MS)
        self._flush.timeout.connect(self.flush)
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

    # Right-click a line -------------------------------------------------------------------

    def message_at(self, pos) -> ChatMessage | None:
        """The chat line under a point of the view, or None (between lines, or the page is being redrawn)."""
        self.flush()
        if self.document().blockCount() != len(self.messages):
            return None
        cursor = self.cursorForPosition(pos)
        block = cursor.block()
        rect = self.document().documentLayout().blockBoundingRect(block)
        y = pos.y() + self.verticalScrollBar().value()
        if not (rect.top() <= y <= rect.bottom()):
            return None
        return self.messages[block.blockNumber()]

    def line_menu(self, message: ChatMessage) -> QMenu:
        """Reply, Hide and Copy for one line (made here, shown by contextMenuEvent)."""
        menu = QMenu(self)
        if message.user and not message.notice:
            if self.can_reply(message):
                menu.addAction(f"Reply to {message.user}").triggered.connect(lambda: self.reply_asked.emit(message))
            menu.addAction(f"Hide {message.user}").triggered.connect(lambda: self.hide_asked.emit(message.user))
            menu.addSeparator()
            self._moderation(menu, message)
        menu.addAction("Copy message").triggered.connect(
            lambda: QApplication.clipboard().setText(f"{message.user}: {message.text}" if message.user else message.text))
        return menu

    def _moderation(self, menu: QMenu, message: ChatMessage) -> None:
        """Timeout, Ban, Delete and Block, where the platform and your sign-in have them."""
        from sprigcast.chat import moderation

        powers = self.powers(message)
        if not powers:
            return

        def ask(action: str, seconds: int = 0):
            return lambda: self.moderate_asked.emit(message, action, seconds)

        if "timeout" in powers:
            lengths = menu.addMenu(f"Timeout {message.user}")
            for seconds, words in moderation.TIMEOUTS:
                lengths.addAction(words.capitalize()).triggered.connect(ask("timeout", seconds))
        if "ban" in powers:  # the second step is the "are you sure?"
            menu.addMenu(f"Ban {message.user}").addAction("Ban for good").triggered.connect(ask("ban"))
        if "delete" in powers and message.id:
            menu.addAction("Delete message").triggered.connect(ask("delete"))
        if "block" in powers:
            menu.addAction(f"Block {message.user}").triggered.connect(ask("block"))
        menu.addSeparator()

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        message = self.message_at(event.pos())
        if message is None:
            return
        menu = self.line_menu(message)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        menu.popup(event.globalPos())  # popup(), not exec(): no second event loop inside the click

    def set_notice(self, text: str) -> None:
        if not text:
            self.notice.hide()
            return
        self.notice.setText(text)
        self.notice.show()
        self._place_notice()

    def _place_notice(self) -> None:
        self.notice.ensurePolished()  # measured with its padding, not the plain look
        self.notice.setWordWrap(False)
        self.notice.setMinimumSize(0, 0)
        self.notice.setMaximumSize(16777215, 16777215)
        self.notice.adjustSize()
        if self.notice.width() > self.width() - 24:  # a narrow chat: wrap instead of cutting it off
            self.notice.setWordWrap(True)
            self.notice.setFixedWidth(max(80, self.width() - 24))
            self.notice.setFixedHeight(self.notice.heightForWidth(self.notice.width()))
        self.notice.move((self.width() - self.notice.width()) // 2, 10)
        self.notice.raise_()

    def at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def add(self, message: ChatMessage) -> None:
        """A new line: kept now, drawn with the others that arrive in the same FLUSH_MS."""
        self.messages.append(message)
        self._urls.update(message.images)
        self._pending.append(message)
        if len(self._pending) > KEEP:
            del self._pending[:-KEEP]
        if not self._flush.isActive():
            self._flush.start()

    def flush(self) -> None:
        """Draw the waiting lines in one go."""
        from PySide6.QtGui import QTextCursor

        self._flush.stop()
        pending, self._pending = self._pending, []
        if not pending:
            return
        stay = self.at_bottom()
        if self._given > EMOTE_CACHE:
            self._fresh_cache()  # redraws every kept line, the waiting ones included
        else:
            document = self.document()
            cursor = QTextCursor(document)
            cursor.beginEditBlock()  # one change for the whole batch: laid out once
            cursor.movePosition(QTextCursor.MoveOperation.End)
            for message in pending:
                if not document.isEmpty():
                    cursor.insertBlock(cursor.blockFormat(), cursor.charFormat())
                cursor.insertHtml(message_html(message, self.mixed, self.names))
                self._wash_with(cursor, mentions(message.text, self.names))
            cursor.endEditBlock()
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
        """Start the page over (the document keeps every picture it was given), then redraw what's on screen.
        The pictures still in use come straight from memory, so nothing is downloaded again: before this,
        a start-over re-requested every emote on the UI thread and froze the window for 2 s (2026-11-18)."""
        self._given = 0
        self._urls = {k: v for m in self.messages for k, v in m.images.items()}
        self.document().clear()  # also drops the pictures it holds
        self.redraw()

    def remove(self, *, message_id: str = "", user_id: str = "", platform: str = "", everything: bool = False,
               user: str = "") -> None:
        """A moderator deleted a message, timed someone out, or cleared chat (on one platform).
        Only those lines leave the page; the rest isn't rebuilt (a full redraw cost 60 ms on a Twitch clear)."""
        from PySide6.QtGui import QTextCursor

        def goes(m: ChatMessage) -> bool:
            if platform and m.platform != platform:
                return False
            return (everything or bool(message_id and m.id == message_id) or bool(user_id and m.user_id == user_id)
                    or bool(user and not m.notice and m.user.lower() == user))  # a hidden account, by name

        self.flush()
        gone = [i for i, m in enumerate(self.messages) if goes(m)]
        if not gone:
            return
        document = self.document()
        if document.blockCount() != len(self.messages) or len(gone) == len(self.messages):
            self.messages = deque((m for m in self.messages if not goes(m)), maxlen=KEEP)
            self.redraw()
            return
        stay = self.at_bottom()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        for i in reversed(gone):  # from the bottom up, so the earlier block numbers still hold
            block = document.findBlockByNumber(i)
            cursor.setPosition(block.position())
            if block.next().isValid():
                cursor.setPosition(block.next().position(), QTextCursor.MoveMode.KeepAnchor)
            else:  # the last line: take the break before it instead
                cursor.setPosition(block.previous().position() + block.previous().length() - 1)
                cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        cursor.endEditBlock()
        self.messages = deque((m for m in self.messages if not goes(m)), maxlen=KEEP)
        if stay:
            self.to_bottom()

    def redraw(self) -> None:
        self._pending.clear()  # every kept line is drawn below
        self._flush.stop()
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

        ChatView._wash_with(QTextCursor(block), on)

    @staticmethod
    def _wash_with(cursor, on: bool) -> None:
        fmt = cursor.blockFormat()
        if on:
            fmt.setBackground(QColor(theme.SELECT_BG))
        elif fmt.hasProperty(fmt.Property.BackgroundBrush):
            fmt.clearBackground()
        else:
            return
        cursor.setBlockFormat(fmt)

    def clear_chat(self) -> None:
        self._pending.clear()
        self._flush.stop()
        self.messages.clear()
        self.clear()
        self.jump.hide()

    def loadResource(self, kind, url: QUrl):  # noqa: N802 (Qt override)
        if url.scheme() == "emote" and kind == QTextDocument.ResourceType.ImageResource:
            picture = self._pictures.get(url.path())
            if picture is not None:
                self._pictures.move_to_end(url.path())
                self._given += 1
                return picture
            self._fetch(url)
            return None  # drawn as a blank box until the picture arrives
        return super().loadResource(kind, url)

    def _fetch(self, url: QUrl) -> None:
        emote_id = url.path()
        if emote_id in self._asked or not re.fullmatch(r"[\w-]{1,64}", emote_id):
            return
        self._asked.add(emote_id)
        self._queue.append(url)
        self._next_fetch()

    def _next_fetch(self) -> None:
        from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest

        while self._queue and self._loading < EMOTE_FETCHES:
            url = self._queue.popleft()
            address = self._urls.get(url.path()) or EMOTE_URL.format(id=url.path())
            if not address.startswith("https://"):
                continue
            if self._net is None:
                self._net = QNetworkAccessManager(self)
            self._loading += 1
            reply = self._net.get(QNetworkRequest(QUrl(address)))
            reply.finished.connect(lambda reply=reply, url=url: self._fetched(reply, url))

    def _fetched(self, reply, url: QUrl) -> None:
        from PySide6.QtGui import QImage

        self._loading -= 1
        image = QImage()
        if image.loadFromData(reply.readAll().data()):
            if max(image.width(), image.height()) > 4 * EMOTE_SIZE:  # big source pictures: kept at a size we draw
                image = image.scaled(2 * EMOTE_SIZE, 2 * EMOTE_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            self._asked.discard(url.path())
            self._pictures[url.path()] = image
            while len(self._pictures) > EMOTE_KEEP:
                self._pictures.popitem(last=False)
            self._given += 1
            self.document().addResource(QTextDocument.ResourceType.ImageResource, url, image)
            self._relayout.start()
        reply.deleteLater()
        self._next_fetch()

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
        self.connect_twitch = connect_twitch  # opens Settings → Stream (sign-in is on the place); the panel hears the result
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
        QTimer.singleShot(MENU_READY_MS, self, lambda: self._ready_menu() if self.isVisible() else None)
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
        self.hidden_page = self._hidden_page()
        for page in (self.empty, self.link_page, self.names_page, self.hidden_page, self.view, self.web_holder):
            self.pages.addWidget(page)
        root.addWidget(self._say_row())
        self.pages.currentChanged.connect(lambda _: self._show_say())

        from sprigcast.output import twitch_auth

        twitch_auth.account_events().changed.connect(self._account_changed)
        from sprigcast.output import stream_details

        self._named: set[str] = set()  # sign-ins already checked this run for a chat to follow
        stream_details.account_events().changed.connect(self._signed_in)
        self.hub.message.connect(self.view.add)
        self.hub.deleted.connect(lambda platform, mid: self.view.remove(platform=platform, message_id=mid))
        self.hub.cleared.connect(lambda platform, uid: self.view.remove(platform=platform, user_id=uid,
                                                                        everything=not uid))
        self.hub.changed.connect(self._hub_changed)
        self.hub.hid.connect(lambda name: self.view.remove(user=name))
        self.view.hide_asked.connect(self.hub.hide)
        self.view.reply_asked.connect(self.reply_to)
        self.view.can_reply = lambda message: self.say_row.isVisibleTo(self) and bool(self._mine(message.platform)) \
            and any(platform == message.platform for platform, _ in self._say_targets())
        self.view.powers = self.powers
        self.view.moderate_asked.connect(self.moderate)
        self.acted.connect(self._acted)
        self.needs_allow.connect(self._offer_allow)

    # Chatting back -----------------------------------------------------------------------

    sent = Signal(str)  # "" when sent, else why not (from the sending thread)

    def _say_row(self) -> QWidget:
        """A box under the chat to say something on Twitch or Kick as your signed-in account."""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self._say_on = ""  # the platform a message goes to, when there's more than one to choose from
        self.say_where = QPushButton("")  # shown only with a choice: click to change where you're chatting
        self.say_where.setStyleSheet(CHIP_STYLE.replace("padding: 7px 16px", "padding: 7px 12px"))
        self.say_where.setCursor(Qt.CursorShape.PointingHandCursor)
        self.say_where.setToolTip("Change which chat you're writing in")
        self.say_where.clicked.connect(self._next_say_target)
        self.say_where.hide()
        layout.addWidget(self.say_where)
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

    def _mine(self, platform: str) -> str:
        """Your signed-in name on a platform ("" = not signed in). Settings only: no credential store."""
        if platform == "twitch":
            return self._account() or ""
        if platform == "kick":
            from sprigcast.output import stream_details

            return stream_details.account("kick").lower()
        return ""

    def _say_targets(self) -> list[tuple[str, str]]:
        """(platform, channel) for each chat you can write in: Twitch, and Kick once you're signed in to it."""
        from sprigcast.chat import moderation

        if self.pages.currentWidget() is not self.view:
            return []
        names = self.hub.names()
        return [(p, names[p]) for p in moderation.CAN_SAY if names.get(p) and (p == "twitch" or self._mine(p))]

    def _say_target(self) -> tuple[str, str] | None:
        """The (platform, channel) a message would go to, or None when there's nowhere to send."""
        targets = self._say_targets()
        return next((t for t in targets if t[0] == self._say_on), targets[0] if targets else None)

    def _next_say_target(self) -> None:
        targets = self._say_targets()
        now = self._say_target()
        if now in targets and len(targets) > 1:
            self._say_on = targets[(targets.index(now) + 1) % len(targets)][0]
        self._show_say()

    def _show_say(self) -> None:
        target = self._say_target()
        self.say_row.setVisible(target is not None)
        if target is None:
            return
        platform, channel = target
        title = PLATFORMS[platform][0]
        self.say_where.setVisible(len(self._say_targets()) > 1)
        self.say_where.setText(title)
        if not self._mine(platform):
            self.say.setPlaceholderText(f"Sign in to {title} to chat back")
            self.say_button.setText("Sign in")
        else:
            self.say.setPlaceholderText(f"Chat on {title}…")
            self.say.setToolTip(f"Sends to {channel}'s {title} chat as you")
            self.say_button.setText("Send")

    def send(self) -> None:
        from sprigcast.chat import moderation
        from sprigcast.output import twitch_auth

        target = self._say_target()
        if target is None or self._sending:
            return
        platform, channel = target
        if platform == "twitch" and (not self._account() or not twitch_auth.can_send_chat()):
            self.say.setToolTip("SprigCast needs your OK once to send chat: sign in to Twitch again.")
            self._connect()
            return
        text = self.say.text().strip()
        if not text:
            return
        self._sending = True
        self.say.setEnabled(False)

        def work():
            try:
                moderation.say(platform, channel, text)
                self.sent.emit("")
            except moderation.NeedsAllow as exc:
                self.sent.emit(str(exc))
                self.needs_allow.emit(exc.platform, str(exc))
            except Exception as exc:  # worded for you, or a surprise
                self.sent.emit(str(exc) or "Couldn't send")

        from sprigcast.core import workers

        workers.run(work, name="chat-send")

    def _sent(self, problem: str) -> None:
        self._sending = False
        self.say.setEnabled(True)
        if problem:
            self.say.setToolTip(problem)
            self._say_so(problem, bad=True)
        else:
            self.say.clear()
            self.say.setToolTip("")
        self.say.setFocus()

    def _say_so(self, text: str, bad: bool = False) -> None:
        """A few words in the panel's top line for five seconds (what was done, or why it wasn't)."""
        self.where.setText(f"<span style='color:{theme.BAD if bad else theme.ACCENT}'>●</span> {html.escape(text)}")
        QTimer.singleShot(5000, self, self._show_states)

    # Moderating ---------------------------------------------------------------------------

    acted = Signal(object, str, int, str)  # the ChatMessage, the action, seconds, "" or why not (from the worker)
    needs_allow = Signal(str, str)  # a platform whose sign-in doesn't allow it yet, and the words for that

    def powers(self, message: ChatMessage) -> tuple[str, ...]:
        """What you may do to a line's sender: nothing to yourself, to events, or where you aren't signed in."""
        from sprigcast.chat import moderation

        mine = self._mine(message.platform)
        if not mine or message.notice or not message.user_id or message.user.lower() == mine:
            return ()
        return moderation.POWERS.get(message.platform, ())

    def moderate(self, message: ChatMessage, action: str, seconds: int = 0) -> None:
        """Timeout, ban, delete or block from a chat line, on a worker; the top line says how it went."""
        from sprigcast.chat import moderation
        from sprigcast.core import workers

        channel = self.hub.names().get(message.platform)
        if not channel or action not in self.powers(message):
            return

        def work():
            try:
                moderation.act(message.platform, channel, action, user_id=message.user_id, message_id=message.id,
                               seconds=seconds)
                self.acted.emit(message, action, seconds, "")
            except moderation.NeedsAllow as exc:
                self.acted.emit(message, action, seconds, str(exc))
                self.needs_allow.emit(exc.platform, str(exc))
            except Exception as exc:  # worded for you, or a surprise
                self.acted.emit(message, action, seconds, str(exc) or "That didn't work")

        workers.run(work, name="chat-moderate")

    def _acted(self, message: ChatMessage, action: str, seconds: int, problem: str) -> None:
        from sprigcast.chat import moderation

        if problem:
            self._say_so(problem, bad=True)
            return
        if action == "delete":
            self.hub._deleted(message.platform, message.id)
        elif action in ("timeout", "ban"):  # the platform says so too; this is at once
            self.hub._cleared(message.platform, message.user_id)
        else:  # blocked: Twitch keeps their lines in your own chat, so they're hidden here as well
            self.hub.hide(message.user)
        self._say_so({"timeout": f"{message.user} timed out for {moderation.length_words(seconds)}",
                      "ban": f"{message.user} banned", "delete": "Message deleted",
                      "block": f"{message.user} blocked"}[action])

    def _offer_allow(self, platform: str, text: str) -> None:
        """A sign-in from before SprigCast asked for chat or moderation: offer the sign-in that allows it."""
        if platform != "kick":
            self._connect()
            return
        from sprigcast.app.signin_dialog import SignInDialog
        from sprigcast.output import service_signin

        if getattr(self, "_allowing", False):
            return
        self._allowing = True
        QSettings().setValue(service_signin.KICK_POWERS, True)  # this and later Kick sign-ins ask for both
        dialog = SignInDialog("kick", self.window(), begin=lambda s: service_signin.begin(s, keep=True), hint="")
        if dialog.message.text().startswith("Finish"):  # the browser opened
            dialog.message.setText("Finish in your browser: Kick asks you to let SprigCast send chat and moderate.")
        try:
            if dialog.exec():
                self._say_so("Kick allowed it. Try again.")
        finally:
            self._allowing = False
            dialog.deleteLater()

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
        hint = QLabel("Just names: SprigCast reads public chat, no sign-in. Leave any empty.")
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
        self._follow_sign_ins()

    def _signed_in(self, service: str) -> None:
        """You signed in to YouTube or Kick (anywhere in the app): its chat joins your chats here."""
        from sprigcast.output import stream_details

        if self.active:
            self._show_say()  # Kick's chat can be written in only while you're signed in to it
        if service not in saved_names() or not stream_details.account(service):
            return  # signed out, or a sign-in that named no chat: nothing here changes
        QSettings().setValue(SOURCE, "chat")
        if self.active:
            self.refresh()

    def _follow_sign_ins(self) -> None:
        """Signed in before sign-ins named the chat: look the channel up once, off the UI thread."""
        import threading

        from sprigcast.output import service_signin, stream_details

        names = saved_names()
        todo = [s for s in ("youtube", "kick") if s in PLATFORMS and s not in names and s not in self._named
                and stream_details.account(s)
                and not QSettings().value(service_signin.CHAT_NAMED.format(s), False, type=bool)]
        self._named.update(todo)
        if todo:
            threading.Thread(target=lambda: [service_signin.name_chat_from_sign_in(s) for s in todo],
                             name="chat-follow-sign-in", daemon=True).start()

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

    # Hidden accounts ---------------------------------------------------------------------

    def _hidden_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)
        label = QLabel("Hidden accounts")
        label.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600")
        layout.addWidget(label)
        self.hidden_field = QPlainTextEdit()
        self.hidden_field.setPlaceholderText("One name a line")
        self.hidden_field.setStyleSheet(FIELD_STYLE.replace("QLineEdit", "QPlainTextEdit"))
        layout.addWidget(self.hidden_field, 1)
        hint = QLabel("These never show in chat here or on your stream. They aren't blocked on the platform.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED}")
        layout.addWidget(hint)
        row = self._buttons(("Add common bots", CHIP_STYLE, self._add_common_bots))
        row.insertStretch(row.count(), 1)
        row.takeAt(0)  # the button sits on the left
        layout.addLayout(row)
        layout.addLayout(self._buttons(("Cancel", CHIP_STYLE, self.refresh), ("Save", GO_STYLE, self._use_hidden)))
        return page

    def ask_hidden(self) -> None:
        self.hidden_field.setPlainText("\n".join(hidden.load()))
        self.pages.setCurrentWidget(self.hidden_page)
        self.where.setText("Hidden accounts")
        self._show_say()
        self.hidden_field.setFocus()

    def _add_common_bots(self) -> None:
        names = hidden.parse(self.hidden_field.toPlainText())
        self.hidden_field.setPlainText("\n".join([*names, *(b for b in hidden.COMMON_BOTS if b not in names)]))

    def _use_hidden(self) -> None:
        self.hub.set_hidden(hidden.parse(self.hidden_field.toPlainText()))
        self.refresh()

    def reply_to(self, message: ChatMessage) -> None:
        """Start a reply: the message box begins with @name (sent like any message, in that line's chat)."""
        if not self.say_row.isVisibleTo(self):
            return
        self._say_on = message.platform
        self._show_say()
        start = f"@{message.user} "
        if not self.say.text().startswith(start):
            self.say.setText(start + self.say.text())
        self.say.setFocus()
        self.say.setCursorPosition(len(self.say.text()))

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
        platform = next((p for p, host in (("twitch", "twitch.tv"), ("youtube", "youtu"), ("kick", "kick.com")) if host in lower), None) if is_link else "twitch"
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
            tips.append(f"{title} ({reader.channel}): {words}")
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
        # Kept and made ready ahead (_ready_menu), opened with popup() (no second event loop in the click): made,
        # styled and opened in the click it was 151 ms on the PC while live (2026-11-18).
        self._ready_menu()
        self._menu.popup(self.more.mapToGlobal(self.more.rect().bottomLeft()))

    def _ready_menu(self) -> None:
        """The ⋯ menu as it is now, made again only when what it holds changed."""
        import shiboken6

        kind, _ = self.source()
        key = (bool(self.reading()), kind, bool(QSettings().value(LINK, "")), bool(self._account()),
               self.web is not None, len(self.hub.hidden))
        menu = getattr(self, "_menu", None)
        if menu is not None and shiboken6.isValid(menu) and self._menu_key == key:
            return
        if menu is not None and shiboken6.isValid(menu):
            menu.deleteLater()
        self._menu, self._menu_key = self.build_menu(), key
        self._menu.ensurePolished()
        self._menu.adjustSize()
        # create(), not winId(): winId() marks it native, which makes its parent and the parent's other
        # children native too, and a native parent window warned "must be a top level window" (PC soak 2026-11-18).
        self._menu.create()

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
        count = len(self.hub.hidden)
        menu.addAction(f"Hidden accounts ({count})…" if count else "Hidden accounts…").triggered.connect(self.ask_hidden)
        if not self._account():
            menu.addAction("Sign in…").triggered.connect(self._connect)
        menu.addSeparator()
        if kind == "chat":
            menu.addAction("Clear").triggered.connect(self.view.clear_chat)
        elif kind == "link" and self.web is not None:
            menu.addAction("Reload").triggered.connect(self.web.reload)
        return menu

    def shutdown(self) -> None:
        self.hub.stop()
