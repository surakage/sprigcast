"""Safe mode: one start with plugins and web panels off, after SprigCast didn't close properly.

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
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox(QMessageBox.Icon.Question, "SprigCast",
                      "SprigCast didn't close properly last time.\n\n"
                      "Start in safe mode? Plugins and web panels stay off until the next start.")
    safe = box.addButton("Safe mode", QMessageBox.ButtonRole.AcceptRole)
    normal = box.addButton("Start normally", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(normal)
    box.exec()
    chosen = box.clickedButton() is safe
    box.deleteLater()
    return chosen
