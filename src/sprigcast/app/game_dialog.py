"""Game capture's settings: what to show, the game's sound and pointer, and a few rarer ones under More.

    Game capture
    Show       ( Any full-screen game | One window | With a hotkey )
    Window     [ Elden Ring (eldenring.exe) ▾ ]          (One window only)
    Game sound                                    (●)
    Mouse pointer                                 ( )
    ▸ More
        See-through                               ( )
        Frame rate   [ Up to 60 a second ▾ ]
        Windows' yellow border                    ( )
                     [ Cancel ] [ Save ]

SprigCast captures through Windows itself and never hooks into a game, so anti-cheat has nothing to
object to and there is no hook speed or overlay setting to get wrong.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QButtonGroup, QComboBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import theme
from sprigcast.app.card import CardDialog, field_label

MODES = (("any", "Any full-screen game"), ("window", "One window"), ("hotkey", "With a hotkey"))
FPS_LIMITS = ((0, "Every frame"), (30, "Up to 30 a second"), (60, "Up to 60 a second"), (120, "Up to 120 a second"))
SEGMENT_STYLE = (
    f"QPushButton {{ background: {theme.CARD}; color: {theme.TEXT}; border: 1px solid {theme.EDGE_STRONG};"
    " border-radius: 15px; padding: 6px 12px; font-weight: 600; }"
    f"QPushButton:hover {{ background: {theme.RAISED}; }}"
    f"QPushButton:checked {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; border-color: {theme.ACCENT}; }}"
)


def _row(label: str, widget: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(2, 0, 2, 0)
    text = QLabel(label)
    text.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
    row.addWidget(text, 1)
    row.addWidget(widget)
    return row


class GameCaptureDialog(CardDialog):
    """windows: the open windows (win32_enum.WindowInfo); has_sound: the capture brings its own sound now."""

    def __init__(self, settings: dict[str, Any], windows: list, has_sound: bool, parent: QWidget | None = None):
        super().__init__("Game capture", parent, width=500)
        from sprigcast.app.live_stage import Switch
        from sprigcast.app.starter import BOX_HEIGHT, combo_style

        self.windows = list(windows)
        self.was_mode = str(settings.get("mode", "any"))
        self.body.addWidget(field_label("Show"))
        modes = QHBoxLayout()
        modes.setSpacing(6)
        self.mode_group = QButtonGroup(self)
        current = str(settings.get("mode", "any"))
        for key, text in MODES:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setStyleSheet(SEGMENT_STYLE)
            button.setProperty("mode", key)
            button.setChecked(key == current)
            self.mode_group.addButton(button)
            modes.addWidget(button)
        modes.addStretch(1)
        self.body.addLayout(modes)

        self.window_box = QWidget()
        box = QVBoxLayout(self.window_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        box.addWidget(field_label("Window"))
        self.window = QComboBox()
        self.window.setStyleSheet(combo_style())
        self.window.setFixedHeight(BOX_HEIGHT)
        self.window.setCursor(Qt.CursorShape.PointingHandCursor)
        from sprigcast.app.names import window_name

        title, process = str(settings.get("title", "")), str(settings.get("process", ""))
        for w in self.windows:
            self.window.addItem(window_name(w.title, w.process), (w.title, w.process))
        known = next((i for i, w in enumerate(self.windows) if process and w.process.lower() == process.lower()), -1)
        if known < 0 and (title or process):  # not open right now: keep it
            self.window.insertItem(0, window_name(title, process), (title, process))
            known = 0
        self.window.setCurrentIndex(max(0, known))
        box.addWidget(self.window)
        self.body.addWidget(self.window_box)
        self.hotkey_note = QLabel("Pick its key in Settings → Hotkeys, then press it while the game is in front.")
        self.hotkey_note.setWordWrap(True)
        self.hotkey_note.setStyleSheet(theme.MUTED_STYLE + "; font-size: 12px;")
        self.body.addWidget(self.hotkey_note)

        self.sound = Switch()
        self.sound.setChecked(has_sound)
        self.body.addLayout(_row("Game sound", self.sound))
        self.cursor = Switch()
        self.cursor.setChecked(bool(settings.get("cursor", False)))
        self.body.addLayout(_row("Mouse pointer", self.cursor))

        self.more_button = QPushButton("▸  More")
        self.more_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_button.setStyleSheet(f"QPushButton {{ background: transparent; border: none; color: {theme.MUTED};"
                                       " font-weight: 600; text-align: left; padding: 2px; }"
                                       f"QPushButton:hover {{ color: {theme.TEXT}; }}")
        self.more_button.clicked.connect(lambda: self._show_more(not self.more.isVisibleTo(self)))
        self.body.addWidget(self.more_button)
        self.more = QWidget()
        more = QVBoxLayout(self.more)
        more.setContentsMargins(0, 0, 0, 0)
        more.setSpacing(12)
        self.see_through = Switch()
        self.see_through.setChecked(bool(settings.get("see_through", False)))
        more.addLayout(_row("See-through", self.see_through))
        self.fps = QComboBox()
        self.fps.setStyleSheet(combo_style())
        self.fps.setFixedHeight(BOX_HEIGHT)
        self.fps.setCursor(Qt.CursorShape.PointingHandCursor)
        for value, text in FPS_LIMITS:
            self.fps.addItem(text, value)
        limit = int(settings.get("fps_limit", 0) or 0)
        self.fps.setCurrentIndex(next((i for i, (v, _t) in enumerate(FPS_LIMITS) if v == limit), 0))
        more.addLayout(_row("Frame rate", self.fps))
        self.border = Switch()
        self.border.setChecked(bool(settings.get("border", False)))
        more.addLayout(_row("Windows' yellow border", self.border))
        note = QLabel("Captured through Windows, never hooked into the game, so anti-cheat stays happy.")
        note.setWordWrap(True)
        note.setStyleSheet(theme.MUTED_STYLE + "; font-size: 12px;")
        more.addWidget(note)
        self.body.addWidget(self.more)
        self._show_more(False)
        self.ok = self.add_buttons("Save")
        self.mode_group.buttonToggled.connect(lambda *_: self._mode_changed())
        self._mode_changed()

    def _show_more(self, on: bool) -> None:
        self.more.setVisible(on)
        self.more_button.setText(("▾  " if on else "▸  ") + "More")

    def mode(self) -> str:
        checked = self.mode_group.checkedButton()
        return str(checked.property("mode")) if checked is not None else "any"

    def _mode_changed(self) -> None:
        mode = self.mode()
        self.window_box.setVisible(mode == "window")
        self.hotkey_note.setVisible(mode == "hotkey")

    def values(self) -> tuple[dict[str, Any], bool]:
        """(settings changes, game sound on)."""
        mode = self.mode()
        out: dict[str, Any] = {"mode": mode, "cursor": self.cursor.isChecked(),
                               "see_through": self.see_through.isChecked(), "fps_limit": int(self.fps.currentData()),
                               "border": self.border.isChecked()}
        if mode == "window" and self.window.currentData() is not None:
            out["title"], out["process"] = self.window.currentData()
        elif mode == "any" or (mode == "hotkey" and self.was_mode != "hotkey"):
            out["title"], out["process"] = "", ""  # a hotkey capture starts empty until its key is pressed
        return out, self.sound.isChecked()
