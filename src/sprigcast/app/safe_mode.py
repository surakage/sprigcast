"""Safe mode: one start with plugins and web panels off and the old (Python) engine, after SprigCast didn't close
properly.

A plugin or web panel that crashes SprigCast while it starts would otherwise do it again on every start.
After an unexpected close the next start asks first (not when it starts minimized with Windows). Starting
with ``--safe-mode`` does it without asking. Nothing is changed or removed: blooms keep their plugin
sprigs, which simply stay empty until the next normal start.
"""

from __future__ import annotations

import logging

ARG = "--safe-mode"

log = logging.getLogger(__name__)
_on = False
_unattended = False  # the question after a crash went unanswered: nobody's at the PC (see unattended)


def on() -> bool:
    return _on


def unattended() -> bool:
    """This start went ahead on its own: the safe mode question counted down with no answer. Whatever else would
    wait for someone (the crash card) shouldn't sit over the window of a PC nobody is at (2026-11-20)."""
    return _unattended


def choose(argv: list[str], crashed, minimized: bool, ask) -> bool:
    """Whether this start is in safe mode: asked for on the command line, or picked after a crash."""
    global _on
    if ARG in argv:
        _on = True
    elif crashed is not None and not minimized:
        _on = bool(ask())
    else:
        _on = False
    if _on:
        log.warning("safe mode: plugins and web panels are off for this start")
    return _on


AUTO_START_S = 8  # the question starts normally on its own after this long, unless touched (15 s made a start
# after a crash take 16.6 s, with 17 Control API requests waiting behind it, PC 2026-11-27)


def ask(seconds: int | None = None) -> bool:
    """The question after an unexpected close. True: safe mode.

    Nobody there to answer (a crash while away, a PC that restarted) left SprigCast down: after the 12:05 crash
    on 2026-11-20 it sat unanswered for 30 minutes. So it starts normally on its own after AUTO_START_S, counting
    down where it can be seen. Any click or key on it stops the count; then it waits for an answer."""
    from PySide6.QtCore import QEvent, QObject, QTimer
    from PySide6.QtWidgets import QApplication, QLabel

    from sprigcast.app import theme
    from sprigcast.app.message import MessageCard

    left = [AUTO_START_S if seconds is None else seconds]
    box = MessageCard("SprigCast didn't close properly last time",
                      "Start in safe mode? Plugins and web panels stay off and the old engine runs until the next "
                      "start.", kind="ask", width=480)
    count = QLabel(box)
    count.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
    box.add_widget(count)
    safe = box.add_button("Safe mode")
    normal = box.add_button("Start normally", primary=True)
    timer = QTimer(box)
    timer.setInterval(1000)
    outcome = ["answered"]

    def show_count() -> None:
        count.setText(f"Starting normally in {left[0]} s")

    def tick() -> None:
        left[0] -= 1
        if left[0] > 0:
            show_count()
            return
        timer.stop()
        outcome[0] = "no answer"
        box.clicked = normal
        box.accept()

    class Touch(QObject):
        """Any click or key on the card: the count stops and it waits for an answer."""

        def eventFilter(self, watched, event):  # noqa: N802
            if timer.isActive() and event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress) \
                    and isinstance(watched, QObject) and _inside(watched, box):
                timer.stop()
                outcome[0] = "answered after stopping the count"
                count.setText("Waiting for your choice")
            return False

    touch = Touch(box)
    app = QApplication.instance()
    show_count()
    timer.timeout.connect(tick)
    timer.start()
    app.installEventFilter(touch)
    from sprigcast.app.logbook import crash_notes_paused

    try:
        with crash_notes_paused():  # COM's own handled 0x8001010d notes while it waits (see there)
            box.exec()
    finally:
        app.removeEventFilter(touch)
        timer.stop()
    chosen = box.clicked is safe
    global _unattended
    _unattended = outcome[0] == "no answer"
    log.info("safe mode question: %s, %s", outcome[0], "safe mode" if chosen else "starting normally")
    box.deleteLater()
    return chosen


def _inside(widget, box) -> bool:
    while widget is not None:
        if widget is box:
            return True
        widget = widget.parent() if hasattr(widget, "parent") else None
    return False
