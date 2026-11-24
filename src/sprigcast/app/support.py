"""Help → Support SprigCast: a thank-you from the maker, then three ways to give, each its own choice.

Support me goes to the tip page on slothbloom.com; the two causes go straight to their own donation pages.
A rounded pop-up like About (transitions_panel.PopUp), with Close at the bottom.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import icons, theme

TIP_URL = "https://slothbloom.com/tip"
DISCORD_URL = "https://discord.gg/fgMD33XrhM"  # the SprigCast server: #help, #bug-reports, #feature-ideas

HEADING = "Thank you for being here"
MESSAGE = (
    "I started SprigCast because I wanted a streaming app that stays out of the way: light enough to leave "
    "room for your game, simple enough to just go live, and free for everyone. It's built in the open, "
    "with no ads and no paywalls, and it always will be."
)
INVITE = "If SprigCast has made your streams a little easier, you can give back in whichever way feels right."
THANKS = "Using SprigCast and telling a friend about it helps too. Thank you."


@dataclass(frozen=True)
class Way:
    key: str
    icon: str
    title: str
    text: str
    button: str
    url: str


WAYS = (
    Way("me", "heart", "Support me", "Keeps SprigCast growing: new features, fixes and the time to make them.",
        "Tip", TIP_URL),
    Way("trees", "tree", "Plant a tree", "Rainforest reforestation with One Tree Planted.",
        "Donate", "https://onetreeplanted.org/products/plant-trees"),
    Way("sloths", "paw", "Sloth Conservation", "Sloth rescue and habitat protection with the Sloth "
        "Conservation Foundation.", "Donate", "https://www.slothconservation.org/donate"),
)

CARD_STYLE = (f"QFrame#wayCard {{ background: {theme.CARD}; border: 1px solid {theme.EDGE};"
              " border-radius: 18px; }")


def _label(text: str, style: str, centre: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(style)
    if centre:
        label.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
    return label


def _way_card(way: Way, open_url) -> QFrame:
    card = QFrame()
    card.setObjectName("wayCard")
    card.setStyleSheet(CARD_STYLE)
    box = QVBoxLayout(card)
    box.setContentsMargins(16, 18, 16, 16)
    box.setSpacing(8)
    mark = QLabel()
    mark.setPixmap(icons.pixmap(way.icon, theme.ACCENT, 26))
    mark.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    box.addWidget(mark)
    box.addWidget(_label(way.title, f"color: {theme.TEXT_BRIGHT}; font-weight: 700; font-size: 14px;", True))
    box.addWidget(_label(way.text, f"color: {theme.MUTED}; font-size: 12px;", True), 1)
    button = QPushButton(way.button)
    button.setObjectName(f"way_{way.key}")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if way.key == "me":
        button.setStyleSheet(theme.PRIMARY_STYLE)
    button.clicked.connect(lambda: open_url(way.url))
    box.addWidget(button)
    card.setFixedWidth(184)
    return card


def support_page(open_url) -> QWidget:
    from sprigcast.app.empty import ArtPicture

    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(8, 0, 8, 4)
    layout.setSpacing(14)

    top = QHBoxLayout()
    top.setSpacing(18)
    top.addWidget(ArtPicture("happy", 120), 0, Qt.AlignmentFlag.AlignTop)
    words = QVBoxLayout()
    words.setSpacing(8)
    words.addWidget(_label(HEADING, f"color: {theme.TEXT_BRIGHT}; font-size: 18px; font-weight: 700;"))
    words.addWidget(_label(MESSAGE, f"color: {theme.TEXT};"))
    top.addLayout(words, 1)
    layout.addLayout(top)
    layout.addWidget(_label(INVITE, f"color: {theme.MUTED};"))

    cards = QHBoxLayout()
    cards.setSpacing(12)
    for way in WAYS:
        cards.addWidget(_way_card(way, open_url))
    layout.addLayout(cards)
    layout.addWidget(_label(THANKS, f"color: {theme.MUTED}; font-size: 12px;", True))

    width = 184 * len(WAYS) + 12 * (len(WAYS) - 1) + 16
    page.setFixedWidth(width)
    # The pop-up's page stack sizes from sizeHint, which ignores how tall the wrapped lines get.
    page.setMinimumHeight(layout.totalHeightForWidth(width))
    return page


def support_dialog(parent: QWidget | None = None, open_url=None):
    """The pop-up: the message, three ways to give, and Close (call .exec() on it)."""
    from sprigcast.app.transitions_panel import PopUp

    open_url = open_url or (lambda url: QDesktopServices.openUrl(QUrl(url)))
    dialog = PopUp("Support SprigCast", parent)
    dialog.add_tab("support", "Support", support_page(open_url))
    dialog.done_button.setText("Close")
    dialog.done_button.setStyleSheet("")
    dialog.close_button = dialog.done_button
    dialog.way_buttons = {w.key: dialog.findChild(QPushButton, f"way_{w.key}") for w in WAYS}
    return dialog
