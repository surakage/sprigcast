"""One SprigCast at a time: a second launch brings the open window forward and quits.

The first copy listens on a local socket named for this Windows user; a second copy that reaches it waits
for the first to answer (it hangs up once its window is showing) and exits before loading anything.
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
    """True when another copy is running: it was asked to show its window.

    Reaching the running copy is the ask itself: no message has to get through. A named pipe on Windows could
    lose a "show" written just before the second copy hung up (a second launch did nothing on the PC,
    2026-11-16), so this copy stays connected until the running one hangs up after showing its window, or
    for at most two seconds."""
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
    socket.write(b"show\n")  # for an older copy that waits for it
    socket.flush()
    if socket.state() == QLocalSocket.LocalSocketState.ConnectedState:
        socket.waitForDisconnected(max(wait_ms, 2000))
    socket.abort()
    return True


def listen(on_show: Callable[[], None], parent=None, name: str | None = None) -> QLocalServer | None:
    """Be the copy that's running: call on_show whenever another launch reaches it."""
    name = name or server_name()
    server = QLocalServer(parent)
    if not server.listen(name):
        QLocalServer.removeServer(name)  # left over from a copy that crashed (Linux/macOS)
        if not server.listen(name):
            log.warning("single copy check off: %s", server.errorString())
            return None

    def arrived() -> None:
        launches = 0
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            launches += 1
            conn.disconnected.connect(conn.deleteLater)
            conn.disconnectFromServer()  # the answer: the second copy stops waiting and quits
        if launches:
            on_show()

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
