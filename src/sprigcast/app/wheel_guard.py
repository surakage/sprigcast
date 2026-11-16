"""The mouse wheel scrolls the page, not the drop-down or number box that happens to be under the pointer.

A drop-down or number box takes the wheel only once you've clicked into it (it has the keyboard). Before,
scrolling down Settings → Output could flip the encoder to x264 without a click.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QComboBox


class WheelGuard(QObject):
    def eventFilter(self, obj, event) -> bool:  # noqa: N802
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
