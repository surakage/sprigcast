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


class AndroidScreenDialog(CardDialog):
    def __init__(self, settings: dict[str, Any], parent: QWidget | None = None, lister=None):
        super().__init__("Android Screen", parent, width=460)
        self.settings_in = {**android_screen.DEFAULTS, **settings}
        self._lister = lister or android_screen.list_phones
        self._found: list | None = None

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
            self.status.setText("SprigCast needs Android's USB tool (adb) to reach the phone.")
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

    def values(self) -> dict[str, Any]:
        serial = self.phone.currentData() or str(self.settings_in.get("serial") or "")
        return {"serial": serial, "longest": int(self.size.currentData()), "bitrate_kbps": int(self.bitrate.currentData())}
