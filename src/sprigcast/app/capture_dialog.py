"""Screen capture's and window capture's settings cards, laid out like game capture's.

    Screen capture                       Window capture
    Screen   [ Monitor 1: 1920x1080 ▾ ]  Window         [ 🌐 Firefox          ▾ ]
    Mouse pointer                  (●)   Find it again  [ Same app            ▾ ]
    Show SprigCast                 ( )   Mouse pointer                          (●)
                                         Show SprigCast                         ( )
                                         Window sound                           ( )

"Find it again" is how a window is looked up when it opens again: Same app (the default) takes another
window of the same app when the title changed, like a browser on a new tab; Exact title waits for that title.

"Show SprigCast" (off by default) is for recording SprigCast itself: on a screen capture it lets SprigCast's windows
through even with Settings → General → Privacy → Hide SprigCast from screen capture on; on a window capture it also
lists SprigCast's own windows. Off, a screen capture of the screen SprigCast is on doesn't show itself in itself
when that Privacy setting is on.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QFileInfo, QSize, Qt
from PySide6.QtWidgets import QComboBox, QFileIconProvider, QWidget

from sprigcast.app import theme
from sprigcast.app.card import CardDialog, field_label
from sprigcast.app.game_dialog import WindowCombo, _row

MATCHES = (("Exact title", "title"), ("Same app", "app"))  # A to Z


def _combo(box: QComboBox) -> QComboBox:
    from sprigcast.app.starter import BOX_HEIGHT, combo_style

    box.setStyleSheet(combo_style())
    box.setFixedHeight(BOX_HEIGHT)
    box.setIconSize(QSize(18, 18))
    box.setCursor(Qt.CursorShape.PointingHandCursor)
    box.setMaxVisibleItems(14)
    return box


class DisplayCaptureDialog(CardDialog):
    """monitors: win32_enum.MonitorInfo list."""

    def __init__(self, settings: dict[str, Any], monitors: list, parent: QWidget | None = None):
        super().__init__("Screen capture", parent, width=480)
        from sprigcast.app.live_stage import Switch

        self.monitors = list(monitors)
        self.body.addWidget(field_label("Screen"))
        self.screen = _combo(QComboBox())
        current = int(settings.get("monitor", 1))
        for m in self.monitors:
            self.screen.addItem(m.label, m.index)
        if current not in [m.index for m in self.monitors]:
            self.screen.addItem(f"Monitor {current}", current)  # unplugged right now: keep it
        self.screen.setCurrentIndex(max(0, self.screen.findData(current)))
        self.screen.setAccessibleName("Screen")
        self.body.addWidget(self.screen)
        self.cursor = Switch()
        self.cursor.setChecked(bool(settings.get("cursor", True)))
        self.cursor.setAccessibleName("Mouse pointer")
        self.body.addLayout(_row("Mouse pointer", self.cursor))
        self.show_own = Switch()
        self.show_own.setChecked(bool(settings.get("sprigcast", False)))
        self.show_own.setAccessibleName("Show SprigCast")
        self.body.addLayout(_row("Show SprigCast", self.show_own))
        self.ok = self.add_buttons("Save")

    def values(self) -> dict[str, Any]:
        return {"monitor": int(self.screen.currentData() or 1), "cursor": self.cursor.isChecked(),
                "sprigcast": self.show_own.isChecked()}

    def monitor(self):
        """The picked MonitorInfo, or None when it isn't plugged in."""
        index = self.values()["monitor"]
        return next((m for m in self.monitors if m.index == index), None)


class WindowCaptureDialog(CardDialog):
    """windows: win32_enum.WindowInfo list; has_sound: the capture brings its own sound now.
    list_windows: called again whenever the drop-down opens (None keeps the first list); list_own: the same with
    SprigCast's own windows too, used while Show SprigCast is on."""

    def __init__(self, settings: dict[str, Any], windows: list, has_sound: bool, parent: QWidget | None = None,
                 list_windows: Callable[[], list] | None = None, list_own: Callable[[], list] | None = None):
        super().__init__("Window capture", parent, width=480)
        from sprigcast.app.live_stage import Switch

        self._list_windows = list_windows
        self._list_own = list_own
        self.show_own = Switch()
        self.show_own.setChecked(bool(settings.get("sprigcast", False)))
        self._icons = QFileIconProvider()
        self.body.addWidget(field_label("Window"))
        self.window = _combo(WindowCombo(self._reload))
        self.window.setAccessibleName("Window")
        self.body.addWidget(self.window)
        if self.show_own.isChecked() and list_own is not None:
            windows = list_own()  # SprigCast's own windows too
        self._fill(list(windows), (str(settings.get("title", "")), str(settings.get("process", ""))))

        self.body.addWidget(field_label("Find it again"))
        self.match = _combo(QComboBox())
        for label, key in MATCHES:
            self.match.addItem(label, key)
        self.match.setCurrentIndex(max(0, self.match.findData(str(settings.get("match", "app")))))
        self.match.setAccessibleName("Find it again")
        self.body.addWidget(self.match)

        self.cursor = Switch()
        self.cursor.setChecked(bool(settings.get("cursor", True)))
        self.cursor.setAccessibleName("Mouse pointer")
        self.body.addLayout(_row("Mouse pointer", self.cursor))
        self.show_own.setAccessibleName("Show SprigCast")
        self.body.addLayout(_row("Show SprigCast", self.show_own))
        self.show_own.toggled.connect(lambda *_: self._reload())  # SprigCast's windows join or leave the list
        self.sound = Switch()
        self.sound.setChecked(has_sound)
        self.sound.setAccessibleName("Window sound")
        self.body.addLayout(_row("Window sound", self.sound))
        self.ok = self.add_buttons("Save")
        self.ok.setEnabled(self.window.count() > 0)

    def _icon(self, w):
        from sprigcast.app.icons import icon

        path = getattr(w, "path", "")
        return self._icons.icon(QFileInfo(path)) if path else icon("panel", theme.MUTED)

    def _fill(self, windows: list, pick: tuple[str, str]) -> None:
        """The open windows A to Z (plus the saved one when it isn't open). pick: (title, process) to select."""
        from sprigcast.app.icons import icon
        from sprigcast.app.names import window_name

        title, process = pick
        self.windows = sorted(windows, key=lambda w: window_name(w.title, w.process).lower())
        box = self.window
        box.blockSignals(True)
        box.clear()
        for w in self.windows:
            box.addItem(self._icon(w), window_name(w.title, w.process), (w.title, w.process))
        if (title or process) and not any(
                (process and w.process.lower() == process.lower()) or (not process and w.title == title)
                for w in self.windows):
            box.addItem(icon("panel", theme.MUTED), window_name(title, process), (title, process))  # closed now
        at = 0
        for i in range(box.count()):
            t, p = box.itemData(i)
            if (process and p.lower() == process.lower() and t == title) or (not process and t == title):
                at = i
                break
            if process and p.lower() == process.lower():
                at = i  # same app, another title: keep looking for the exact one
        box.setCurrentIndex(at)
        box.blockSignals(False)
        if hasattr(self, "ok"):
            self.ok.setEnabled(box.count() > 0)

    def _reload(self) -> None:
        lister = self._list_own if self.show_own.isChecked() and self._list_own is not None else self._list_windows
        if lister is None:
            return
        self._fill(list(lister()), self.window.currentData() or ("", ""))

    def values(self) -> tuple[dict[str, Any], bool]:
        """(settings changes, window sound on)."""
        title, process = self.window.currentData() or ("", "")
        out = {"title": title, "process": process, "match": str(self.match.currentData() or "app"),
               "cursor": self.cursor.isChecked(), "sprigcast": self.show_own.isChecked()}
        return out, self.sound.isChecked()
