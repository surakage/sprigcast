"""Pop-ups that let go once they're closed.

A dialog run with .exec() stays a child of the window it was opened from. Most were freed once nothing in Python
held them any more, but any that something still held (a timer still queued, a closure kept by a page, a hook)
stayed for the rest of the session: on 2026-11-17 every Settings visit kept ~1,500 widgets and every Filters visit
another dialog. OBS deletes its properties and settings windows on close; modal() does the same for ours.
"""

from __future__ import annotations


def modal(dialog) -> int:
    """dialog.exec(), then delete it once control returns to the window's event loop. Reading its answers right
    after (dialog.values(), dialog.account) is fine: it's only deleted later."""
    from sprigcast.app.theme import ui_burst

    ui_burst()  # showing it lays out and polishes every widget: Python's lock comes back quickly meanwhile
    from sprigcast.app import privacy

    if privacy.hidden():  # Settings → General → Privacy: kept out of screen captures like the main window
        privacy.apply_to(dialog, True)
    watch = _CloseWatch.start(dialog)
    try:
        return dialog.exec()
    finally:
        if watch is not None:
            watch.stop()
        delete = getattr(dialog, "deleteLater", None)  # stand-ins in tests may not have it
        if delete is not None:
            delete()


WM_CLOSE = 0x0010


def _main_window():
    from PySide6.QtWidgets import QApplication

    # Asked of the class, never the window: Settings makes its pages when an attribute it doesn't have yet is
    # asked for, so hasattr(window, ...) built every page at once each time Settings opened (1.1 to 1.2 s with
    # the scene held, PC 2026-11-19).
    return next((w for w in QApplication.topLevelWidgets() if hasattr(type(w), "request_close")), None)


def close_main_after(dialog, window) -> None:
    """The main window's × was clicked while `dialog` was open: cancel the pop-up, then close the window the
    usual way. Windows drops that click while a pop-up is up, so closing waited until the pop-up was closed
    by hand (PC, 2026-11-20: 92 s)."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    def cancel(widget) -> None:
        reject = getattr(widget, "reject", None) or getattr(widget, "close", None)
        if reject is not None:
            reject()

    def next_one(left: int = 10) -> None:  # a pop-up opened from a pop-up: each one goes, inner first
        still = QApplication.activeModalWidget()
        if still is not None and still is not window and left > 0:
            cancel(still)
            QTimer.singleShot(0, lambda: next_one(left - 1))
            return
        window.request_close()

    cancel(dialog)
    QTimer.singleShot(0, next_one)  # after exec() has returned and the caller has moved on


class _CloseWatch:
    """While a pop-up is open on Windows, watches for the main window's close message. Installed only for that
    time: a Python filter on every native message all the time would cost the UI thread."""

    def __init__(self, dialog, window):
        from PySide6.QtCore import QAbstractNativeEventFilter

        watch = self

        class Filter(QAbstractNativeEventFilter):
            def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt naming)
                watch.saw(event_type, message)
                return False, 0

        self.dialog, self.window, self.hwnd, self.fired = dialog, window, int(window.winId()), False
        self.filter = Filter()

    @classmethod
    def start(cls, dialog) -> _CloseWatch | None:
        import sys

        if sys.platform != "win32":
            return None
        try:
            from PySide6.QtCore import QCoreApplication

            window = _main_window()
            if window is None or window is dialog or not window.isVisible():
                return None
            watch = cls(dialog, window)
            QCoreApplication.instance().installNativeEventFilter(watch.filter)
            return watch
        except Exception:  # noqa: BLE001 (a pop-up always opens, watched or not)
            return None

    def saw(self, event_type, message) -> None:
        try:
            if self.fired or bytes(event_type) != b"windows_generic_MSG":
                return
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_CLOSE and int(msg.hWnd or 0) == self.hwnd:
                from PySide6.QtCore import QTimer

                self.fired = True
                QTimer.singleShot(0, lambda: close_main_after(self.dialog, self.window))
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        try:
            from PySide6.QtCore import QCoreApplication

            QCoreApplication.instance().removeNativeEventFilter(self.filter)
        except Exception:  # noqa: BLE001
            pass
