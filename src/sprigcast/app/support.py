"""Help → Support SprigCast: what the project is, why a tip helps, and a button to the tip page.

A rounded pop-up like About (transitions_panel.PopUp). Close leaves it; the green button opens the tip page,
where people can also give to the two charities named here.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import theme

TIP_URL = "https://slothbloom.com/tip"

ABOUT = ("SprigCast is a free, open-source streaming app made to be light: horizontal and vertical "
         "at once, without slowing down your games.")
WHY = ("It's made by one person, in the open, with no ads and no paywalls. Tips pay for tools, testing "
       "and the time to keep it growing, and a portion goes to the causes below.")
# (name, what it does), worded as on slothbloom.com/tip, where people can give to them directly.
CHARITIES: list[tuple[str, str]] = [
    ("Plant a tree", "rainforest reforestation and environmental restoration"),
    ("Sloth Conservation", "sloth rescue, habitat protection and conservation"),
]


def support_page() -> QWidget:
    from sprigcast.app.empty import ArtPicture

    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(24, 4, 24, 4)
    layout.setSpacing(12)
    layout.addWidget(ArtPicture("happy", 200), 0, Qt.AlignmentFlag.AlignHCenter)

    def line(text: str, style: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(style)
        layout.addWidget(label)
        return label

    line(ABOUT, f"color: {theme.TEXT};")
    line(WHY, f"color: {theme.MUTED};")
    if CHARITIES:
        line("On the tip page you can also give to:", f"color: {theme.MUTED};")
        for name, what in CHARITIES:
            line(f"<b style='color: {theme.TEXT_BRIGHT}'>{name}</b>  ·  {what}", f"color: {theme.MUTED};")
    page.setFixedWidth(440)
    # The pop-up's page stack sizes from sizeHint, which ignores how tall the wrapped lines get.
    page.setMinimumHeight(layout.totalHeightForWidth(440))
    return page


def support_dialog(parent: QWidget | None = None, open_url=None):
    """The pop-up, with Close and Go to tip page (call .exec() on it)."""
    from sprigcast.app.transitions_panel import PopUp

    open_url = open_url or (lambda url: QDesktopServices.openUrl(QUrl(url)))
    dialog = PopUp("Support SprigCast", parent)
    dialog.add_tab("support", "Support", support_page())
    dialog.done_button.hide()
    row = QHBoxLayout()
    row.setSpacing(10)
    row.addStretch(1)
    close = QPushButton("Close")
    close.setObjectName("supportClose")
    close.clicked.connect(dialog.reject)
    row.addWidget(close)
    go = QPushButton("Go to tip page")
    go.setObjectName("supportGo")
    go.setStyleSheet(theme.PRIMARY_STYLE)
    go.setCursor(Qt.CursorShape.PointingHandCursor)
    go.clicked.connect(lambda: (open_url(TIP_URL), dialog.accept()))
    row.addWidget(go)
    dialog.layout().itemAt(0).widget().layout().addLayout(row)
    dialog.close_button, dialog.go_button = close, go
    return dialog
