"""A chatter's card: click a name in the Chat panel.

Their platform and badges, how old their account is (Twitch and YouTube), their lines this stream, and what you
can do: Reply, Hide, Timeout 10 minutes and Ban (Ban asks "Sure?" once). The actions are the same as a line's
right-click menu and hover buttons; the panel carries them out.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from sprigcast.app import card, platform_logos, theme
from sprigcast.chat import PLATFORMS, ChatMessage

RECENT = 5  # their newest lines shown
BADGE_WORDS = {"broadcaster": "Streamer", "moderator": "Moderator", "vip": "VIP", "subscriber": "Subscriber",
               "member": "Member", "og": "OG", "verified": "Verified", "first": "First message"}


class UserCard(card.CardDialog):
    replied = Signal(object)  # the ChatMessage
    hidden = Signal(str)  # the name
    moderated = Signal(object, str, int)  # the ChatMessage, "timeout" / "ban", seconds
    _aged = Signal(str)  # from the worker: the account's age in words, or ""

    def __init__(self, message: ChatMessage, lines: list[ChatMessage], powers, can_reply: bool,
                 parent: QWidget | None = None):
        super().__init__(message.user, parent, width=400)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.message = message
        self.heading.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; background: transparent;")

        top = QHBoxLayout()
        top.setSpacing(10)
        logo = QLabel()
        logo.setFixedSize(24, 24)
        logo.setPixmap(platform_logos.tile(message.platform, 24, ratio=max(2.0, self.devicePixelRatioF())))
        logo.setStyleSheet("background: transparent;")
        top.addWidget(logo)
        words = [BADGE_WORDS[b] for b in message.badges if b in BADGE_WORDS]
        where = QLabel(" · ".join([PLATFORMS.get(message.platform, (message.platform.title(),))[0], *words]))
        where.setStyleSheet(f"color: {theme.LABEL}; background: transparent;")
        top.addWidget(where, 1)
        self.body.addLayout(top)

        about = self.section("About")
        self.age = QLabel("Account made: …")
        self.age.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        self.age.setVisible(message.platform in ("twitch", "youtube"))
        about.addWidget(self.age)
        count = len(lines)
        said = QLabel(f"This stream: {count} message{'s' if count != 1 else ''}")
        said.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        about.addWidget(said)

        if lines:
            recent = self.section("Recent messages", spacing=8)
            for line in lines[-RECENT:]:
                text = QLabel(line.text)
                text.setWordWrap(True)
                text.setTextFormat(Qt.TextFormat.PlainText)
                text.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
                recent.addWidget(text)

        self.buttons: dict[str, QPushButton] = {}
        if "timeout" in powers or "ban" in powers:  # moderating: its own section card
            mod = QHBoxLayout()
            mod.setSpacing(8)
            for key, label in (("timeout", "Timeout 10 min"), ("ban", "Ban")):
                if key in powers:
                    mod.addWidget(self._button(key, label))
            mod.addStretch(1)
            self.section("Moderate").addLayout(mod)
        row = QHBoxLayout()
        row.setSpacing(8)
        if can_reply:
            row.addWidget(self._button("reply", "Reply"))
        row.addWidget(self._button("hide", "Hide"))
        row.addStretch(1)
        close = QPushButton("Close")
        close.setStyleSheet(theme.button_style(15, "7px 16px"))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.close)
        row.addWidget(close)
        self.body.addSpacing(2)
        self.body.addLayout(row)

        self._sure = QTimer(self)
        self._sure.setSingleShot(True)
        self._sure.setInterval(3000)
        self._sure.timeout.connect(self._unsure)
        self._aged.connect(self._show_age)
        if self.age.isVisible():
            self._read_age()

    def _button(self, key: str, label: str) -> QPushButton:
        button = QPushButton(label)
        button.setStyleSheet(theme.button_style(15, "7px 16px"))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _=False, k=key: self._press(k))
        self.buttons[key] = button
        return button

    def _read_age(self) -> None:
        from sprigcast.chat import moderation
        from sprigcast.core import workers

        platform, user_id = self.message.platform, self.message.user_id

        def work():
            words = moderation.age_words(moderation.account_made(platform, user_id))
            try:
                self._aged.emit(words)
            except RuntimeError:  # the card was closed meanwhile
                pass

        workers.run(work, name="chat-user-card")

    def _show_age(self, words: str) -> None:
        if words:
            self.age.setText(f"Account made: {words}")
        else:
            self.age.hide()

    def _press(self, key: str) -> None:
        if key == "ban" and not self._sure.isActive():
            self.buttons["ban"].setText("Sure? Ban")
            self.buttons["ban"].setStyleSheet(theme.button_style(15, "7px 16px") + (
                f"QPushButton {{ background: {theme.BAD}; color: #ffffff; border-color: {theme.BAD}; }}"))
            self._sure.start()
            return
        if key == "reply":
            self.replied.emit(self.message)
        elif key == "hide":
            self.hidden.emit(self.message.user)
        else:
            self.moderated.emit(self.message, key, 600 if key == "timeout" else 0)
        self.close()

    def _unsure(self) -> None:
        if "ban" in self.buttons:
            self.buttons["ban"].setText("Ban")
            self.buttons["ban"].setStyleSheet(theme.button_style(15, "7px 16px"))
