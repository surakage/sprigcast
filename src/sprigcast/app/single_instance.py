"""One SprigCast at a time: a second launch brings the open window forward and quits.

The first copy listens on a local socket named for this Windows user; a second copy that reaches it
sends "show" and exits before loading anything.
"""

from __future__ import annotations

import getpass
import logging
import sys
from collections.abc import Callable

from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)


def server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return "SprigCast-" + "".join(c for c in user if c.isalnum()) or "SprigCast"


def ask_running_copy(name: str | None = None, wait_ms: int = 400) -> bool:
    """True when another copy is running: it was asked to show its window."""
    socket = QLocalSocket()
    socket.connectToServer(name or server_name())
    if not socket.waitForConnected(wait_ms):
        return False
    if sys.platform == "win32":  # let the running copy take the foreground (Windows blocks it otherwise)
        try:
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:
            pass
    socket.write(b"show\n")
    socket.flush()
    socket.waitForBytesWritten(wait_ms)
    socket.disconnectFromServer()
    return True


def listen(on_show: Callable[[], None], parent=None, name: str | None = None) -> QLocalServer | None:
    """Be the copy that's running: call on_show whenever another launch asks."""
    name = name or server_name()
    server = QLocalServer(parent)
    if not server.listen(name):
        QLocalServer.removeServer(name)  # left over from a copy that crashed (Linux/macOS)
        if not server.listen(name):
            log.warning("single copy check off: %s", server.errorString())
            return None

    def arrived() -> None:
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            conn.readyRead.connect(lambda c=conn: (c.readAll(), on_show()))
            conn.disconnected.connect(conn.deleteLater)

    server.newConnection.connect(arrived)
    return server


def bring_forward(window) -> None:
    """Show the window on top: restored if minimised or hidden to the tray."""
    from PySide6.QtCore import Qt

    if window.isMinimized():
        window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
    window.show()
    window.raise_()
    window.activateWindow()
