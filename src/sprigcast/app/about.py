"""Help → About SprigCast: the sloth, the S logo, the version, the code, the licenses and a tip link.

A rounded pop-up like the rest of the app (transitions_panel.PopUp), instead of a plain system box.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app.modal import modal
from sprigcast.app import theme
from sprigcast.app.support import SITE_URL, SOURCE_URL, TIP_URL

PILL_H = 34  # the About buttons: fully rounded ends
NOTICES = "THIRD-PARTY-NOTICES.txt"  # written next to python.exe by packaging/notices.py


def notices_path() -> Path | None:
    """The installed copy's list of bundled software and their licenses; None when running from source."""
    path = Path(sys.base_prefix) / NOTICES
    return path if path.is_file() else None


def licenses_url() -> str:
    path = notices_path()
    return QUrl.fromLocalFile(str(path)).toString() if path else SOURCE_URL


def about_page() -> QWidget:
    from sprigcast import build_label, display_version
    from sprigcast.app.empty import ArtPicture
    from sprigcast.app.main import brand_mark

    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(24, 4, 24, 4)
    layout.setSpacing(8)
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
    version = QLabel(display_version())
    version.setObjectName("aboutVersion")
    version.setStyleSheet(f"color: {theme.MUTED}; font-size: 13px; padding-top: 6px;")
    name_row.addWidget(version)
    name_row.addStretch(1)
    layout.addLayout(name_row)
    label = build_label()
    if label != display_version():  # a test build: its run number and commit, for reports
        build = QLabel(label.removeprefix(display_version()).strip().capitalize())
        build.setObjectName("aboutBuild")
        build.setAlignment(Qt.AlignmentFlag.AlignCenter)
        build.setStyleSheet(f"color: {theme.MUTED};")
        layout.addWidget(build)

    for name_, text, style in (
        ("aboutTagline", "One show, every shape: stream and record horizontal and vertical at once.", f"color: {theme.TEXT};"),
        ("aboutLicense", "Open source under the GPL 3.0 or later.", f"color: {theme.MUTED};"),
    ):
        line = QLabel(text)
        line.setObjectName(name_)
        line.setWordWrap(True)
        line.setAlignment(Qt.AlignmentFlag.AlignCenter)
        line.setStyleSheet(style)
        layout.addWidget(line)

    def link(name_: str, url: str, text: str) -> QPushButton:
        button = QPushButton(text)  # a rounded SprigCast button that opens the page
        button.setObjectName(name_)
        button.setToolTip(url)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedHeight(PILL_H)  # Qt draws a radius over half the height square, so pin the height
        button.setStyleSheet(theme.button_style(radius=PILL_H // 2, padding="0 18px"))
        button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        return button

    links = QHBoxLayout()  # one row, evenly spaced, instead of three lines with gaps between them
    links.setContentsMargins(0, 4, 0, 0)
    links.setSpacing(10)
    links.addStretch(1)
    links.addWidget(link("aboutLink", SITE_URL, "Website"))
    links.addWidget(link("aboutLicenses", licenses_url(), "Licenses"))
    links.addWidget(link("aboutTip", TIP_URL, "Support SprigCast"))
    links.addStretch(1)
    layout.addLayout(links)
    layout.addStretch(1)  # any spare height goes below, not between the lines
    page.setMinimumWidth(380)
    return page


def show_about(parent: QWidget | None = None) -> None:
    from sprigcast.app.transitions_panel import PopUp

    dialog = PopUp("About SprigCast", parent)
    dialog.add_tab("about", "About", about_page())
    layout = dialog.done_button.parentWidget().layout()
    layout.setAlignment(dialog.done_button, Qt.AlignmentFlag.AlignHCenter)  # centered under the centered page
    dialog.done_button.setFixedHeight(PILL_H)
    dialog.done_button.setStyleSheet(theme.PRIMARY_STYLE + f"QPushButton {{ border-radius: {PILL_H // 2}px; padding: 0 40px; }}")
    modal(dialog)
