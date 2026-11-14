"""Settings → Connections: let Streamer.bot, Stream Deck and other apps control SprigCast.

The server speaks OBS's WebSocket (version 5), so in those apps you add SprigCast as if it were OBS:
host 127.0.0.1, this port and this password.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.control_server import ENABLED, LAN, PASSWORD, PORT, USE_PASSWORD, saved_settings
from sprigcast.control.protocol import new_password

APPS = "Streamer.bot, Speaker.bot, Stream Deck, SAMMI, Touch Portal"


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.MUTED}")
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class ConnectionsPage(QWidget):
    def __init__(self, server=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.server = server
        s = saved_settings()

        main = QGroupBox("Remote control")
        form = QFormLayout(main)
        self.enabled = QCheckBox("Let other apps control SprigCast")
        self.enabled.setChecked(s["enabled"])
        form.addRow("", self.enabled)
        form.addRow("", _hint(f"For {APPS} and anything else made for OBS's WebSocket (version 5). "
                              "In those apps, add SprigCast as if it were OBS."))
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(s["port"])
        form.addRow("Port", self.port)

        self.use_password = QCheckBox("Ask for a password")
        self.use_password.setChecked(s["use_password"])
        form.addRow("", self.use_password)
        self.password = QLineEdit(s["password"])
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_password = QPushButton("Show")
        self.show_password.setCheckable(True)
        self.show_password.toggled.connect(self._reveal)
        self.new_password = QPushButton("New")
        self.new_password.setToolTip("Make a new password (apps using the old one need the new one)")
        self.new_password.clicked.connect(lambda: self.password.setText(new_password()))
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self.password, 1)
        for button in (self.show_password, self.new_password):
            button.setStyleSheet(theme.PILL_STYLE)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(button)
        form.addRow("Password", row)
        self.use_password.toggled.connect(lambda on: [w.setEnabled(on) for w in (self.password, self.show_password, self.new_password)])
        self.use_password.toggled.emit(self.use_password.isChecked())

        self.lan = QCheckBox("Let phones and other computers on my network connect")
        self.lan.setChecked(s["lan"])
        form.addRow("", self.lan)
        form.addRow("", _hint("Leave this off unless an app runs on another device, like Touch Portal on a phone. "
                              "Keep a password on when it's on."))

        connect = QGroupBox("Connect an app")
        box = QVBoxLayout(connect)
        self.details = QLabel()
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy = QPushButton("Copy details")
        self.copy.setStyleSheet(theme.PILL_STYLE)
        self.copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy.clicked.connect(self._copy)
        top = QHBoxLayout()
        top.addWidget(self.details, 1)
        top.addWidget(self.copy, 0, Qt.AlignmentFlag.AlignTop)
        box.addLayout(top)
        self.status = _hint("")
        box.addWidget(self.status)
        box.addWidget(_hint(
            "Streamer.bot: Stream Apps → OBS → Add. Speaker.bot: Integrations → OBS. "
            "Stream Deck: the OBS Studio plugin's Server settings. If OBS is open too, give SprigCast "
            "another port (like 4456) so they don't clash."))
        box.addWidget(_hint(
            "Blooms show up as scenes and sprigs as nested scenes; switching a bloom switches its "
            "vertical twin too. SprigCast's extras (Bloom it, clips, sprig states, one canvas at a time) "
            "are vendor requests named \"sprigcast\"."))

        for signal in (self.enabled.toggled, self.use_password.toggled, self.lan.toggled):
            signal.connect(self._refresh)
        self.port.valueChanged.connect(self._refresh)
        self.password.textChanged.connect(self._refresh)
        if server is not None:
            server.on_clients_changed = lambda _count: self._refresh()
            self.destroyed.connect(lambda *_: setattr(server, "on_clients_changed", lambda _count: None))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        layout.addWidget(main)
        layout.addWidget(connect)
        self._refresh()

    def _reveal(self, on: bool) -> None:
        self.password.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        self.show_password.setText("Hide" if on else "Show")

    def connect_text(self) -> str:
        host = "127.0.0.1"
        lines = [f"Server: {host}", f"Port: {self.port.value()}"]
        lines.append(f"Password: {self.password.text()}" if self.use_password.isChecked() else "Password: none")
        return "\n".join(lines)

    def _refresh(self, *_args) -> None:
        shown = self.connect_text()
        if self.use_password.isChecked() and not self.show_password.isChecked():
            shown = shown.rsplit("\n", 1)[0] + "\nPassword: ••••••••"
        self.details.setText(shown)
        server = self.server
        if not self.enabled.isChecked():
            self.status.setText("Off: no app can connect.")
        elif server is None or not server.listening:
            error = getattr(server, "error", "")
            self.status.setText(f"Couldn't listen: {error}. Try another port." if error else "Starts when you press Apply.")
        else:
            count = len(server.sessions)
            apps = "no apps" if count == 0 else "1 app" if count == 1 else f"{count} apps"
            self.status.setText(f"On, port {server.port}: {apps} connected.")

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.connect_text())
        self.copy.setText("Copied")

    def apply(self) -> None:
        q = QSettings()
        q.setValue(ENABLED, self.enabled.isChecked())
        q.setValue(PORT, self.port.value())
        q.setValue(USE_PASSWORD, self.use_password.isChecked())
        q.setValue(PASSWORD, self.password.text().strip() or new_password())
        q.setValue(LAN, self.lan.isChecked())
        QTimer.singleShot(0, self._refresh)  # the server restarts once Settings is applied
