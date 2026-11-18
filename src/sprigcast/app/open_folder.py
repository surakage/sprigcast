"""Open a folder in Explorer without holding up the window.

QDesktopServices.openUrl waits for Windows to start Explorer: 325 ms of the window's thread on the PC
(2026-11-18, Help → Logs), with the canvases waiting on Python's lock meanwhile. Here Windows is asked on a
worker and the window carries on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def open_folder(path: str | Path) -> None:
    folder = str(path)
    if sys.platform == "win32":
        from sprigcast.core import workers

        workers.run(os.startfile, folder, name="open folder")  # noqa: S606 (a folder SprigCast made)
        return
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
