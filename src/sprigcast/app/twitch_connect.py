"""The "Connect Twitch" window: shows the sign-in code and waits while you approve it on twitch.tv."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QFontMetrics, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.output import twitch_auth
from sprigcast.output.twitch_auth import Pending, TwitchAuthError


class TwitchConnectDialog(QDialog):
    """Run exec(); on success `account` is the login name and `stream_key` the fetched key."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Connect Twitch")
        self.setMinimumWidth(440)
        self.account: str | None = None
        self.stream_key: str | None = None
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._job: Future | None = None
        self._code: twitch_auth.DeviceCode | None = None
        self._next_poll = 0

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        self.message = QLabel("Asking Twitch for a sign-in code…")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.code = QLabel("")
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 2.4)
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 4)
        self.code.setFont(font)
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setMinimumHeight(QFontMetrics(font).height() + 24)
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.code)
        row = QHBoxLayout()
        self.open_button = QPushButton("Open twitch.tv/activate")
        self.open_button.clicked.connect(self._open_browser)
        self.open_button.setEnabled(False)
        self.copy_button = QPushButton("Copy code")
        self.copy_button.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.code.text()))
        self.copy_button.setEnabled(False)
        row.addWidget(self.open_button)
        row.addWidget(self.copy_button)
        layout.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: gray")
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._job = self._pool.submit(twitch_auth.start_device_flow)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(250)

    def _open_browser(self) -> None:
        if self._code is not None:
            QDesktopServices.openUrl(QUrl(self._code.verification_uri))

    def _fail(self, text: str) -> None:
        self.timer.stop()
        self.message.setText(text)
        self.status.setText("")
        self.open_button.setEnabled(False)

    def _tick(self) -> None:
        """Runs on the UI thread: collect the background job's result and start the next one."""
        if self._job is not None and not self._job.done():
            return
        job, self._job = self._job, None
        if job is not None:
            try:
                result = job.result()
            except Pending:
                result = None
            except TwitchAuthError as exc:
                self._fail(str(exc))
                return
            except Exception as exc:  # unexpected reply shape, keyring trouble, ...
                self._fail(f"Something went wrong: {exc}")
                return
            if isinstance(result, twitch_auth.DeviceCode):
                self._code = result
                self.code.setText(result.user_code)
                self.message.setText("Approve SprigCast on Twitch. If your browser didn't open, "
                                     "go to twitch.tv/activate and enter:")
                self.status.setText("Waiting for you to approve on Twitch…")
                self.open_button.setEnabled(True)
                self.copy_button.setEnabled(True)
                self._open_browser()
                self._next_poll = result.interval * 4  # ticks are 250 ms
                return
            if isinstance(result, dict):
                self.account = result["login"]
                self.status.setText(f"Connected as {self.account}. Fetching your stream key…")
                self._job = self._pool.submit(twitch_auth.fetch_stream_key)
                return
            if isinstance(result, str):
                self.stream_key = result
                self.timer.stop()
                self.accept()
                return
        if self._code is None:
            return
        self._next_poll -= 1
        if self._next_poll <= 0:
            self._next_poll = self._code.interval * 4
            self._job = self._pool.submit(twitch_auth.poll_device_flow, self._code)

    def done(self, result: int) -> None:  # noqa: D401 (Qt override)
        self.timer.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)
        super().done(result)
