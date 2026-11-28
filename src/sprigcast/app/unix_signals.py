"""Linux and macOS: a polite "please close" from the system is a clean close.

Signing out, shutting down and a plain `kill` send SIGTERM. Left alone it ends Python on the spot: nothing saved,
recordings not finished, and the next start said "closed unexpectedly: No error was recorded" (Ubuntu virtual PC,
test build 16, 2026-11-28). Here it closes the window the way Quit does: streams stop, recordings finish, the
garden is saved.

Python only runs a signal handler when its own code next runs on the main thread, and Qt's event loop can sit in
C++ for a long while. So the signal's number is also written to a socket (signal.set_wakeup_fd) that Qt watches.
Windows has its own way (the session's commitDataRequest, app/main.py) and nothing is installed there.
"""

from __future__ import annotations

import logging
import signal
import socket
import sys

log = logging.getLogger(__name__)

NAMES = ("SIGTERM", "SIGHUP", "SIGINT")
_keep: list = []  # the sockets and Qt's watcher, for the life of the app


def install(app, window) -> bool:
    """Close `window` and end `app` when the system asks. False (nothing done) on Windows."""
    if sys.platform == "win32":
        return False
    from PySide6.QtCore import QSocketNotifier

    reader, writer = socket.socketpair()
    reader.setblocking(False)
    writer.setblocking(False)
    asked: list[str] = []

    def note(number, _frame) -> None:
        asked.append(signal.Signals(number).name)

    def close() -> None:
        try:
            reader.recv(64)
        except OSError:
            pass
        if not asked or app.property("sprigcast_signal"):
            return
        app.setProperty("sprigcast_signal", asked[0])
        log.info("closing: the system sent %s", asked[0])
        window.close()  # as Quit does, without asking: nobody may be there to answer
        app.quit()

    for name in NAMES:
        signal.signal(getattr(signal, name), note)
    signal.set_wakeup_fd(writer.fileno(), warn_on_full_buffer=False)
    notifier = QSocketNotifier(reader.fileno(), QSocketNotifier.Type.Read, app)
    notifier.activated.connect(lambda *_: close())
    _keep[:] = [reader, writer, notifier]
    return True
