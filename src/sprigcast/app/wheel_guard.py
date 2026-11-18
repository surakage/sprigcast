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
_BOXES = (QComboBox, QAbstractSpinBox)
_STRONG = Qt.FocusPolicy.StrongFocus
_WHEEL = Qt.FocusPolicy.WheelFocus
_LEFT_ROOM: tuple[int, int, int, int] | None = None


def _left_room() -> tuple[int, int, int, int]:
    """number_room's margins for a left-aligned box, worked out once."""
    global _LEFT_ROOM
    if _LEFT_ROOM is None:
        from sprigcast.app.theme import NUMBER_PAD

        _LEFT_ROOM = (NUMBER_PAD, 0, 4, 0)
    return _LEFT_ROOM


SETTLE_STEP = 24  # boxes settled a tick
SETTLE_GAP_MS = 15
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
    if not isinstance(widget, _BOXES) or getattr(widget, "_guard_due", False) or widget.property(GUARDED):
        return
    _fresh(widget)


def guard_new(widget: QWidget) -> None:
    """guard() for a box just made (theme.fit_widgets hooks __init__): nothing is asked of it first. Its focus
    is Qt's WheelFocus and its text left-aligned, so both are set straight away: asking each new box (and
    turning Qt's answers into Python enums) was 170 ms of opening the Sound gear while live on the PC (2026-11-18,
    three boxes a sound)."""
    _fresh(widget)


def _fresh(widget: QWidget) -> None:
    """As few calls into Qt as can be: while live each one may wait its turn for Python's lock (the Sound gear's
    rows, three boxes each, were 107-136 ms of it on the PC, 2026-11-18). The focus and the filter go on with
    the rest a moment later (settle); only a number box's text room is now, so its number doesn't jump."""
    widget._guard_due = True  # Python's own note: no call into Qt
    if isinstance(widget, QAbstractSpinBox):
        edit = widget.lineEdit()
        if edit is not None:
            edit.setTextMargins(*_left_room())  # number_room for a left-aligned box (setAlignment calls it again)
    _pending.append(widget)
    if len(_pending) == 1:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(SETTLE_MS, settle)


def settle(step: int | None = SETTLE_STEP) -> None:
    """Put the filter on the boxes made since the last time (and keep the wheel from focusing them), `step` of
    them now and the rest a tick later; None: all of them now."""
    import shiboken6

    boxes = _pending[:step]
    del _pending[:step]
    if _pending:  # a big page's boxes a few at a time, the window answering in between
        from PySide6.QtCore import QTimer

        QTimer.singleShot(SETTLE_GAP_MS, settle)
    the_guard = _the_guard()
    for widget in boxes:
        if not shiboken6.isValid(widget):
            continue  # a pop-up closed meanwhile
        if widget.focusPolicy() == _WHEEL:
            # Qt gives a WheelFocus widget the focus before any filter sees the wheel, so a scroll could still
            # change a stream's Resolution (PC test, 2026-11-17).
            widget.setFocusPolicy(_STRONG)
        widget.installEventFilter(the_guard)
        widget.setProperty(GUARDED, True)


def install(app: QApplication) -> WheelGuard:
    """Kept for callers: makes the shared filter. Widgets get it as they're made (theme.fit_widgets)."""
    return _the_guard()
