"""The Chat panel: one place for chat, whatever it comes from.

It shows one of two things, and switching never adds another panel:

* **Twitch chat**, read natively (chat/twitch.py): light, no browser. Connecting Twitch anywhere
  in SprigCast switches the panel to your channel's chat.
* **A chat link**: any web page that shows chat (a pop-out chat, StreamBridge, Streamer.bot,
  a multi-chat overlay), shown in the panel itself.

Pasting a twitch.tv link (or just a channel name) reads that channel natively instead of loading
Twitch's heavy web page.

    ┌ Chat ───────────────── ↗  × ┐
    │ ● Twitch · surakage       ⋯ │
    │ mossy  hi chat!             │
    │ fern   that leaf tho 🌿     │
    └─────────────────────────────┘
"""

from __future__ import annotations

import html
import re
import zlib
from collections import deque

from PySide6.QtCore import QSettings, Qt, QUrl
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
from sprigcast.chat import ChatMessage

KEY = "chat"  # the panel's key in app/panels.py
SOURCE = "chat/source"  # "twitch" | "link" | "" (nothing picked yet)
LINK = "chat/link"
CHANNEL = "chat/twitch_channel"  # a channel picked by hand; empty = your connected account
KEEP = 300  # messages kept on screen
EMOTE_URL = "https://static-cdn.jtvnw.net/emoticons/v2/{id}/default/dark/1.0"
EMOTE_SIZE = 22

# Names with no colour of their own get one of these, always the same one for the same name.
NAME_COLOURS = ("#7fd6a2", "#9cc9ff", "#f2b880", "#d8a6ff", "#f5d76e", "#7fe0d6", "#ff9fb8", "#b9e27a")
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


def readable_colour(colour: str, name: str) -> str:
    """The chatter's colour, lifted if it would vanish on the dark panel; a steady pick if they have none."""
    if not colour:
        return NAME_COLOURS[zlib.crc32(name.lower().encode()) % len(NAME_COLOURS)]
    c = QColor(colour)
    if not c.isValid():
        return NAME_COLOURS[0]
    h, s, lightness, _ = c.getHsl()
    if lightness < 140:
        c.setHsl(max(h, 0), min(s, 200), 175)
    return c.name()


def message_html(message: ChatMessage) -> str:
    """One chat line as rich text: coloured name, then the words with emotes and links."""
    colour = readable_colour(message.color, message.user)
    if message.notice:
        return (f"<p style='margin:6px 0; color:{theme.SELECT_TEXT}; background:{theme.SELECT_BG};'>"
                f"&nbsp;✿ {_body(message)}&nbsp;</p>")
    name = f"<span style='color:{colour}; font-weight:600'>{html.escape(message.user)}</span>"
    marks = ""
    if "broadcaster" in message.badges:
        marks = f"<span style='color:{theme.LIVE_TEXT}'>● </span>"
    elif "moderator" in message.badges:
        marks = f"<span style='color:{theme.ACCENT}'>● </span>"
    elif "vip" in message.badges:
        marks = f"<span style='color:{theme.INFO}'>● </span>"
    body = _body(message)
    if message.action:
        body = f"<span style='color:{colour}'><i>{body}</i></span>"
    return f"<p style='margin:3px 0'>{marks}{name}&nbsp; {body}</p>"


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
    """The native chat list. Emote pictures load once each and are kept for the session."""

    def __init__(self):
        super().__init__()
        self.setOpenExternalLinks(True)
        self.setFrameShape(QTextBrowser.Shape.NoFrame)
        self.setStyleSheet(f"QTextBrowser {{ background: transparent; color: {theme.TEXT}; border: none; }}")
        self.document().setDocumentMargin(2)
        self.document().setMaximumBlockCount(KEEP)  # the oldest line leaves as a new one comes
        self.messages: deque[ChatMessage] = deque(maxlen=KEEP)
        self._net = None
        self._asked: set[str] = set()

    def add(self, message: ChatMessage) -> None:
        bar = self.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        self.messages.append(message)
        self.append(message_html(message))
        if at_bottom:
            bar.setValue(bar.maximum())

    def remove(self, *, message_id: str = "", user_id: str = "", everything: bool = False) -> None:
        """A moderator deleted a message, timed someone out, or cleared chat."""
        before = len(self.messages)
        kept = [] if everything else [
            m for m in self.messages
            if not ((message_id and m.id == message_id) or (user_id and m.user_id == user_id))
        ]
        if len(kept) == before:
            return
        self.messages = deque(kept, maxlen=KEEP)
        self.setHtml("".join(message_html(m) for m in self.messages))
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def clear_chat(self) -> None:
        self.messages.clear()
        self.clear()

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
        reply = self._net.get(QNetworkRequest(QUrl(EMOTE_URL.format(id=emote_id))))
        reply.finished.connect(lambda: self._fetched(reply, url))

    def _fetched(self, reply, url: QUrl) -> None:
        from PySide6.QtGui import QImage

        image = QImage()
        if image.loadFromData(reply.readAll().data()):
            self.document().addResource(QTextDocument.ResourceType.ImageResource, url, image)
            self.document().markContentsDirty(0, self.document().characterCount())
            self.viewport().update()
        reply.deleteLater()


class ChatPanel(QWidget):
    """The Chat panel's body. Nothing connects or loads until the panel is first shown (activate)."""

    def __init__(self, connect_twitch=None):
        super().__init__()
        self.connect_twitch = connect_twitch  # opens Connect Twitch; the panel hears the result itself
        self.reader = None
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
        self.view = ChatView()
        self.web_holder = QWidget()
        QVBoxLayout(self.web_holder).setContentsMargins(0, 0, 0, 0)
        for page in (self.empty, self.link_page, self.view, self.web_holder):
            self.pages.addWidget(page)

        from sprigcast.output import twitch_auth

        twitch_auth.account_events().changed.connect(self._account_changed)
        self._back_to = None

    # Pages ------------------------------------------------------------------------------

    def _empty_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)
        layout.addStretch(1)
        title = QLabel("Chat shows up here")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 15px; font-weight: 600")
        hint = QLabel("Connect Twitch, or paste any chat link.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED}")
        layout.addWidget(title)
        layout.addWidget(hint)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        self.connect_button = QPushButton("Connect Twitch")
        self.connect_button.setStyleSheet(GO_STYLE)
        self.connect_button.clicked.connect(self._connect)
        paste = QPushButton("Paste a link")
        paste.setStyleSheet(CHIP_STYLE)
        paste.clicked.connect(self.ask_link)
        for button in (self.connect_button, paste):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(button)
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
        self.link_field.setPlaceholderText("https://… or a Twitch name")
        self.link_field.setStyleSheet(FIELD_STYLE)
        self.link_field.returnPressed.connect(self._use_link)
        layout.addWidget(self.link_field)
        hint = QLabel("A Twitch link reads chat natively. Any other page shows here as it is.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED}")
        layout.addWidget(hint)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(CHIP_STYLE)
        cancel.clicked.connect(self._cancel_link)
        use = QPushButton("Use")
        use.setStyleSheet(GO_STYLE)
        use.clicked.connect(self._use_link)
        for button in (cancel, use):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(button)
        layout.addLayout(row)
        layout.addStretch(2)
        return page

    # Choosing a source --------------------------------------------------------------------

    def activate(self) -> None:
        """The panel is on screen: start reading (only the first time does anything)."""
        if self.active:
            return
        self.active = True
        self.refresh()

    def ask_link(self) -> None:
        self._back_to = self.pages.currentWidget()
        self.link_field.setText(str(QSettings().value(LINK, "") or ""))
        self.pages.setCurrentWidget(self.link_page)
        self.where.setText("Pick a chat")
        self.link_field.setFocus()
        self.link_field.selectAll()

    def _cancel_link(self) -> None:
        self.refresh()

    def _use_link(self) -> None:
        from sprigcast.app.custom_docks import normalise_url
        from sprigcast.chat.twitch import channel_from_text

        text = self.link_field.text().strip()
        if not text:
            return
        settings = QSettings()
        is_link = "://" in text or "." in text or "/" in text
        channel = channel_from_text(text)
        if channel and (not is_link or "twitch.tv" in text.lower()):
            mine = self._account() or ""
            settings.setValue(CHANNEL, "" if channel == mine else channel)
            settings.setValue(SOURCE, "twitch")
        else:
            settings.setValue(LINK, normalise_url(text))
            settings.setValue(SOURCE, "link")
        self.refresh()

    def use_twitch(self, channel: str = "") -> None:
        settings = QSettings()
        settings.setValue(CHANNEL, channel)
        settings.setValue(SOURCE, "twitch")
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
        """Connecting Twitch (here or in Settings) switches chat to your channel; disconnecting stops it."""
        settings = QSettings()
        if login:
            if settings.value(SOURCE, "") != "twitch" or not settings.value(CHANNEL, ""):
                settings.setValue(CHANNEL, "")
                settings.setValue(SOURCE, "twitch")
        elif settings.value(SOURCE, "") == "twitch" and not settings.value(CHANNEL, ""):
            settings.setValue(SOURCE, "link" if settings.value(LINK, "") else "")
        if self.active:
            self.refresh()

    def _account(self) -> str | None:
        from sprigcast.output import twitch_auth

        login = twitch_auth.remembered_login()
        if login is None:  # connected before SprigCast remembered it: ask the credential store once
            login = twitch_auth.connected_account() or ""
            QSettings().setValue(twitch_auth.LOGIN_SETTING, login)
        return login or None

    def source(self) -> tuple[str, str]:
        """("twitch", channel) | ("link", url) | ("", "")."""
        settings = QSettings()
        kind = str(settings.value(SOURCE, "") or "")
        if kind == "link" and settings.value(LINK, ""):
            return "link", str(settings.value(LINK))
        if kind in ("twitch", ""):
            channel = str(settings.value(CHANNEL, "") or "") or (self._account() or "")
            if channel:
                return "twitch", channel
        if settings.value(LINK, ""):
            return "link", str(settings.value(LINK))
        return "", ""

    def refresh(self) -> None:
        if not self.active:
            return
        kind, value = self.source()
        if kind != "twitch" and self.reader is not None:
            self.reader.stop()
        if kind != "link" and self.web is not None:
            self.web.set_url("about:blank")  # free the page's memory; the browser itself stays for later
        if kind == "twitch":
            self._read_twitch(value)
        elif kind == "link":
            self._show_link(value)
        else:
            self.where.setText("No chat yet")
            self.pages.setCurrentWidget(self.empty)

    def _read_twitch(self, channel: str) -> None:
        from sprigcast.chat.twitch import TwitchChat

        if self.reader is None:
            self.reader = TwitchChat(self)
            self.reader.message.connect(self.view.add)
            self.reader.deleted.connect(lambda mid: self.view.remove(message_id=mid))
            self.reader.cleared.connect(
                lambda uid: self.view.remove(user_id=uid) if uid else self.view.remove(everything=True))
            self.reader.status.connect(self._reader_status)
        if self.reader.channel != channel:
            self.view.clear_chat()
        self.pages.setCurrentWidget(self.view)
        self.reader.start(channel)
        self._reader_status("connecting" if self.reader.socket is not None else "live")

    def _reader_status(self, status: str) -> None:
        if self.reader is None:
            return
        dot = {"live": theme.ACCENT, "connecting": theme.WARN}.get(status, theme.BAD)
        words = "" if status == "live" else ("  connecting…" if status == "connecting" else "  reconnecting…")
        self.where.setText(f"<span style='color:{dot}'>●</span> Twitch · {html.escape(self.reader.channel)}"
                           f"<span style='color:{theme.MUTED}'>{words}</span>")
        if status.startswith("offline"):
            self.where.setToolTip(status.split(":", 1)[-1].strip())

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
        mine = self._account()
        if mine:
            twitch = menu.addAction(f"Twitch · {mine}")
            twitch.setCheckable(True)
            twitch.setChecked(kind == "twitch" and not QSettings().value(CHANNEL, ""))
            twitch.triggered.connect(lambda: self.use_twitch(""))
        else:
            menu.addAction("Connect Twitch…").triggered.connect(self._connect)
        saved = str(QSettings().value(LINK, "") or "")
        if saved:
            link = menu.addAction("Chat link")
            link.setCheckable(True)
            link.setChecked(kind == "link")
            link.triggered.connect(self.use_link)
        menu.addAction("Paste a link…" if not saved else "Change link…").triggered.connect(self.ask_link)
        menu.addSeparator()
        if kind == "twitch":
            menu.addAction("Clear").triggered.connect(self.view.clear_chat)
        elif kind == "link" and self.web is not None:
            menu.addAction("Reload").triggered.connect(self.web.reload)
        return menu

    def shutdown(self) -> None:
        if self.reader is not None:
            self.reader.stop()
