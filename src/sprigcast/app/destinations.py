"""The Destinations panel: one card per place you stream to, with its on/off switch and live health.

Replaces the "Stream to …" checkboxes. A card shows what goes where (service, canvas, size or
Enhanced), whether it is ready (key, server, canvas), and while live its bitrate, drops and health.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.core.scene import Output, SceneCollection
from sprigcast.output.stream import SERVICES

HEALTH_COLORS = {"good": "#3ddc84", "warning": "#ffd23f", "bad": "#ff5a5a", "idle": "#5f9c7c", "off": "#1f5236"}


def what_goes(output: Output, collection: SceneCollection) -> str:
    """"horizontal 1920×1080 · 60 fps" or "Enhanced · horizontal + vertical"."""
    if output.service == "twitch" and output.enhanced:
        return "Enhanced · " + ("horizontal + vertical" if output.enhanced_vertical else output.canvas)
    canvas = next((c for c in collection.canvases if c.id == output.canvas), None)
    w, h = output.size or ((canvas.width, canvas.height) if canvas else (0, 0))
    fps = output.fps or (canvas.fps if canvas else 0)
    return f"{output.canvas} · {w}×{h} · {fps} fps · {output.bitrate_kbps} kbps"


def problem(output: Output, collection: SceneCollection, has_key: Callable[[str | None], bool]) -> str | None:
    """Why this destination can't go live, in a few words, or None when it's ready."""
    service = SERVICES.get(output.service or "custom", SERVICES["custom"])
    if not (service.server or output.server):
        return "Needs a server URL"
    if not (has_key(output.key_ref) or (output.service == "twitch" and output.twitch_account)):
        return "Needs a stream key"
    canvas = next((c for c in collection.canvases if c.id == output.canvas), None)
    if canvas is None or not canvas.enabled:
        return f"The {output.canvas} canvas is off"
    return None


class DestinationCard(QFrame):
    def __init__(self, output: Output, on_toggle: Callable[[str, bool], None], on_edit: Callable[[str], None]):
        super().__init__()
        self.output_id = output.id
        self.setObjectName("destinationCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        shadow = QGraphicsDropShadowEffect(self)  # lifts the card off the dock
        shadow.setBlurRadius(14)
        shadow.setOffset(0, 3)
        shadow.setColor(QColor(0, 0, 0, 140))
        self.setGraphicsEffect(shadow)
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(2)
        top = QHBoxLayout()
        self.dot = QLabel("●")
        self.name = QLabel()
        font = self.name.font()
        font.setBold(True)
        self.name.setFont(font)
        self.switch = QCheckBox("On")
        self.switch.setToolTip("Go live here when you press Go live")
        self.switch.toggled.connect(lambda on: on_toggle(self.output_id, on))
        edit = QToolButton()
        edit.setText("⚙")
        edit.setAutoRaise(True)
        edit.setToolTip("Edit this destination")
        edit.clicked.connect(lambda: on_edit(self.output_id))
        top.addWidget(self.dot)
        top.addWidget(self.name, 1)
        top.addWidget(self.switch)
        top.addWidget(edit)
        root.addLayout(top)
        self.what = QLabel()
        self.what.setStyleSheet("color: #8cc4a3")
        root.addWidget(self.what)
        self.state = QLabel()
        self.state.setWordWrap(True)
        root.addWidget(self.state)

    def show_output(self, output: Output, collection: SceneCollection, issue: str | None) -> None:
        service = SERVICES.get(output.service or "custom", SERVICES["custom"])
        name = service.label
        if output.bandwidth_test and service.bandwidth_test:
            name += "  (test)"
        self.name.setText(name)
        self.what.setText(what_goes(output, collection))
        self.switch.blockSignals(True)
        self.switch.setChecked(output.enabled)
        self.switch.blockSignals(False)
        if issue:
            self.set_state("bad" if output.enabled else "off", issue)
        else:
            self.set_state("idle" if output.enabled else "off", "Ready" if output.enabled else "Off")

    def set_state(self, level: str, text: str) -> None:
        self.dot.setStyleSheet(f"color: {HEALTH_COLORS.get(level, HEALTH_COLORS['idle'])}")
        self.state.setText(text)
        self.setProperty("level", level)


class DestinationsPanel(QWidget):
    """Cards for every stream output. The main window calls show_outputs() after changes and
    show_live() about once a second while streaming."""

    def __init__(self, on_toggle: Callable[[str, bool], None], on_edit: Callable[[str | None], None],
                 has_key: Callable[[str | None], bool]):
        super().__init__()
        self.on_toggle, self.on_edit, self.has_key = on_toggle, on_edit, has_key
        self.cards: dict[str, DestinationCard] = {}
        self.live = False
        self.setStyleSheet(
            "#destinationCard { border: 1px solid #1f5236; border-top-color: #2c7a4f; border-radius: 10px;"
            " background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #173d28, stop:1 #10291b); }"
        )
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._cards_box = QVBoxLayout()
        self._cards_box.setSpacing(6)
        self._layout.addLayout(self._cards_box)
        self.empty = QLabel("Nowhere to stream yet. Add Twitch, YouTube, TikTok or any RTMP server.")
        self.empty.setWordWrap(True)
        self._cards_box.addWidget(self.empty)
        self.add_button = QPushButton("+ Add destination")
        self.add_button.clicked.connect(lambda: self.on_edit(None))
        self._layout.addWidget(self.add_button)
        self._layout.addStretch(1)

    def show_outputs(self, collection: SceneCollection) -> None:
        outputs = [o for o in collection.outputs if o.type == "stream"]
        if [o.id for o in outputs] != list(self.cards):
            for card in self.cards.values():
                self._cards_box.removeWidget(card)
                card.setParent(None)
                card.deleteLater()
            self.cards = {}
            for output in outputs:
                card = DestinationCard(output, self.on_toggle, lambda oid: self.on_edit(oid))
                self.cards[output.id] = card
                self._cards_box.addWidget(card)
        self.empty.setVisible(not outputs)
        for output in outputs:
            self.cards[output.id].show_output(output, collection, problem(output, collection, self.has_key))
        self.set_live(self.live)

    def set_live(self, live: bool) -> None:
        """While live, switches are locked: a destination joins or leaves only between streams."""
        self.live = live
        for card in self.cards.values():
            card.switch.setEnabled(not live)
        self.add_button.setEnabled(not live)

    def show_live(self, states: dict[str, tuple[str, str]]) -> None:
        """states: output id -> (health level, one-line text) for destinations that are streaming."""
        for output_id, card in self.cards.items():
            if output_id in states:
                card.set_state(*states[output_id])
            elif card.switch.isChecked():
                card.set_state("off", "Not in this stream")


def live_line(st) -> tuple[str, str]:
    """(level, text) for one streamer, for its card."""
    level, reason = st.health()
    if not st.connected:
        return "warning", st.status.capitalize()
    text = f"{st.measure_kbps():.0f} kbps · {st.out_width}×{st.out_height} {st.fps} fps"
    if reason != "live":
        text += f" · {reason}"
    if st.dropped:
        text += f" · {st.dropped} dropped"
    if st.reconnects:
        text += f" · {st.reconnects} reconnects"
    return level, text

