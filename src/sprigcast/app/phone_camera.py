"""The Phone Camera leaf: a web page leaf (kind "browser", settings["phone"] = its code) that shows a phone's camera
sent over Wi-Fi (app/phone_server.py), and its pop-up with the QR code to scan."""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from sprigcast.app import theme
from sprigcast.app.card import CardDialog, combo

log = logging.getLogger(__name__)

SHAPES = (("Landscape", (1280, 720)), ("Portrait", (720, 1280)))
_server = None


def is_phone(config) -> bool:
    return config.kind == "browser" and bool((config.settings or {}).get("phone"))


def server():
    """The one server (made the first time a Phone Camera leaf needs it)."""
    global _server
    if _server is None:
        from pathlib import Path

        from PySide6.QtCore import QStandardPaths

        from sprigcast.app.phone_server import PhoneServer

        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        _server = PhoneServer(Path(base) / "phone")
    return _server


def new_settings(vertical: bool = False) -> dict[str, Any]:
    """A new Phone Camera leaf's settings: a page of its own (the view on this PC), its sound to the mixer, kept
    running while hidden (the phone stays connected between blooms)."""
    from sprigcast.app.phone_server import VIEW_PORT, new_code
    from sprigcast.sources.browser import DEFAULTS

    code = new_code()
    w, h = SHAPES[1 if vertical else 0][1]
    return {**DEFAULTS, "phone": code, "url": f"http://127.0.0.1:{VIEW_PORT}/view/{code}", "width": w, "height": h,
            "keep_running": True, "audio_to_mixer": True, "css": "body { background: transparent; margin: 0; }"}


def sync(collection) -> bool:
    """Run the server while the garden has a Phone Camera leaf, and point each leaf at the port it got. True when
    a leaf's address changed (the caller saves)."""
    phones = [c for c in collection.sources if is_phone(c)]
    if not phones and _server is None:
        return False
    srv = server()
    try:
        srv.use(c.settings["phone"] for c in phones)
    except OSError as exc:
        log.warning("phone camera: couldn't start its page: %s", exc)
        return False
    changed = False
    for config in phones:
        url = srv.view_url(config.settings["phone"])
        if config.settings.get("url") != url:
            config.settings = {**config.settings, "url": url}
            changed = True
    return changed


def qr_picture(text: str, size: int = 220) -> QPixmap:
    """The QR code, dark on white with its quiet zone, sharp at any screen scale."""
    from sprigcast.core.qr import encode

    modules = encode(text)
    n = len(modules) + 8
    ratio = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    pix = QPixmap(round(size * ratio), round(size * ratio))
    pix.setDevicePixelRatio(ratio)
    pix.fill(QColor("#ffffff"))
    p = QPainter(pix)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#000000"))
    cell = size / n
    for y, row in enumerate(modules):
        for x, dark in enumerate(row):
            if dark:
                p.drawRect(QRectF((x + 4) * cell, (y + 4) * cell, cell + 0.02, cell + 0.02))
    p.end()
    return pix


class PhoneDialog(CardDialog):
    """Scan to connect, and whether the phone is sending. Landscape or portrait sets the leaf's shape."""

    def __init__(self, settings: dict[str, Any], parent: QWidget | None = None):
        super().__init__("Phone Camera", parent, width=440)
        self.settings_in = dict(settings)
        self.code = str(settings.get("phone", ""))
        srv = server()
        url = srv.phone_url(self.code) if srv.running else ""

        scan = self.section("Scan with your phone")
        holder = QHBoxLayout()
        holder.addStretch(1)
        self.qr = QLabel()
        self.qr.setObjectName("phoneQr")
        self.qr.setStyleSheet("QLabel#phoneQr { background: #ffffff; border-radius: 16px; padding: 12px; }")
        holder.addWidget(self.qr)
        holder.addStretch(1)
        scan.addLayout(holder)
        self.address = QLabel()
        self.address.setWordWrap(True)
        self.address.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.address.setStyleSheet(f"color: {theme.MUTED}; background: transparent; padding: 2px 8px;")
        scan.addWidget(self.address)
        self.note = QLabel("Use the same Wi-Fi as this PC. The first time, your phone warns that the connection "
                           "isn't private: tap Show details (or Advanced), then visit the page.")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"{theme.NOTE_STYLE} padding: 10px 14px;")
        scan.addWidget(self.note)
        self.copy = QPushButton("Copy address")
        self.copy.setStyleSheet(theme.button_style(17, "9px 18px"))
        self.copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy.setAutoDefault(False)
        self.copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(url))
        scan.addWidget(self.copy, 0, Qt.AlignmentFlag.AlignLeft)
        if url:
            self.qr.setPixmap(qr_picture(url))
            self.address.setText(url)
        else:
            self.qr.hide()
            self.copy.hide()
            self.address.setText("Connect this PC to your home network (Wi-Fi or a cable) to use a phone.")

        state = self.section("Phone")
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        self.dot = QLabel()
        self.dot.setFixedSize(12, 12)
        self.status = QLabel()
        self.status.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        row.addWidget(self.dot)
        row.addSpacing(8)
        row.addWidget(self.status, 1)
        state.addLayout(row)
        shape_row = QHBoxLayout()
        shape_row.setContentsMargins(2, 0, 2, 0)
        label = QLabel("Shape")
        label.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        self.shape = combo(10)
        for name, size in SHAPES:
            self.shape.addItem(name, size)
        portrait = int(settings.get("height", 720)) > int(settings.get("width", 1280))
        self.shape.setCurrentIndex(1 if portrait else 0)
        self.shape.setFixedWidth(150)
        shape_row.addWidget(label, 1)
        shape_row.addWidget(self.shape)
        state.addLayout(shape_row)

        self.add_buttons("Done")
        self._look()
        self._timer = QTimer(self, interval=1000)
        self._timer.timeout.connect(self._look)
        self._timer.start()

    def _look(self) -> None:
        on = server().connected(self.code)
        colour = theme.ACCENT if on else theme.MUTED
        self.dot.setStyleSheet(f"background: {colour}; border-radius: 6px;")
        self.status.setText("Connected and sending" if on else "Waiting for your phone")

    def values(self) -> dict[str, Any]:
        w, h = self.shape.currentData()
        return {"width": w, "height": h}
