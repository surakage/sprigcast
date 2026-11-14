"""Settings → Connections: let Streamer.bot, Stream Deck and other apps control SprigCast.

The server speaks OBS's WebSocket (version 5), so in those apps you add SprigCast as if it were OBS:
host 127.0.0.1, this port and this password.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.live_stage import heading
from sprigcast.app.control_server import (
    ENABLED,
    LAN,
    NATIVE_PATH,
    OBS_DOOR,
    PASSWORD,
    PORT,
    USE_PASSWORD,
    saved_settings,
)
from sprigcast.control.protocol import new_password

def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.MUTED}")
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


FIELD_STYLE = (  # roomier than the usual Settings field, so the text never hugs the edge
    f"QLineEdit, QSpinBox {{ background: {theme.PANEL}; border: none; border-radius: 12px;"
    f" padding: 9px 16px; min-height: 20px; }}"
    f"QLineEdit:focus, QSpinBox:focus {{ background: {theme.RAISED}; }}"
    "QSpinBox::up-button, QSpinBox::down-button { width: 0px; border: none; }"
)
LABEL_W = 104  # the same label column as the Stream page


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    """A soft rounded card with its title inside, like the Stream page's."""
    card = QFrame()
    card.setObjectName("connectionsCard")
    card.setStyleSheet(f"#connectionsCard {{ background: {theme.CARD}; border-radius: 20px; }}")
    box = QVBoxLayout(card)
    box.setContentsMargins(28, 22, 28, 24)
    box.setSpacing(14)
    box.addWidget(heading(title, 15))
    return card, box


def _row(label: str, *widgets: QWidget, stretch: bool = True) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(10)
    name = QLabel(label)
    name.setFixedWidth(LABEL_W)
    name.setStyleSheet(theme.MUTED_STYLE)
    row.addWidget(name)
    for widget in widgets:
        row.addWidget(widget, 1 if isinstance(widget, QLineEdit) else 0)
    if stretch:
        row.addStretch(1)
    return row


def _pill(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setStyleSheet(theme.PILL_STYLE)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setMinimumWidth(78)
    return button


class ConnectionsPage(QWidget):
    def __init__(self, server=None, parent: QWidget | None = None):
        super().__init__(parent)
        self.server = server
        s = saved_settings()

        main, box = _card("Remote control")
        self.enabled = QCheckBox("Let other apps control SprigCast")
        self.enabled.setChecked(s["enabled"])
        box.addWidget(self.enabled)
        box.addWidget(_hint("Stream tools, control decks and bots can switch blooms, mute sounds and go live."))
        box.addSpacing(6)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setStyleSheet(FIELD_STYLE)
        self.port.setFixedWidth(130)
        self.port.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.port.setValue(s["port"])
        box.addLayout(_row("Port", self.port))
        self.use_password = QCheckBox("Ask for a password")
        self.use_password.setChecked(s["use_password"])
        box.addLayout(_row("", self.use_password))
        self.password = QLineEdit(s["password"])
        self.password.setStyleSheet(FIELD_STYLE)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_password = _pill("Show")
        self.show_password.setCheckable(True)
        self.show_password.toggled.connect(self._reveal)
        self.new_password = _pill("New")
        self.new_password.setToolTip("Make a new password (apps using the old one need the new one)")
        self.new_password.clicked.connect(lambda: self.password.setText(new_password()))
        box.addLayout(_row("Password", self.password, self.show_password, self.new_password, stretch=False))
        self.use_password.toggled.connect(lambda on: [w.setEnabled(on) for w in (self.password, self.show_password, self.new_password)])
        self.use_password.toggled.emit(self.use_password.isChecked())

        devices, dbox = _card("Who can connect")
        self.lan = QCheckBox("Let phones and other computers on my network connect")
        self.lan.setChecked(s["lan"])
        dbox.addWidget(self.lan)
        dbox.addWidget(_hint("Only needed when the app runs on another device. Keep a password on."))
        dbox.addSpacing(6)
        self.obs_door = QCheckBox("Compatibility mode")
        self.obs_door.setChecked(s["obs_door"])
        dbox.addWidget(self.obs_door)
        dbox.addWidget(_hint("For apps that can only connect the way they connect to OBS. "
                             "New features come to SprigCast's own connection first."))

        connect, cbox = _card("Connect an app")
        self.details = QLabel()
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy = _pill("Copy details")
        self.copy.clicked.connect(self._copy)
        top = QHBoxLayout()
        top.setSpacing(16)
        top.addWidget(self.details, 1)
        top.addWidget(self.copy, 0, Qt.AlignmentFlag.AlignTop)
        cbox.addLayout(top)
        self.status = QLabel()
        self.status.setStyleSheet(theme.NOTE_STYLE + " padding: 8px 14px;")
        cbox.addWidget(self.status, 0, Qt.AlignmentFlag.AlignLeft)

        for signal in (self.enabled.toggled, self.use_password.toggled, self.lan.toggled, self.obs_door.toggled):
            signal.connect(self._refresh)
        self.port.valueChanged.connect(self._refresh)
        self.password.textChanged.connect(self._refresh)
        if server is not None:
            server.on_clients_changed = lambda _count: self._refresh()
            self.destroyed.connect(lambda *_: setattr(server, "on_clients_changed", lambda _count: None))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        for card in (main, devices, connect):
            layout.addWidget(card)
        self._refresh()

    def _reveal(self, on: bool) -> None:
        self.password.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        self.show_password.setText("Hide" if on else "Show")

    def connect_text(self) -> str:
        host = "127.0.0.1"
        lines = [f"Address: ws://{host}:{self.port.value()}{NATIVE_PATH}", f"Server: {host}", f"Port: {self.port.value()}"]
        lines.append(f"Password: {self.password.text()}" if self.use_password.isChecked() else "Password: none")
        return "\n".join(lines)

    def _refresh(self, *_args) -> None:
        rows = [line.split(": ", 1) for line in self.connect_text().split("\n")]
        if self.use_password.isChecked() and not self.show_password.isChecked():
            rows[-1][1] = "••••••••"
        self.details.setText(  # a small table: muted names in the label column, values beside
            "<table cellspacing='0' cellpadding='0'>" + "".join(
                f"<tr><td width='{LABEL_W}' style='color:{theme.MUTED}; padding: 3px 0'>{name}</td>"
                f"<td style='padding: 3px 0'>{value}</td></tr>" for name, value in rows) + "</table>")
        server = self.server
        if not self.enabled.isChecked():
            self.status.setText("Off")
        elif server is None or not server.listening:
            error = getattr(server, "error", "")
            self.status.setText(f"Couldn't start: {error}. Try another port." if error else "Starts when you press Apply")
        else:
            count = len(server.sessions)
            apps = "no apps" if count == 0 else "1 app" if count == 1 else f"{count} apps"
            self.status.setText(f"On · {apps} connected")

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
        q.setValue(OBS_DOOR, self.obs_door.isChecked())
        QTimer.singleShot(0, self._refresh)  # the server restarts once Settings is applied
