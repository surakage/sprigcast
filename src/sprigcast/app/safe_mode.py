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


def on() -> bool:
    return _on


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


def ask() -> bool:
    """The question after an unexpected close. True: safe mode."""
    from sprigcast.app.message import MessageCard

    box = MessageCard("SprigCast didn't close properly last time",
                      "Start in safe mode? Plugins and web panels stay off and the old engine runs until the next "
                      "start.", kind="ask", width=480)
    safe = box.add_button("Safe mode")
    box.add_button("Start normally", primary=True)
    from sprigcast.app.logbook import crash_notes_paused

    with crash_notes_paused():  # COM's own handled 0x8001010d notes while it waits (see there)
        box.exec()
    chosen = box.clicked is safe
    box.deleteLater()
    return chosen
