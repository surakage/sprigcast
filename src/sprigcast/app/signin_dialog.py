"""The small "finish signing in in your browser" window for YouTube and Kick (output/service_signin.py).

Threads: the window, the browser and the sign-in's settings stay on the UI thread. Only the waiting and
the web calls run on a worker, and that worker gets plain Python objects (the Flow, a cancel Event and a
Job to put its answer in), never this window. A worker holding the window could drop the last reference
to it and free Qt objects off the UI thread, which crashes Windows with heap corruption (2026-11-16).
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from sprigcast.app import theme
from sprigcast.app.card import CardDialog
from sprigcast.output import service_signin
from sprigcast.output.oauth_loopback import Cancelled, SignInError
from sprigcast.output.stream import SERVICES

log = logging.getLogger(__name__)

JOIN_SECONDS = 0.5  # on close: how long to wait for the worker to notice the cancel


class Job:
    """The worker's answer, handed back to the UI thread: fetched, or an error worded for the person."""

    def __init__(self) -> None:
        self.fetched: service_signin.Fetched | None = None
        self.error: str | None = None


def _work(finish, cancel: threading.Event, job: Job) -> None:
    """Runs on the worker. Only plain objects come in: no window, no Qt."""
    try:
        job.fetched = finish(cancel)
    except Cancelled:
        pass
    except SignInError as exc:
        job.error = str(exc)
    except Exception as exc:  # noqa: BLE001 (worded for the person; Flow.finish logged it)
        job.error = f"Sign-in failed: {exc}"


def open_in_browser(url: str) -> bool:
    """Open the sign-in page. On the UI thread: Windows' ShellExecute wants a thread set up for COM."""
    return QDesktopServices.openUrl(QUrl.fromEncoded(url.encode()))


class SignInDialog(CardDialog):
    """exec(); on success `fetched` holds the account, key and server.

    ``begin`` makes the Flow (service_signin.begin); ``open_url`` opens the browser (both on the UI thread)."""

    def __init__(self, service: str, parent: QWidget | None = None, begin=None, open_url=None,
                 hint: str = "If the browser shows an error, cancel and paste the stream key instead."):
        label = SERVICES[service].label
        super().__init__(f"Sign in to {label}", parent, width=440)  # a rounded card like every pop-up
        self.service = service
        self.fetched: service_signin.Fetched | None = None
        self._cancel = threading.Event()
        self._job = Job()
        self._thread: threading.Thread | None = None
        self._start_error: str | None = None
        layout = self.body
        self.message = QLabel(f"Finish signing in to {label} in your browser.")
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        layout.addWidget(self.message)
        self.hint = QLabel(hint)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        layout.addWidget(self.hint)
        self.hint.setVisible(bool(hint))
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setStyleSheet(theme.button_style(17, "9px 18px"))
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(self.reject)
        row.addWidget(self.cancel_button)
        layout.addSpacing(4)
        layout.addLayout(row)
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._check)
        try:
            flow = (begin or service_signin.begin)(service)
        except SignInError as exc:
            self._start_error = str(exc)
            self._show_error(str(exc))
            return
        opened = (open_url or open_in_browser)(flow.url)
        log.info("sign-in: %s browser %s", service, "opened" if opened is not False else "didn't open")
        if opened is False:
            self.message.setText(f"Your browser didn't open. Cancel and paste the {label} stream key instead."
                                 if hint else "Your browser didn't open. Cancel and try again.")
        self._thread = threading.Thread(target=_work, args=(flow.finish, self._cancel, self._job),
                                        name=f"{service}-sign-in", daemon=True)
        self._thread.start()
        self._timer.start()

    @property
    def error(self) -> str | None:
        """What went wrong, worded for the person, once the sign-in ended without a key."""
        return self._job.error or self._start_error

    def _show_error(self, text: str) -> None:
        self.message.setText(text)
        self.message.setStyleSheet(f"color: {theme.WARN}; background: transparent;")
        self.hint.hide()
        self.cancel_button.setText("Close")

    def _check(self) -> None:
        """On the UI thread: pick up the worker's answer once it's done."""
        if self._thread is None or self._thread.is_alive():
            return
        self._timer.stop()
        if self._job.fetched is not None:
            self.fetched = self._job.fetched
            self.accept()
        elif self._job.error:
            self._show_error(self._job.error)

    def done(self, result: int) -> None:  # noqa: D401 (Qt override)
        """Closing cancels the sign-in and waits a moment for the worker; it stops the loopback server."""
        self._cancel.set()
        self._timer.stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(JOIN_SECONDS)
            if thread.is_alive():  # in a web call: it ends by itself (10 s at most) and touches nothing of ours
                log.info("sign-in: %s worker still finishing a web call after close", self.service)
        super().done(result)


def sign_in(service: str, parent: QWidget | None = None) -> service_signin.Fetched | None:
    dialog = SignInDialog(service, parent)
    try:
        return dialog.fetched if dialog.exec() else None
    finally:
        dialog.deleteLater()  # freed by the UI thread's event loop, never by a worker
