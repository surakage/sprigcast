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
    LOCAL_KEY,
    NATIVE_PATH,
    OBS_DOOR,
    PASSWORD,
    PORT,
    USE_PASSWORD,
    WEB_DOOR,
    save_secret,
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

        pair_card = self._pair_card()

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
        dbox.addWidget(_hint("Using THSV StreamBridge or Streamer.bot? Keep this on."))

        for signal in (self.enabled.toggled, self.use_password.toggled, self.lan.toggled, self.obs_door.toggled,
                       self.web_door.toggled):
            signal.connect(self._refresh)
        self.port.valueChanged.connect(self._refresh)
        self.password.textChanged.connect(self._refresh)
        if server is not None:
            import weakref

            import shiboken6

            page = weakref.ref(self)

            def clients_changed(_count) -> None:
                # Settings closed and deleted this page: the server outlives it (a "destroyed" hook didn't
                # always run first, and the next app connecting hit a deleted spin box).
                alive = page()
                if alive is not None and shiboken6.isValid(alive):
                    alive._refresh()

            server.on_clients_changed = clients_changed

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        for card in (main, pair_card, devices):
            layout.addWidget(card)
        self._saved_password = s["password"]
        self._refresh()

    # Pair a device ---------------------------------------------------------------------

    def _pair_card(self) -> QFrame:
        """Pair a device: a 6-digit code for 2 minutes instead of the password, and the devices paired so far."""
        card, box = _card("Paired devices")
        line = QHBoxLayout()
        line.setSpacing(16)
        self.pair_button = _pill("Pair a device")
        self.pair_button.clicked.connect(self._pair)
        line.addWidget(self.pair_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.pair_code = QLabel()
        self.pair_code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.pair_code.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-family: Consolas, 'Cascadia Mono', monospace;"
                                     " font-size: 26px; font-weight: 700; letter-spacing: 4px;")
        self.pair_code.hide()
        line.addWidget(self.pair_code, 0, Qt.AlignmentFlag.AlignVCenter)
        self.pair_note = QLabel()
        self.pair_note.setStyleSheet(theme.MUTED_STYLE)
        line.addWidget(self.pair_note, 1, Qt.AlignmentFlag.AlignVCenter)
        box.addLayout(line)
        self.device_rows = QVBoxLayout()
        self.device_rows.setSpacing(8)
        box.addLayout(self.device_rows)
        self._pair_timer = QTimer(self, interval=1000)
        self._pair_timer.timeout.connect(self._pair_tick)
        self._paired_count = len(self._pairing.devices) if self._pairing else 0
        self._fill_devices()
        return card

    @property
    def _pairing(self):
        return getattr(self.server, "pairing", None)

    def _pair(self) -> None:
        if self._pairing is None:
            return
        if self._pairing.locked_seconds():
            self._pair_tick()
            return
        code = self._pairing.start()
        self._paired_count = len(self._pairing.devices)
        self.pair_code.setText(f"{code[:3]} {code[3:]}")
        self.pair_code.show()
        self.pair_button.setText("New code")
        self._pair_timer.start()
        self._pair_tick()

    def _pair_tick(self) -> None:
        pairing = self._pairing
        if pairing is None:
            return
        if len(pairing.devices) > self._paired_count:  # an app used the code
            self._paired_count = len(pairing.devices)
            self._fill_devices()
            self._pair_done(f"Paired with {pairing.devices[-1].name}")
            return
        locked = pairing.locked_seconds()
        if locked:
            self._pair_done(f"Too many wrong codes · try again in {locked} s")
            return
        left = pairing.seconds_left()
        if not left:
            self._pair_done("Code ran out")
            return
        self.pair_note.setText(f"Type it in the app · {left // 60}:{left % 60:02d}")

    def _pair_done(self, note: str) -> None:
        self._pair_timer.stop()
        self.pair_code.hide()
        self.pair_button.setText("Pair a device")
        self.pair_note.setText(note)

    def _fill_devices(self) -> None:
        while self.device_rows.count():
            item = self.device_rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        pairing = self._pairing
        self.pair_button.setEnabled(pairing is not None)
        devices = sorted(pairing.devices, key=lambda d: d.name.lower()) if pairing else []
        if not devices:
            none = QLabel("None yet")
            none.setStyleSheet(theme.MUTED_STYLE)
            self.device_rows.addWidget(none)
        for device in devices:
            row = QFrame()
            row.setObjectName("deviceRow")
            row.setStyleSheet(f"#deviceRow {{ background: {theme.PANEL}; border-radius: 14px; }}")
            line = QHBoxLayout(row)
            line.setContentsMargins(18, 8, 10, 8)
            line.setSpacing(12)
            name = QLabel(device.name)
            name.setStyleSheet(f"color: {theme.TEXT_BRIGHT};")
            line.addWidget(name, 1)
            added = QLabel(device.added)
            added.setStyleSheet(theme.MUTED_STYLE)
            line.addWidget(added)
            remove = _pill("Remove")
            remove.setToolTip("It can't connect any more (pair it again to bring it back)")
            remove.clicked.connect(lambda _=False, d=device.id: self._remove_device(d))
            line.addWidget(remove)
            self.device_rows.addWidget(row)

    def _remove_device(self, device_id: str) -> None:
        pairing = self._pairing
        key = next((d.key for d in pairing.devices if d.id == device_id), "") if pairing else ""
        if pairing is not None and pairing.remove(device_id):
            self.server.drop_key(key)
            self._paired_count = len(self._pairing.devices)
            self._fill_devices()

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
        QTimer.singleShot(1500, self.copy, lambda: self.copy.setText("Copy"))  # not after Settings closes

    def apply(self) -> None:
        q = QSettings()
        q.setValue(ENABLED, self.enabled.isChecked())
        q.setValue(PORT, self.port.value())
        q.setValue(USE_PASSWORD, self.use_password.isChecked())
        password = self.password.text().strip() or new_password()
        save_secret(PASSWORD, password)  # encrypted for this Windows user
        if password != self._saved_password:  # apps on this PC get a new key with the new password
            q.remove(LOCAL_KEY)
            self._saved_password = password
        q.setValue(LAN, self.lan.isChecked())
        q.setValue(OBS_DOOR, self.obs_door.isChecked())
        q.setValue(WEB_DOOR, self.web_door.isChecked())
        QTimer.singleShot(0, self, self._refresh)  # the server restarts once Settings is applied
