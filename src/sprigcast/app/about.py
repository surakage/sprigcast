"""Help → About SprigCast: the welcome sloth, the S logo, the version, where the code lives and a tip link.

A rounded pop-up like the rest of the app (transitions_panel.PopUp), instead of a plain system box.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from sprigcast.app import theme
from sprigcast.app.support import TIP_URL

REPO_URL = "https://github.com/surakage/sprigcast"


def about_page() -> QWidget:
    from sprigcast import __version__
    from sprigcast.app.empty import ArtPicture
    from sprigcast.app.main import brand_mark

    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(24, 4, 24, 4)
    layout.setSpacing(12)
    layout.addWidget(ArtPicture("welcome", 260), 0, Qt.AlignmentFlag.AlignHCenter)

    name_row = QHBoxLayout()
    name_row.setSpacing(10)
    name_row.addStretch(1)
    mark = QLabel()
    mark.setObjectName("aboutMark")
    mark.setPixmap(brand_mark().pixmap(40, 40))
    name_row.addWidget(mark)
    name = QLabel("SprigCast")
    name.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 24px; font-weight: 700;")
    name_row.addWidget(name)
    version = QLabel(__version__)
    version.setObjectName("aboutVersion")
    version.setStyleSheet(f"color: {theme.MUTED}; font-size: 13px; padding-top: 6px;")
    name_row.addWidget(version)
    name_row.addStretch(1)
    layout.addLayout(name_row)

    for text, style in (
        ("One show, every shape: stream and record horizontal and vertical at once.", f"color: {theme.TEXT};"),
        ("Open source under the GPL 3.0 or later.", f"color: {theme.MUTED};"),
    ):
        line = QLabel(text)
        line.setWordWrap(True)
        line.setAlignment(Qt.AlignmentFlag.AlignCenter)
        line.setStyleSheet(style)
        layout.addWidget(line)
    link = QLabel(f"<a href='{REPO_URL}' style='color: {theme.ACCENT}; text-decoration: none;'>"
                  "github.com/surakage/sprigcast</a>")
    link.setObjectName("aboutLink")
    link.setOpenExternalLinks(True)
    link.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(link)
    tip = QLabel(f"<a href='{TIP_URL}' style='color: {theme.ACCENT}; text-decoration: none;'>Support SprigCast</a>")
    tip.setObjectName("aboutTip")
    tip.setOpenExternalLinks(True)
    tip.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(tip)
    page.setMinimumWidth(380)
    return page


def show_about(parent: QWidget | None = None) -> None:
    from sprigcast.app.transitions_panel import PopUp

    dialog = PopUp("About SprigCast", parent)
    dialog.add_tab("about", "About", about_page())
    dialog.exec()
    dialog.deleteLater()
