"""The mouse wheel scrolls the page, not the drop-down or number box that happens to be under the pointer.

A drop-down or number box takes the wheel only once you've clicked into it (it has the keyboard). Before,
scrolling down Settings → Stream → Quality could flip the encoder to x264 without a click.

Each drop-down and number box gets the filter itself when it's first polished (theme.RoomyStyle). It used
to be one filter on the whole app, which ran Python for every event of every widget: while streaming and
recording, other threads hold Python's lock, so each of those ~36,000 calls waited its turn and opening
Settings froze the window for seconds (PC test, 2026-11-17).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QComboBox, QWidget

GUARDED = "_sprigcast_wheel_guard"


class WheelGuard(QObject):
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Wheel and not obj.hasFocus():
            event.ignore()  # handled here, not accepted: Qt passes it on to the page behind
            return True
        return False


_guard: WheelGuard | None = None


def _the_guard() -> WheelGuard:
    global _guard
    if _guard is None:
        _guard = WheelGuard(QApplication.instance())
    return _guard


def guard(widget: QWidget) -> None:
    """A drop-down or number box: no wheel until it has the keyboard. Once per widget."""
    if not isinstance(widget, (QComboBox, QAbstractSpinBox)) or widget.property(GUARDED):
        return
    widget.setProperty(GUARDED, True)
    if widget.focusPolicy() == Qt.FocusPolicy.WheelFocus:
        # Qt gives a WheelFocus widget the focus before any filter sees the wheel, so a scroll could still
        # change a stream's Resolution (PC test, 2026-11-17).
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.installEventFilter(_the_guard())


def install(app: QApplication) -> WheelGuard:
    """Kept for callers: makes the shared filter. Widgets get it from theme.RoomyStyle as they're polished."""
    return _the_guard()
