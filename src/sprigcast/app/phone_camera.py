"""The Phone Camera leaf: a web page leaf (kind "browser", settings["phone"] = its code) that shows a phone's camera
sent over Wi-Fi (app/phone_server.py), and its pop-up with the QR code to scan."""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import theme
from sprigcast.app.card import CardDialog, combo, section

log = logging.getLogger(__name__)

SHAPES = (("Landscape", (1280, 720)), ("Portrait", (720, 1280)))
FOLLOW = "follow"  # settings: the leaf turns with the phone (user 2026-11-26), the default for new leaves
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
            FOLLOW: True, "keep_running": True, "audio_to_mixer": True, "css": "body { background: transparent; margin: 0; }"}


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


def turned(collection) -> list[tuple[Any, tuple[int, int], tuple[int, int]]]:
    """Phone Camera leaves that follow the phone and whose picture now comes the other way up (the phone was
    turned): each one's shape is set to match, and (leaf, old size, new size) is returned so the window turns
    its places (Collection.turn_places) and saves. The page only resizes: the phone stays connected."""
    if _server is None or not _server.running:
        return []
    out = []
    for config in collection.sources:
        if not is_phone(config) or not config.settings.get(FOLLOW, True):
            continue
        video = _server.video_size(config.settings["phone"])
        if video is None or video[0] == video[1]:
            continue
        old = (int(config.settings.get("width", 1280)), int(config.settings.get("height", 720)))
        if (old[0] > old[1]) == (video[0] > video[1]):
            continue
        new = SHAPES[0][1] if video[0] > video[1] else SHAPES[1][1]
        config.settings = {**config.settings, "width": new[0], "height": new[1]}
        out.append((config, old, new))
    return out


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


class _Grabbed(QObject):
    """Carries a picture from the render thread to the window (queued). No parent: a picture that lands after the
    pop-up closed reaches no one."""

    got = Signal(object)


class LiveView(QWidget):
    """What SprigCast shows of the leaf, live (user 2026-11-26: "show the camera view so we know what we're showing
    is what is shown"): its picture as the engine draws it, a few times a second, only while the pop-up is open.
    grab is the Filters window's EngineGrab (Engine.grab_source with the Python engine)."""

    HEIGHT = 230
    EVERY_MS = 200
    LIMIT = 480

    def __init__(self, source_id: str, grab, parent: QWidget | None = None):
        super().__init__(parent)
        self.source_id, self.fetch = source_id, grab
        self.image: QImage | None = None
        self._waiting = False
        self.setFixedHeight(self.HEIGHT)
        self.setAccessibleName("What SprigCast shows")
        self.bridge = _Grabbed()
        self.bridge.got.connect(self._take)
        self.timer = QTimer(self, interval=max(self.EVERY_MS, int(getattr(grab, "every_ms", 0) or 0)))
        self.timer.timeout.connect(self.ask)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self.fetch is not None:
            QTimer.singleShot(150, self, self._start)  # once the pop-up is up (as the Filters window's)

    def _start(self) -> None:
        if self.isVisible():
            self.ask()
            self.timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(event)

    def stop(self) -> None:
        self.timer.stop()
        stop = getattr(self.fetch, "stop", None)
        if stop is not None:
            stop()

    def ask(self) -> None:
        if self._waiting or not self.isVisible() or self.fetch is None:
            return
        bridge = self.bridge

        def back(picture) -> None:  # render thread (or right away with the native engine's views)
            image = None
            if picture is not None and getattr(picture, "ndim", 0) == 3 and picture.shape[0] and picture.shape[1]:
                h, w = picture.shape[:2]
                image = QImage(picture.tobytes(), w, h, w * 4, QImage.Format.Format_RGBA8888_Premultiplied).copy()
            bridge.got.emit(image)

        self._waiting = True
        try:
            self.fetch(self.source_id, back, False, self.LIMIT)
        except Exception:  # noqa: BLE001
            self._waiting = False

    def _take(self, image) -> None:
        self._waiting = False
        if image is not None or self.image is not None:
            self.image = image
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        box = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(box, 14, 14)
        p.setClipPath(clip)
        p.fillRect(box, QColor(theme.CHROME))
        image = self.image
        if image is not None and not image.isNull():
            k = min(box.width() / image.width(), box.height() / image.height())
            w, h = image.width() * k, image.height() * k
            p.drawImage(QRectF(box.center().x() - w / 2, box.center().y() - h / 2, w, h), image)
        else:
            p.setPen(QColor(theme.MUTED))
            p.drawText(box.adjusted(16, 16, -16, -16), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       "No picture yet" if self.fetch is not None else "Shows here while SprigCast runs")
        p.end()


def _pin_text(pin: str) -> str:
    return f"{pin[:3]} {pin[3:]}"


class PhoneDialog(CardDialog):
    """Scan to connect, then the 6-digit code; whether the phone is sending, and what SprigCast shows of it.
    Landscape or portrait sets the leaf's shape."""

    def __init__(self, settings: dict[str, Any], parent: QWidget | None = None, source_id: str = "",
                 grab=None):
        super().__init__("Phone Camera", parent, width=780)
        self.settings_in = dict(settings)
        self.code = str(settings.get("phone", ""))
        srv = server()
        url = srv.phone_url(self.code) if srv.running else ""

        columns = QHBoxLayout()
        columns.setSpacing(14)
        left, right = QVBoxLayout(), QVBoxLayout()
        left.setSpacing(14)
        right.setSpacing(14)
        columns.addLayout(left, 0)
        columns.addLayout(right, 1)
        self.body.addLayout(columns)

        frame, scan = section("Scan with your phone")
        frame.setFixedWidth(330)
        left.addWidget(frame)
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
        self.copy = QPushButton("Copy address")
        self.copy.setStyleSheet(theme.button_style(17, "9px 18px"))
        self.copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy.setAutoDefault(False)
        self.copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(url))
        scan.addWidget(self.copy, 0, Qt.AlignmentFlag.AlignHCenter)

        frame, pair = section("Then enter this code")
        frame.setFixedWidth(330)
        left.addWidget(frame)
        self.pin = QLabel()
        self.pin.setObjectName("phonePin")
        self.pin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pin.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.pin.setAccessibleName("Pairing code")
        font = self.pin.font()
        font.setFamilies(["Cascadia Mono", "Consolas", "DejaVu Sans Mono", "monospace"])
        font.setPixelSize(34)
        font.setWeight(font.Weight.DemiBold)
        font.setLetterSpacing(font.SpacingType.AbsoluteSpacing, 4)
        self.pin.setFont(font)
        self.pin.setStyleSheet(f"QLabel#phonePin {{ color: {theme.TEXT_BRIGHT}; background: {theme.CHROME};"
                               " border-radius: 14px; padding: 12px 18px; }")
        pair.addWidget(self.pin)
        left.addStretch(1)

        frame, show = section("What SprigCast shows")
        right.addWidget(frame)
        self.view = LiveView(str(source_id), grab)
        show.addWidget(self.view)

        frame, state = section("Phone")
        right.addWidget(frame)
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        self.dot = QLabel()
        self.dot.setFixedSize(12, 12)
        self.status = QLabel()
        self.status.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        row.addWidget(self.dot)
        row.addSpacing(8)
        row.addWidget(self.status, 1)
        self.forget = QPushButton("Forget phones")
        self.forget.setStyleSheet(theme.button_style(17, "7px 16px"))
        self.forget.setCursor(Qt.CursorShape.PointingHandCursor)
        self.forget.setAutoDefault(False)
        self.forget.clicked.connect(self._forget)
        row.addWidget(self.forget)
        state.addLayout(row)
        shape_row = QHBoxLayout()
        shape_row.setContentsMargins(2, 0, 2, 0)
        label = QLabel("Shape")
        label.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        self.shape = combo(10)
        self.shape.addItem("Follow the phone", None)
        for name, size in SHAPES:
            self.shape.addItem(name, size)
        portrait = int(settings.get("height", 720)) > int(settings.get("width", 1280))
        self.shape.setCurrentIndex(0 if settings.get(FOLLOW, True) else 2 if portrait else 1)
        self.shape.setFixedWidth(190)
        shape_row.addWidget(label, 1)
        shape_row.addWidget(self.shape)
        state.addLayout(shape_row)
        self.note = QLabel("Use the same Wi-Fi as this PC. The first time, your phone warns that the connection "
                           "isn't private: tap Show details (or Advanced), then visit the page.")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"{theme.NOTE_STYLE} padding: 10px 14px;")
        right.addWidget(self.note)
        right.addStretch(1)

        if url:
            self.qr.setPixmap(qr_picture(url))
            self.address.setText(url)
        else:
            self.qr.hide()
            self.copy.hide()
            self.pin.parentWidget().hide()
            self.address.setText("Connect this PC to your home network (Wi-Fi or a cable) to use a phone.")

        self.add_buttons("Done")
        self._look()
        self._timer = QTimer(self, interval=1000)
        self._timer.timeout.connect(self._look)
        self._timer.start()

    def _look(self) -> None:
        srv = server()
        on = srv.connected(self.code)
        colour = theme.ACCENT if on else theme.MUTED
        self.dot.setStyleSheet(f"background: {colour}; border-radius: 6px;")
        self.status.setText("Connected and sending" if on else "Waiting for your phone")
        self.pin.setText(_pin_text(srv.pin(self.code)))  # a new one after too many wrong tries
        self.forget.setVisible(srv.paired(self.code))

    def _forget(self) -> None:
        server().forget(self.code)
        self._look()

    def done(self, result: int) -> None:
        self._timer.stop()
        self.view.stop()
        super().done(result)

    def values(self) -> dict[str, Any]:
        size = self.shape.currentData()
        if size is None:  # follows the phone: the shape it has now, until the phone turns
            return {FOLLOW: True}
        w, h = size
        return {"width": w, "height": h, FOLLOW: False}
