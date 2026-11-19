"""Settings → General → System tray: SprigCast's icon beside the clock, and the window out of the way there.

- Show in the tray: the icon, with Open SprigCast, Go live, Start recording and Quit. A click on it opens the
  window, and resting the pointer on it says whether you're live or recording.
- Minimize to the tray: minimizing hides the window (no taskbar button) instead.
- Close to the tray: the window's × hides it and SprigCast keeps running (a stream or recording carries on).
  Quit on the icon's menu closes it for real, asking first while live or recording like the × does.

The last two only work while the icon is really there: where Windows has no tray (or the icon couldn't be made)
the window minimizes and closes as usual, so it can never be hidden with no way back.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QSettings

log = logging.getLogger(__name__)

SHOW = "general/tray_show"
MINIMIZE_TO = "general/tray_minimize"
CLOSE_TO = "general/tray_close"
TOLD = "general/tray_told"  # the "still running" note was shown once


def wanted() -> bool:
    return QSettings().value(SHOW, False, type=bool)


def minimize_to() -> bool:
    return wanted() and QSettings().value(MINIMIZE_TO, False, type=bool)


def close_to() -> bool:
    return wanted() and QSettings().value(CLOSE_TO, False, type=bool)


def _make_icon(window):
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon

    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    return QSystemTrayIcon(QApplication.windowIcon(), window)


class Tray:
    """The main window's tray icon, there only while the setting is on. make_icon: for tests."""

    def __init__(self, window, make_icon=_make_icon):
        self.window = window
        self.icon = None
        self.menu = None
        self._make_icon = make_icon
        self._state = (False, False)  # (live, recording), as last shown

    def active(self) -> bool:
        return self.icon is not None and self.icon.isVisible()

    def sync(self) -> None:
        """Follow the setting: make the icon, or take it away (and bring back a window hidden there)."""
        if wanted() and self.icon is None:
            try:
                self.icon = self._make_icon(self.window)
            except Exception:  # noqa: BLE001  never in the way of the window
                log.warning("tray: couldn't make the icon", exc_info=True)
                self.icon = None
            if self.icon is None:
                return
            from PySide6.QtWidgets import QMenu

            self.menu = QMenu(self.window)
            self.menu.addAction("Open SprigCast", self.open)
            self.menu.addSeparator()
            self.live_action = self.menu.addAction("Go live", self._live)
            self.record_action = self.menu.addAction("Start recording", self._record)
            self.menu.addSeparator()
            self.menu.addAction("Quit", self.quit)
            self.icon.setContextMenu(self.menu)
            self._state = (None, None)
            self.show_state(False, False)
            self.icon.activated.connect(self._activated)
            self.icon.show()
            log.info("tray: icon shown")
        elif not wanted() and self.icon is not None:
            self.remove()
            if not self.window.isVisible():
                self.open()

    def remove(self) -> None:
        """Take the icon away (the setting went off, or SprigCast is closing: no ghost icon left by the clock)."""
        icon, menu, self.icon, self.menu = self.icon, self.menu, None, None
        if icon is not None:
            icon.hide()
            icon.deleteLater()
        if menu is not None:
            menu.deleteLater()

    def show_state(self, live: bool, recording: bool) -> None:
        """Live and Recording on the icon's tip and menu (asked often by the window: nothing done unless changed)."""
        if (live, recording) == self._state or self.icon is None:
            return
        self._state = (live, recording)
        doing = " and ".join(what for what, on in (("Live", live), ("Recording", recording)) if on)
        self.icon.setToolTip(f"SprigCast · {doing}" if doing else "SprigCast")
        self.live_action.setText("End stream" if live else "Go live")
        self.record_action.setText("Stop recording" if recording else "Start recording")

    def _live(self) -> None:
        """As the Go live button: it asks where the window would, so the window comes up for the question."""
        self.open()
        self.window._on_live_clicked()

    def _record(self) -> None:
        window = self.window
        if getattr(window, "recording", None) is not None:
            self.open()  # stopping may ask first
        window._on_record_clicked()

    def _activated(self, reason) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.open()

    def open(self) -> None:
        from sprigcast.app.single_instance import bring_forward

        bring_forward(self.window)

    def quit(self) -> None:
        """Close for real. While live or recording it asks first, as the window's × does."""
        window = self.window
        if not window.isVisible():
            self.open()  # the question needs its window
        if window._may_close():
            window.close()

    def hide_window(self, why: str) -> bool:
        """Hide the window to the tray. False (and nothing done) when there's no icon to bring it back with."""
        if not self.active():
            return False
        self.window.hide()
        log.info("tray: window hidden (%s)", why)
        settings = QSettings()
        if not settings.value(TOLD, False, type=bool):  # once: a window that vanishes needs saying where it went
            settings.setValue(TOLD, True)
            try:
                self.icon.showMessage("SprigCast", "Still running here. Right-click for Quit.")
            except Exception:  # noqa: BLE001
                pass
        return True

    def closing(self) -> bool:
        """The window's × was pressed: True when it was hidden to the tray instead."""
        return close_to() and self.hide_window("closed")

    def minimized(self) -> bool:
        return minimize_to() and self.hide_window("minimized")
