"""Accounts: the one place to sign in and out of Twitch, YouTube and Kick.

One sign-in per platform serves everything that platform offers: the stream key on your destinations,
Stream details (title, category, tags) and chat. Destinations, Stream details and the Chat panel never sign
out themselves; they use whoever is signed in here. Settings → Accounts shows this page, and the same page
opens as a pop-up from Stream details and the Chat panel.

Signing out of YouTube or Kick asks the service to forget SprigCast (a short web call, on a worker). Stream keys
already filled in on your destinations keep working: a key doesn't depend on the sign-in.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import theme
from sprigcast.app.card import PRIMARY_PILL, CardDialog

log = logging.getLogger(__name__)

PLATFORMS = (("twitch", "Twitch"), ("youtube", "YouTube"), ("kick", "Kick"))


def signed_in_as(service: str) -> str:
    """Who is signed in on that platform, "" when nobody. Cheap: QSettings only."""
    if service == "twitch":
        from sprigcast.output import twitch_auth

        try:
            return twitch_auth.remembered_login() or ""
        except Exception:  # noqa: BLE001
            return ""
    from sprigcast.output import stream_details

    return stream_details.account(service)


def offered(service: str) -> bool:
    """Twitch always; YouTube and Kick when this copy of SprigCast has their sign-in set up."""
    if service == "twitch":
        return True
    from sprigcast.output import service_signin

    return service_signin.available(service)


class AccountsPage(QWidget):
    """A row per platform: who's signed in, and Sign in or Sign out."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.rows: dict[str, tuple[QLabel, QPushButton]] = {}
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {theme.WARN}; background: transparent;")
        self.error.hide()
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)
        for service, label in PLATFORMS:
            if not offered(service):
                continue
            row = QHBoxLayout()
            row.setSpacing(12)
            name = QLabel(label)
            name.setFixedWidth(84)
            name.setStyleSheet("background: transparent; font-weight: 600;")
            state = QLabel()
            state.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
            button = QPushButton()
            button.setStyleSheet(PRIMARY_PILL)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumWidth(104)
            button.clicked.connect(lambda _=False, s=service: self._clicked(s))
            row.addWidget(name)
            row.addWidget(state, 1)
            row.addWidget(button)
            box.addLayout(row)
            self.rows[service] = (state, button)
        box.addWidget(self.error)
        box.addStretch(1)
        try:
            from sprigcast.output import twitch_auth

            twitch_auth.account_events().changed.connect(lambda _login: self.refresh())
        except Exception:  # noqa: BLE001
            pass
        self.refresh()

    def refresh(self) -> None:
        for service, (state, button) in self.rows.items():
            name = signed_in_as(service)
            state.setText(f"Signed in as {name}" if name else "Not signed in")
            button.setText("Sign out" if name else "Sign in")

    def _clicked(self, service: str) -> None:
        self.error.hide()
        if signed_in_as(service):
            self.sign_out(service)
        else:
            self.sign_in(service)
        self.refresh()

    def sign_in(self, service: str) -> None:
        from sprigcast.app.modal import modal

        if service == "twitch":
            from sprigcast.app.twitch_connect import TwitchConnectDialog

            modal(TwitchConnectDialog(self.window()))
            return
        from sprigcast.app.signin_dialog import SignInDialog
        from sprigcast.output import service_signin

        dialog = SignInDialog(service, self.window(), begin=lambda s: service_signin.begin(s, keep=True), hint="")
        if not modal(dialog) and dialog.error:
            self.error.setText(dialog.error)
            self.error.show()

    def sign_out(self, service: str) -> None:
        log.info("accounts: %s signed out", service)
        if service == "twitch":
            from sprigcast.output import twitch_auth

            try:
                twitch_auth.disconnect()
            except Exception as exc:  # noqa: BLE001
                self.error.setText(f"Couldn't sign out of Twitch: {exc}")
                self.error.show()
            return
        from sprigcast.output import stream_details

        stream_details._remember(service, "")  # shown at once; the service is told on a worker
        threading.Thread(target=stream_details.sign_out, args=(service,), name="accounts-sign-out",
                         daemon=True).start()


class AccountsDialog(CardDialog):
    """The Accounts page as a pop-up, for Stream details and the Chat panel."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Accounts", parent, width=480)
        self.page = AccountsPage(self)
        self.body.addWidget(self.page)
        close = QPushButton("Close")
        close.setStyleSheet(PRIMARY_PILL)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(close)
        self.body.addLayout(bottom)


def open_accounts(parent: QWidget | None) -> None:
    from sprigcast.app.modal import modal

    modal(AccountsDialog(parent))
