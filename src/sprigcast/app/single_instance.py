"""One SprigCast at a time: a second launch brings the open window forward and quits.

The first copy listens on a local socket named for this Windows user; a second copy that reaches it waits
for the first to answer (it hangs up once its window is showing) and exits before loading anything.

The socket only exists once the first copy has got that far, which took almost a minute on a slow PC with a hard
drive (tester PC, 2026-11-22): launches clicked again meanwhile found nobody listening and all opened, up to five
copies fighting over one graphics chip. So on Windows a copy first claims a named mutex the moment it starts
(claim()); a copy that finds it taken waits for the one holding it (wait_for_first_copy()).
"""

from __future__ import annotations

import getpass
import logging
import sys
import time
from collections.abc import Callable

from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)


def server_name() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return "SprigCast-" + "".join(c for c in user if c.isalnum()) or "SprigCast"


ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0, WAIT_ABANDONED = 0x0, 0x80
FIRST_COPY_WAIT_S = 90.0  # longest a launch waits for a copy that's still starting before it opens anyway
_claim = None  # the mutex handle this copy holds, or the one it waits on


def claim(name: str | None = None) -> bool:
    """Windows: take this user's "SprigCast is running" mutex at once. False when another copy holds it
    (running, starting, or closing for a restart). Elsewhere always True: the socket alone decides there."""
    global _claim
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, True, "SprigCastOneCopy-" + (name or server_name()))
        taken = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001 - no mutex: the socket check alone, as before
        return True
    if not handle:
        return True
    _claim = handle  # kept (owned or not) until this copy ends; Windows lets go of it then
    return not taken


def _wait_turn(ms: int) -> bool:
    """True when the copy holding the mutex let go (closed) and this copy now holds it."""
    if _claim is None:
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    return kernel32.WaitForSingleObject(_claim, ms) in (WAIT_OBJECT_0, WAIT_ABANDONED)


def wait_for_first_copy(ask: Callable[[], bool] | None = None, turn: Callable[[int], bool] | None = None,
                        longest_s: float = FIRST_COPY_WAIT_S) -> bool:
    """Another copy holds the mutex: True once it was asked to show its window (this copy should quit), False
    when it closed meanwhile (a restart) or never answered in longest_s (this copy opens)."""
    ask = ask or ask_running_copy
    turn = turn or _wait_turn
    told = False
    deadline = time.monotonic() + longest_s
    while True:
        if ask():
            return True
        if turn(500):
            return False
        if time.monotonic() >= deadline:
            log.warning("single copy check: another SprigCast didn't answer in %.0f s; opening anyway", longest_s)
            return False
        if not told:
            told = True
            log.info("single copy check: another SprigCast is starting; it comes forward once it's ready")


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
