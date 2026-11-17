"""The mouse wheel scrolls the page, not the drop-down or number box that happens to be under the pointer.

A drop-down or number box takes the wheel only once you've clicked into it (it has the keyboard). Before,
scrolling down Settings → Stream → Quality could flip the encoder to x264 without a click.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QComboBox


class WheelGuard(QObject):
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.Polish and isinstance(obj, (QComboBox, QAbstractSpinBox)) \
                and obj.focusPolicy() == Qt.FocusPolicy.WheelFocus:
            # Qt gives a WheelFocus widget the focus before any filter sees the wheel, so the check below
            # found it focused and let a scroll change a stream's Resolution (PC test, 2026-11-17).
            obj.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if event.type() == QEvent.Type.Wheel and isinstance(obj, (QComboBox, QAbstractSpinBox)) \
                and not obj.hasFocus():
            event.ignore()  # handled here, not accepted: Qt passes it on to the page behind
            return True
        return False


_guard: WheelGuard | None = None


def install(app: QApplication) -> WheelGuard:
    global _guard
    if _guard is None:
        _guard = WheelGuard(app)
        app.installEventFilter(_guard)
    return _guard
