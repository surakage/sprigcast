"""The small "finish signing in in your browser" window for YouTube and Kick (output/service_signin.py)."""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app.modal import modal
from sprigcast.app import theme
from sprigcast.output import service_signin
from sprigcast.output.oauth_loopback import Cancelled, SignInError
from sprigcast.output.stream import SERVICES


class SignInDialog(QDialog):
    """exec(); on success `fetched` holds the account, key and server."""

    def __init__(self, service: str, parent: QWidget | None = None, run=None):
        super().__init__(parent)
        label = SERVICES[service].label
        self.setWindowTitle(f"Sign in to {label}")
        self.setMinimumWidth(420)
        self.fetched: service_signin.Fetched | None = None
        self._error: str | None = None
        self._cancel = threading.Event()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        self.message = QLabel(f"Finish signing in to {label} in your browser.")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setStyleSheet(theme.SMALL_PILL_STYLE)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        layout.addLayout(row)
        work = run or service_signin.SIGN_IN[service]

        def go() -> None:
            try:
                self.fetched = work(self._cancel)
            except Cancelled:
                pass
            except SignInError as exc:
                self._error = str(exc)
            except Exception as exc:  # noqa: BLE001 (worded for the person, logged by the caller)
                self._error = f"Sign-in failed: {exc}"

        self._thread = threading.Thread(target=go, name=f"{service}-sign-in", daemon=True)
        self._thread.start()
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._check)
        self._timer.start()

    def _check(self) -> None:
        if self._thread.is_alive():
            return
        self._timer.stop()
        if self.fetched is not None:
            self.accept()
        elif self._error:
            self.message.setText(self._error)
            self.message.setStyleSheet(f"color: {theme.WARN}")

    def done(self, result: int) -> None:  # noqa: D401 (Qt override)
        self._cancel.set()
        self._timer.stop()
        super().done(result)


def sign_in(service: str, parent: QWidget | None = None) -> service_signin.Fetched | None:
    dialog = SignInDialog(service, parent)
    return dialog.fetched if modal(dialog) else None
