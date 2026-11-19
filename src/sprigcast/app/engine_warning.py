"""The window's warning that the old (Python) engine is drawing on a PC whose graphics card could run the new one
(user, 2026-11-19): in safe mode, with SPRIGCAST_ENGINE=python, when sprigcore.dll is missing, or after the engine
stopped during this run. The old engine uses more of the PC, so it shouldn't go unnoticed there. A PC without a
graphics card keeps the old engine and is told nothing."""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel

from sprigcast import sprigcore
from sprigcast.app import theme

log = logging.getLogger(__name__)

CHECK_MS = 5_000  # the engine can stop while SprigCast runs: looked at again this often


def text() -> str:
    why = sprigcore.old_engine_warning()
    return f"Old engine in use ({why.lower()})" if why else ""


def install(window) -> QLabel:
    """A label at the right of the window's status bar, shown only while the warning holds."""
    label = QLabel()
    label.setObjectName("engineWarning")
    label.setStyleSheet(f"color: {theme.WARN}; padding: 0 10px;")
    label.hide()
    window.statusBar().addPermanentWidget(label)

    def refresh() -> None:
        now = text()
        if now != label.text():
            label.setText(now)
            label.setVisible(bool(now))
            if now:
                log.warning("%s: this PC's graphics card could run the new engine", now)

    timer = QTimer(label)
    timer.setInterval(CHECK_MS)
    timer.timeout.connect(refresh)
    timer.start()
    label.refresh = refresh  # type: ignore[attr-defined]
    refresh()
    return label
