"""Pop-ups that let go once they're closed.

A dialog run with .exec() stays a child of the window it was opened from. Most were freed once nothing in Python
held them any more, but any that something still held (a timer still queued, a closure kept by a page, a hook)
stayed for the rest of the session: on 2026-11-17 every Settings visit kept ~1,500 widgets and every Filters visit
another dialog. OBS deletes its properties and settings windows on close; modal() does the same for ours.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def modal(dialog) -> int:
    """dialog.exec(), then delete it once control returns to the window's event loop. Reading its answers right
    after (dialog.values(), dialog.account) is fine: it's only deleted later."""
    from sprigcast.app.theme import ui_burst

    ui_burst()  # showing it lays out and polishes every widget: Python's lock comes back quickly meanwhile
    from sprigcast.app import privacy

    if privacy.hidden():  # Settings → General → Privacy: kept out of screen captures like the main window
        privacy.apply_to(dialog, True)
    global _depth
    watch = _CloseWatch.start(dialog) if _depth == 0 else None  # one watch covers pop-ups opened from pop-ups
    _depth += 1
    try:
        return dialog.exec()
    finally:
        _depth -= 1
        if watch is not None:
            watch.stop()
        delete = getattr(dialog, "deleteLater", None)  # stand-ins in tests may not have it
        if delete is not None:
            delete()
        if _depth == 0 and _closing is not None:
            _close_when_free()


_depth = 0  # pop-ups open through modal() right now (one opened from another counts twice)
_closing = None  # the main window, once its × was clicked while a pop-up was up


WM_CLOSE = 0x0010
WM_SYSCOMMAND, SC_CLOSE = 0x0112, 0xF060


def _main_window():
    from PySide6.QtWidgets import QApplication

    # Asked of the class, never the window: Settings makes its pages when an attribute it doesn't have yet is
    # asked for, so hasattr(window, ...) built every page at once each time Settings opened (1.1 to 1.2 s with
    # the scene held, PC 2026-11-19).
    return next((w for w in QApplication.topLevelWidgets() if hasattr(type(w), "request_close")), None)


def close_main_after(dialog, window) -> None:
    """The main window's × was clicked while `dialog` was open: cancel every pop-up, inner first, then close the
    window the usual way once the last one has really returned (PC, 2026-11-20: Windows drops that click while
    a pop-up is up, so closing waited until it was closed by hand: 92 s; with Filters open the first try was
    lost because the window was asked to close from inside the pop-up's own loop)."""
    global _closing
    from PySide6.QtCore import QTimer

    log.info("main window closed while a pop-up was open: closing the pop-up first")
    _closing = window
    _cancel_all(dialog)
    if _depth == 0:  # not opened through modal() (tests, or a stand-in): nothing will say when it's done
        QTimer.singleShot(0, _close_when_free)


def _cancel(widget) -> None:
    reject = getattr(widget, "reject", None) or getattr(widget, "close", None)
    if reject is not None:
        try:
            reject()
        except Exception:  # noqa: BLE001 (a pop-up that won't cancel still mustn't stop the window closing)
            log.exception("couldn't cancel a pop-up while closing")


def _cancel_all(dialog, left: int = 10) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    still = QApplication.activeModalWidget()
    if still is not None and still is not _closing and left > 0:
        _cancel(still)  # the innermost first: a message box over Filters, then Filters
        QTimer.singleShot(0, lambda: _cancel_all(dialog, left - 1))
    elif dialog is not None and dialog.isVisible():
        _cancel(dialog)


def _close_when_free(left: int = 50) -> None:
    """Close the window from the top level: once no pop-up is up, a turn of the window's own loop later."""
    global _closing
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    window = _closing
    if window is None:
        return
    still = QApplication.activeModalWidget()
    if (still is not None and still is not window or _depth > 0) and left > 0:
        if still is not None and still is not window:
            _cancel(still)
        QTimer.singleShot(20, lambda: _close_when_free(left - 1))
        return
    _closing = None
    from shiboken6 import isValid

    if isValid(window):
        QTimer.singleShot(0, window.request_close)


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
            closing = msg.message == WM_CLOSE or (msg.message == WM_SYSCOMMAND and (int(msg.wParam) & 0xFFF0) == SC_CLOSE)
            if closing and int(msg.hWnd or 0) == self.hwnd:
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
