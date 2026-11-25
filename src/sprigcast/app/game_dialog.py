"""Game capture's settings: one drop-down for what to show, then the game's sound and pointer.

    Game capture
    Capture   [ ✦ Auto: any full-screen game     ▾ ]
                ✦ Auto: any full-screen game
                ───────────────
                🎮 Elden Ring          (open now, with its icon)
                🌐 Firefox
                ───────────────
                ⌨ Game in front, on a hotkey
    Game sound                                    (●)
    Mouse pointer                                 ( )
                     [ Cancel ] [ Save ]

The list of windows is read again every time the drop-down opens, so a game started after the card
opened still shows up. SprigCast captures through Windows itself and never hooks into a game.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QFileInfo, QSize, Qt
from PySide6.QtWidgets import QFileIconProvider, QHBoxLayout, QLabel, QWidget

from sprigcast.app import theme
from sprigcast.app.card import CardDialog
from sprigcast.app.window_list import WindowCombo, add_window

AUTO, HOTKEY = "Auto: any full-screen game", "Game in front, on a hotkey"
OFF = {"see_through": False, "fps_limit": 0, "border": False}  # the old "More" options, back to their defaults


def _row(label: str, widget: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(2, 0, 2, 0)
    text = QLabel(label)
    text.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
    row.addWidget(text, 1)
    row.addWidget(widget)
    return row


class GameCaptureDialog(CardDialog):
    """windows: the open windows (win32_enum.WindowInfo); has_sound: the capture brings its own sound now.
    list_windows: called again whenever the drop-down opens (None keeps the first list)."""

    def __init__(self, settings: dict[str, Any], windows: list, has_sound: bool, parent: QWidget | None = None,
                 list_windows: Callable[[], list] | None = None):
        super().__init__("Game capture", parent, width=480)
        from sprigcast.app.live_stage import Switch
        from sprigcast.app.starter import BOX_HEIGHT, combo_style

        self._list_windows = list_windows
        self._icons = QFileIconProvider()
        self.was_mode = str(settings.get("mode", "any"))
        pick = self.section("Capture")
        self.capture = WindowCombo(self._reload)
        self.capture.setStyleSheet(combo_style())
        self.capture.setFixedHeight(BOX_HEIGHT)
        self.capture.setIconSize(QSize(18, 18))
        self.capture.setCursor(Qt.CursorShape.PointingHandCursor)
        pick.addWidget(self.capture)
        self.hotkey_note = QLabel("Set its key in Tools → Hotkeys.")
        self.hotkey_note.setStyleSheet(theme.MUTED_STYLE + "; font-size: 12px;")
        pick.addWidget(self.hotkey_note)
        self._fill(list(windows), (self.was_mode, str(settings.get("title", "")), str(settings.get("process", ""))))

        self.sound = Switch()
        self.sound.setChecked(has_sound)
        options = self.section("Options")
        options.addLayout(_row("Game sound", self.sound))
        self.cursor = Switch()
        self.cursor.setChecked(bool(settings.get("cursor", False)))
        options.addLayout(_row("Mouse pointer", self.cursor))
        self.ok = self.add_buttons("Save")
        self.capture.currentIndexChanged.connect(lambda *_: self._mode_changed())
        self._mode_changed()

    def _icon(self, w):
        path = getattr(w, "path", "")
        from sprigcast.app.icons import icon

        return self._icons.icon(QFileInfo(path)) if path else icon("panel", theme.MUTED)

    def _fill(self, windows: list, pick: tuple[str, str, str]) -> None:
        """Auto, the open windows A to Z, then With a hotkey. pick: (mode, title, process) to select."""
        from sprigcast.app.icons import icon
        from sprigcast.app.names import window_app, window_detail, window_name

        mode, title, process = pick
        self.windows = sorted(windows, key=lambda w: (window_app(w.title, w.process, getattr(w, "app", "")).lower(),
                                                      w.title.lower()))
        box = self.capture
        box.blockSignals(True)
        box.clear()
        box.addItem(icon("gamepad", theme.ACCENT), AUTO, ("any", "", ""))
        box.insertSeparator(box.count())
        for w in self.windows:
            app = window_app(w.title, w.process, getattr(w, "app", ""))
            add_window(box, self._icon(w), app, window_detail(w.title, app, w.process), ("window", w.title, w.process))
        if mode == "window" and (title or process) and not any(
                process and w.process.lower() == process.lower() for w in self.windows):
            app = window_name(title, process)
            add_window(box, icon("panel", theme.MUTED), app, window_detail(title, app, process),
                       ("window", title, process))  # not open right now: keep it
        box.insertSeparator(box.count())
        box.addItem(icon("keyboard", theme.MUTED), HOTKEY, ("hotkey", "", ""))
        at = 0
        for i in range(box.count()):
            data = box.itemData(i)
            if not data or data[0] != mode:
                continue
            if mode != "window" or (process and data[2].lower() == process.lower()) or (
                    not process and data[1] == title):
                at = i
                break
        box.setCurrentIndex(at)
        box.blockSignals(False)

    def _reload(self) -> None:
        if self._list_windows is None:
            return
        data = self.capture.currentData() or ("any", "", "")
        self._fill(list(self._list_windows()), data)

    def mode(self) -> str:
        data = self.capture.currentData()
        return str(data[0]) if data else "any"

    def _mode_changed(self) -> None:
        self.hotkey_note.setVisible(self.mode() == "hotkey")

    def values(self) -> tuple[dict[str, Any], bool]:
        """(settings changes, game sound on)."""
        mode, title, process = self.capture.currentData() or ("any", "", "")
        out: dict[str, Any] = {"mode": mode, "cursor": self.cursor.isChecked(), **OFF}
        if mode == "window":
            out["title"], out["process"] = title, process
        elif mode == "any" or self.was_mode != "hotkey":
            out["title"], out["process"] = "", ""  # a hotkey capture starts empty until its key is pressed
        return out, self.sound.isChecked()

