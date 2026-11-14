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
    WEB_DOOR,
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


def _stacked(name: str, field) -> QVBoxLayout:
    """A small name over its field, so the parts of one address read left to right."""
    col = QVBoxLayout()
    col.setSpacing(6)
    label = QLabel(name)
    label.setObjectName("fieldName")
    col.addWidget(label)
    if isinstance(field, QWidget):
        col.addWidget(field)
    else:
        col.addLayout(field)
    return col


def _joint() -> QVBoxLayout:
    """The ':' between server and port (and port and password): one address in parts."""
    joint = QLabel(":")
    joint.setAlignment(Qt.AlignmentFlag.AlignCenter)
    joint.setFixedSize(22, FIELD_H)
    joint.setStyleSheet(f"color: {theme.ACCENT}; font-size: 20px; font-weight: 700;")
    col = QVBoxLayout()
    col.setSpacing(6)
    col.addStretch(1)
    col.addWidget(joint)
    return col


FIELD_H = 40  # server, port and password share one height so the address reads as one line


def _value_chip(text: str) -> QLineEdit:
    """A read-only field that looks exactly like its editable neighbours."""
    chip = QLineEdit(text)
    chip.setReadOnly(True)
    chip.setStyleSheet(FIELD_STYLE.replace(theme.PANEL, theme.RAISED))
    chip.setFixedSize(124, FIELD_H)
    chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return chip


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

        # One connected block: the address apps type in, with the port and password that make it up.
        block = QFrame()
        block.setObjectName("connBlock")
        block.setStyleSheet(f"#connBlock {{ background: {theme.PANEL}; border-radius: 18px; }}"
                            f"#connBlock QLabel#fieldName {{ color: {theme.MUTED}; font-size: 12px; }}")
        bl = QVBoxLayout(block)
        bl.setContentsMargins(20, 16, 20, 18)
        bl.setSpacing(14)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        top.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.address = QLabel()
        self.address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.address.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-family: Consolas, 'Cascadia Mono', monospace;"
                                   " font-size: 14px;")
        top.addWidget(self.address, 1)
        self.copy = _pill("Copy")
        self.copy.setToolTip("Copy the address, port and password for the app you're connecting")
        self.copy.clicked.connect(self._copy)
        top.addWidget(self.copy)
        bl.addLayout(top)

        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setStyleSheet(FIELD_STYLE.replace(theme.PANEL, theme.RAISED))
        self.port.setFixedSize(110, FIELD_H)
        self.port.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.port.setValue(s["port"])
        self.password = QLineEdit(s["password"])
        self.password.setStyleSheet(FIELD_STYLE.replace(theme.PANEL, theme.RAISED))
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setFixedHeight(FIELD_H)
        self.show_password = _pill("Show")
        self.show_password.setCheckable(True)
        self.show_password.toggled.connect(self._reveal)
        self.new_password = _pill("New")
        self.new_password.setToolTip("Make a new password (apps using the old one need the new one)")
        self.new_password.clicked.connect(lambda: self.password.setText(new_password()))
        fields = QHBoxLayout()
        fields.setSpacing(0)
        fields.addLayout(_stacked("Server", _value_chip("127.0.0.1")))
        fields.addLayout(_joint())
        fields.addLayout(_stacked("Port", self.port))
        fields.addLayout(_joint())
        pw = QHBoxLayout()
        pw.setSpacing(8)
        pw.addWidget(self.password, 1)
        pw.addWidget(self.show_password)
        pw.addWidget(self.new_password)
        fields.addLayout(_stacked("Password", pw), 1)
        bl.addLayout(fields)
        self.use_password = QCheckBox("Ask for a password")
        self.use_password.setChecked(s["use_password"])
        bl.addWidget(self.use_password)
        self.status = QLabel()
        self.status.setStyleSheet(theme.MUTED_STYLE)
        bl.addWidget(self.status)
        box.addSpacing(4)
        box.addWidget(block)
        self.use_password.toggled.connect(lambda on: [w.setEnabled(on) for w in (self.password, self.show_password, self.new_password)])
        self.use_password.toggled.emit(self.use_password.isChecked())
        self.enabled.toggled.connect(block.setEnabled)
        block.setEnabled(self.enabled.isChecked())

        devices, dbox = _card("Who can connect")
        self.lan = QCheckBox("Let phones and other computers on my network connect")
        self.lan.setChecked(s["lan"])
        dbox.addWidget(self.lan)
        dbox.addWidget(_hint("Only needed when the app runs on another device. Keep a password on."))
        dbox.addSpacing(6)
        self.web_door = QCheckBox("Allow web links")
        self.web_door.setChecked(s["web_door"])
        dbox.addWidget(self.web_door)
        dbox.addWidget(_hint("For buttons and scripts that can only open a link. Always needs the password."))
        dbox.addSpacing(6)
        self.obs_door = QCheckBox("Compatibility mode")
        self.obs_door.setChecked(s["obs_door"])
        dbox.addWidget(self.obs_door)
        dbox.addWidget(_hint("For apps that can only connect the way they connect to OBS. "
                             "New features come to SprigCast's own connection first."))

        for signal in (self.enabled.toggled, self.use_password.toggled, self.lan.toggled, self.obs_door.toggled,
                       self.web_door.toggled):
            signal.connect(self._refresh)
        self.port.valueChanged.connect(self._refresh)
        self.password.textChanged.connect(self._refresh)
        if server is not None:
            server.on_clients_changed = lambda _count: self._refresh()
            self.destroyed.connect(lambda *_: setattr(server, "on_clients_changed", lambda _count: None))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        for card in (main, devices):
            layout.addWidget(card)
        self._refresh()

    def _reveal(self, on: bool) -> None:
        self.password.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password)
        self.show_password.setText("Hide" if on else "Show")

    def connect_text(self) -> str:
        host = "127.0.0.1"
        lines = [f"Address: ws://{host}:{self.port.value()}{NATIVE_PATH}", f"Server: {host}", f"Port: {self.port.value()}"]
        lines.append(f"Password: {self.password.text()}" if self.use_password.isChecked() else "Password: none")
        if self.web_door.isChecked():
            lines.insert(1, f"Web links: http://{host}:{self.port.value()}/api")
        return "\n".join(lines)

    def _refresh(self, *_args) -> None:
        self.address.setText(f"ws://127.0.0.1:{self.port.value()}{NATIVE_PATH}")
        server = self.server
        on = self.enabled.isChecked() and server is not None and server.listening
        self.dot.setStyleSheet(f"background: {theme.ACCENT if on else theme.MUTED}; border-radius: 5px;")
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
        QTimer.singleShot(1500, lambda: self.copy.setText("Copy"))

    def apply(self) -> None:
        q = QSettings()
        q.setValue(ENABLED, self.enabled.isChecked())
        q.setValue(PORT, self.port.value())
        q.setValue(USE_PASSWORD, self.use_password.isChecked())
        q.setValue(PASSWORD, self.password.text().strip() or new_password())
        q.setValue(LAN, self.lan.isChecked())
        q.setValue(OBS_DOOR, self.obs_door.isChecked())
        q.setValue(WEB_DOOR, self.web_door.isChecked())
        QTimer.singleShot(0, self._refresh)  # the server restarts once Settings is applied
