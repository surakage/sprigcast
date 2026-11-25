"""Help → About SprigCast: the sloth, the S logo, the version, the code, the licenses and a tip link.

A rounded pop-up like the rest of the app (transitions_panel.PopUp), instead of a plain system box.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from sprigcast.app.modal import modal
from sprigcast.app import theme
from sprigcast.app.support import SITE_URL, SOURCE_URL, TIP_URL

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

    for text, style in (
        ("One show, every shape: stream and record horizontal and vertical at once.", f"color: {theme.TEXT};"),
        ("Open source under the GPL 3.0 or later.", f"color: {theme.MUTED};"),
    ):
        line = QLabel(text)
        line.setWordWrap(True)
        line.setAlignment(Qt.AlignmentFlag.AlignCenter)
        line.setStyleSheet(style)
        layout.addWidget(line)
    link = QLabel(f"<a href='{SITE_URL}' style='color: {theme.ACCENT}; text-decoration: none;'>"
                  "sprigcast.slothbloom.com</a>")
    link.setObjectName("aboutLink")
    link.setOpenExternalLinks(True)
    link.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(link)
    licenses = QLabel(f"<a href='{licenses_url()}' style='color: {theme.ACCENT}; text-decoration: none;'>Licenses</a>")
    licenses.setObjectName("aboutLicenses")
    licenses.setOpenExternalLinks(True)
    licenses.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(licenses)
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
    modal(dialog)
