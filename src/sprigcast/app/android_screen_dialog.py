"""The Android Screen leaf's pop-up: which phone, how big, and how to switch on USB debugging."""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from sprigcast.app import theme
from sprigcast.app.card import CardDialog, combo
from sprigcast.sources import android_screen

BITRATES = ((4000, "4 Mbps"), (8000, "8 Mbps"), (12000, "12 Mbps"), (16000, "16 Mbps"))
HOW_TO = ("Turn on USB debugging once: in Settings, open About phone and tap Build number 7 times. Then in "
          "Developer options, turn on USB debugging. Plug in the phone and tap Allow.")


def _row(label: str, widget: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(2, 0, 2, 0)
    text = QLabel(label)
    text.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
    row.addWidget(text, 1)
    row.addWidget(widget)
    return row


class AdbDownloadCard(CardDialog):
    """Android Screen's one question when the PC has no adb: download Google's from Google? (Cedar: it isn't
    shipped with SprigCast; it's fetched on request from dl.google.com, with Google's NOTICE.txt kept.)"""

    def __init__(self, parent: QWidget | None = None, download=None):
        super().__init__("Google's adb tool", parent, width=440)
        self._download = download or download_adb
        box = self.section()
        text = QLabel("Android Screen needs Google's adb tool (about 7\u00a0MB). Download it from Google?")
        text.setWordWrap(True)
        text.setStyleSheet(f"color: {theme.TEXT}; background: transparent; padding: 2px 4px;")
        box.addWidget(text)
        terms = QLabel(f'<a href="{android_screen.ADB_TERMS_URL}" style="color: {theme.ACCENT};">'
                       "Google's SDK terms</a>")
        terms.setOpenExternalLinks(True)
        terms.setStyleSheet("background: transparent; padding: 2px 4px;")
        box.addWidget(terms)
        self.progress = QLabel()
        self.progress.setStyleSheet(f"color: {theme.MUTED}; background: transparent; padding: 2px 4px;")
        self.progress.hide()
        box.addWidget(self.progress)
        self.go = self.add_buttons("Download")
        self.go.clicked.disconnect()
        self.go.clicked.connect(self.start)
        self._state: tuple | None = None  # set by the download thread: ("progress", done, total) / ("done", e)
        self._cancel = False
        self._poll = QTimer(self, interval=100)
        self._poll.timeout.connect(self._look)

    def start(self) -> None:
        self.go.setEnabled(False)
        self.go.setText("Downloading…")
        self.progress.setText("Starting…")
        self.progress.show()
        self._cancel = False

        def work() -> None:
            try:
                self._download(lambda done, total: setattr(self, "_state", ("progress", done, total)),
                               lambda: self._cancel)
                self._state = ("done", "")
            except Exception as exc:  # noqa: BLE001 (said on the card: no network, Google unreachable...)
                self._state = ("done", str(exc) or type(exc).__name__)

        threading.Thread(target=work, name="adb download", daemon=True).start()
        self._poll.start()

    def _look(self) -> None:
        state = self._state
        if state is None:
            return
        if state[0] == "progress":
            done, total = state[1], state[2]
            self.progress.setText(f"{done / 1e6:.1f} of {total / 1e6:.1f} MB" if total else f"{done / 1e6:.1f} MB")
            return
        self._poll.stop()
        if not state[1]:
            self.accept()
            return
        self.progress.setText("The download didn't finish. Check your internet and try again.")
        self.go.setText("Try again")
        self.go.setEnabled(True)
        self._state = None

    def reject(self) -> None:
        self._cancel = True
        super().reject()


def download_adb(progress=None, cancelled=lambda: False) -> str:
    """Fetch Google's platform-tools zip over HTTPS and keep adb from it (sources/android_screen.install_adb)."""
    import logging
    import tempfile
    from pathlib import Path

    from sprigcast import update

    home = android_screen.ADB_HOME
    if home is None:
        raise OSError("no place for it")
    with tempfile.TemporaryDirectory() as folder:
        zip_path = Path(folder) / "platform-tools.zip"
        update.download(android_screen.ADB_URL, zip_path, progress, cancelled)
        if cancelled():
            raise OSError("canceled")
        adb = android_screen.install_adb(zip_path, home)
    logging.getLogger(__name__).info("android screen: Google's adb downloaded")
    return str(adb)


class AndroidScreenDialog(CardDialog):
    def __init__(self, settings: dict[str, Any], parent: QWidget | None = None, lister=None):
        super().__init__("Android Screen", parent, width=460)
        self.settings_in = {**android_screen.DEFAULTS, **settings}
        self._lister = lister or android_screen.list_phones
        self._found: list | None = None
        self._asked = False  # the download card is offered once per pop-up

        phone = self.section("Phone")
        self.phone = combo(14)
        self.phone.setFixedWidth(230)
        phone.addLayout(_row("Phone", self.phone))
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.MUTED}; background: transparent; padding: 2px 4px;")
        phone.addWidget(self.status)
        self.again = QPushButton("Look again")
        self.again.setStyleSheet(theme.button_style(17, "9px 18px"))
        self.again.setCursor(Qt.CursorShape.PointingHandCursor)
        self.again.setAutoDefault(False)
        self.again.clicked.connect(self.look)
        phone.addWidget(self.again, 0, Qt.AlignmentFlag.AlignLeft)

        picture = self.section("Picture")
        self.size = combo(8)
        for name, longest in android_screen.SIZES:
            self.size.addItem(name, longest)
        self.size.setCurrentIndex(max(0, self.size.findData(int(self.settings_in.get("longest") or 1280))))
        self.size.setFixedWidth(150)
        picture.addLayout(_row("Size", self.size))
        self.bitrate = combo(8)
        for kbps, name in BITRATES:
            self.bitrate.addItem(name, kbps)
        self.bitrate.setCurrentIndex(max(0, self.bitrate.findData(int(self.settings_in.get("bitrate_kbps") or 8000))))
        self.bitrate.setFixedWidth(150)
        picture.addLayout(_row("Quality", self.bitrate))

        self.note = QLabel(HOW_TO)
        self.note.setWordWrap(True)
        self.note.setStyleSheet(f"{theme.NOTE_STYLE} padding: 10px 14px;")
        self.body.addWidget(self.note)
        self.add_buttons("Save")
        self._poll = QTimer(self, interval=150)
        self._poll.timeout.connect(self._fill)
        self.look()

    def look(self) -> None:
        """Ask adb which phones are plugged in, off the window's thread."""
        self.status.setText("Looking for phones…")
        self.again.setEnabled(False)
        self._found = None
        adb = android_screen.find_adb() if self._lister is android_screen.list_phones else "given"

        def work() -> None:
            self._found = self._lister() if adb else ["no adb"]

        threading.Thread(target=work, name="android phones", daemon=True).start()
        self._poll.start()

    def _fill(self) -> None:
        if self._found is None:
            return
        self._poll.stop()
        self.again.setEnabled(True)
        found, self._found = self._found, None
        self.phone.clear()
        if found == ["no adb"]:
            self.status.setText("Android Screen needs Google's adb tool to reach the phone.")
            if not self._asked:
                self._asked = True
                QTimer.singleShot(0, self, self._offer_adb)
            return
        ready = [p for p in found if p["state"] == "device"]
        for p in ready:
            self.phone.addItem(p["model"] or p["serial"], p["serial"])
        want = str(self.settings_in.get("serial") or "")
        if want and self.phone.findData(want) >= 0:
            self.phone.setCurrentIndex(self.phone.findData(want))
        if ready:
            self.status.setText(f"{len(ready)} phone{'s' if len(ready) > 1 else ''} ready")
        elif any(p["state"] == "unauthorized" for p in found):
            self.status.setText("Tap Allow on your phone, then Look again.")
        else:
            self.status.setText("No phone found. Plug it in with USB debugging on, then Look again.")

    def _offer_adb(self) -> None:
        from sprigcast.app.modal import modal

        card = AdbDownloadCard(self)
        if modal(card):
            self.look()

    def values(self) -> dict[str, Any]:
        serial = self.phone.currentData() or str(self.settings_in.get("serial") or "")
        return {"serial": serial, "longest": int(self.size.currentData()), "bitrate_kbps": int(self.bitrate.currentData())}
