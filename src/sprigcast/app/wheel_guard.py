"""The mouse wheel scrolls the page, not the drop-down or number box that happens to be under the pointer.

A drop-down or number box takes the wheel only once you've clicked into it (it has the keyboard). Before,
scrolling down Settings → Stream → Quality could flip the encoder to x264 without a click.

Each drop-down and number box gets the filter itself just after it's made (theme.fit_widgets, guard). It used
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


def number_room(box: QAbstractSpinBox) -> None:
    """Qt ignores stylesheet padding for a number box's text (its line edit sits flush against the left edge):
    room on the left, or on both sides when it's centred (again when its alignment changes, see theme)."""
    from sprigcast.app.theme import NUMBER_PAD

    edit = box.lineEdit()
    if edit is not None:
        centred = bool(box.alignment() & Qt.AlignmentFlag.AlignHCenter)
        edit.setTextMargins(*((NUMBER_PAD // 2, 0, NUMBER_PAD // 2, 0) if centred else (NUMBER_PAD, 0, 4, 0)))


_guard: WheelGuard | None = None
_pending: list[QWidget] = []
SETTLE_MS = 250  # the filter goes on once a pop-up's widgets are made and shown


def _the_guard() -> WheelGuard:
    global _guard
    if _guard is None:
        _guard = WheelGuard(QApplication.instance())
    return _guard


def guard(widget: QWidget) -> None:
    """A drop-down or number box: no wheel until it has the keyboard. Once per widget.

    The filter itself goes on a moment later, for all the boxes made meanwhile at once: on from the start, it
    ran Python for each of the ~900 events of Settings → Stream's boxes being made and shown, each waiting for
    Python's lock while the render thread ran (PC 2026-11-18). Nobody scrolls a pop-up in its first 250 ms."""
    if not isinstance(widget, (QComboBox, QAbstractSpinBox)) or widget.property(GUARDED):
        return
    widget.setProperty(GUARDED, True)
    if widget.focusPolicy() == Qt.FocusPolicy.WheelFocus:
        widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # (and again once settled: set later by its maker)
    if isinstance(widget, QAbstractSpinBox):
        number_room(widget)
    _pending.append(widget)
    if len(_pending) == 1:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(SETTLE_MS, settle)


def settle() -> None:
    """Put the filter on every box made since the last time (and keep the wheel from focusing them)."""
    import shiboken6

    boxes = _pending[:]
    _pending.clear()
    the_guard = _the_guard()
    for widget in boxes:
        if not shiboken6.isValid(widget):
            continue  # a pop-up closed meanwhile
        if widget.focusPolicy() == Qt.FocusPolicy.WheelFocus:
            # Qt gives a WheelFocus widget the focus before any filter sees the wheel, so a scroll could still
            # change a stream's Resolution (PC test, 2026-11-17).
            widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        widget.installEventFilter(the_guard)


def install(app: QApplication) -> WheelGuard:
    """Kept for callers: makes the shared filter. Widgets get it as they're made (theme.fit_widgets)."""
    return _the_guard()
