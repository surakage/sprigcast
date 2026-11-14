"""Build mode's own screen: bloom pills on top, what's inside on the left, the canvas in the middle
and the inspector on the right.

    [Just Chatting] [Gaming] [Starting soon] [+]                       ❧ Sprigs  Link  Safe zones
    ┌ Inside ─┐ ┌ canvas ───────────────────────────┐ ┌ Facecam ─────┐
    │ Frame   │ │  the shape you edit      [twin]   │ │ Place  X  Y  │
    │ Chat    │ │                                   │ │ Look   …     │
    └─────────┘ └───────────────────────────────────┘ └──────────────┘

The big canvas is the shape being edited. The other shape sits beside it as a small "twin" you
can't edit by accident: click it to swap. Whatever you select is outlined in both, but moving it in
one shape never moves it in the other. The main window builds the widgets that go in the slots.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.live_stage import heading

LEFT_WIDTH = 240
RIGHT_WIDTH = 290
SWATCH = (44, 25)


@dataclass(frozen=True)
class BloomPill:
    id: str
    name: str
    colour: str | None
    selected: bool
    on_air: bool


def swatch(colour: str | None, image: QImage | None) -> QIcon:
    """A little rounded picture of a bloom: its last on-air frame, or its folder colour."""
    w, h = SWATCH
    pixmap = QPixmap(w * 2, h * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
    p.setClipPath(path)
    p.fillRect(QRectF(0, 0, w, h), QColor(colour or theme.STAGE_EMPTY))
    if image is not None and not image.isNull():
        size = QSize(image.width(), image.height()).scaled(QSize(w, h), Qt.AspectRatioMode.KeepAspectRatioByExpanding)
        p.drawImage(QRectF((w - size.width()) / 2, (h - size.height()) / 2, size.width(), size.height()), image)
    p.end()
    return QIcon(pixmap)


class BloomBar(QWidget):
    """One pill per bloom on the shape being edited; click to edit it. Tools sit on the right."""

    def __init__(self, on_pick: Callable[[str], None], add_menu: QMenu):
        super().__init__()
        self.on_pick = on_pick
        self.pills: dict[str, QPushButton] = {}
        self._shape: list | None = None
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.scroll.setFixedHeight(46)
        inner = QWidget()
        self.pill_row = QHBoxLayout(inner)
        self.pill_row.setContentsMargins(0, 0, 0, 0)
        self.pill_row.setSpacing(8)
        self.pill_row.addStretch(1)
        self.scroll.setWidget(inner)
        row.addWidget(self.scroll, 1)
        self.add = QPushButton("+")
        self.add.setFixedSize(36, 36)
        self.add.setToolTip("New bloom, folder or sprig")
        self.add.setStyleSheet(
            f"QPushButton {{ background: {theme.PANEL}; color: {theme.MUTED}; border: none; border-radius: 18px;"
            f" font-size: 17px; padding: 0; }}"
            f"QPushButton:hover {{ background: {theme.RAISED}; color: {theme.TEXT}; }}"
            "QPushButton::menu-indicator { width: 0; }")
        self.add.setMenu(add_menu)
        self.pill_row.insertWidget(0, self.add)
        self.tools = QHBoxLayout()  # sprigs, link and safe zones (filled by the main window)
        self.tools.setSpacing(6)
        row.addLayout(self.tools)

    def show_blooms(self, pills: list[BloomPill], thumbs: dict[str, QImage]) -> None:
        shape = [(b.id, b.name, b.colour, id(thumbs.get(b.id))) for b in pills]
        if shape != self._shape:
            self._shape = shape
            for pill in self.pills.values():
                self.pill_row.removeWidget(pill)
                pill.setParent(None)
                pill.deleteLater()
            self.pills = {}
            for i, bloom in enumerate(pills):
                pill = QPushButton(bloom.name)
                pill.setCheckable(True)
                pill.setIcon(swatch(bloom.colour, thumbs.get(bloom.id)))
                pill.setIconSize(QSize(*SWATCH))
                pill.setCursor(Qt.CursorShape.PointingHandCursor)
                pill.setStyleSheet(theme.BLOOM_PILL_STYLE)
                pill.clicked.connect(lambda _=False, sid=bloom.id: self.on_pick(sid))
                self.pill_row.insertWidget(i, pill)
                self.pills[bloom.id] = pill
        for bloom in pills:
            pill = self.pills[bloom.id]
            pill.setChecked(bloom.selected)
            pill.setToolTip(bloom.name + ("  ·  on air" if bloom.on_air else ""))
            pill.setText(bloom.name + ("  ●" if bloom.on_air else ""))


def side_panel(width: int) -> tuple[QWidget, QVBoxLayout]:
    panel = QWidget()
    panel.setFixedWidth(width)
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    return panel, layout


class BuildStage(QWidget):
    """The whole Build screen. `left`, `card` and `right` are filled by the main window."""

    def __init__(self, bar: BloomBar):
        super().__init__()
        self.setObjectName("buildStage")
        self.setStyleSheet(f"#buildStage {{ background: {theme.CHROME}; }}")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 8, 24, 20)
        root.setSpacing(18)
        self.bar = bar
        root.addWidget(bar)
        body = QHBoxLayout()
        body.setSpacing(24)
        self.left_panel, self.left = side_panel(LEFT_WIDTH)
        top = QHBoxLayout()
        top.addWidget(heading("Inside", 16), 1)
        self.add_slot = QHBoxLayout()
        top.addLayout(self.add_slot)
        self.left.addLayout(top)
        body.addWidget(self.left_panel)
        self.card = QFrame()
        self.card.setObjectName("buildCard")
        self.card.setStyleSheet(f"#buildCard {{ background: {theme.PANEL}; border: none; border-radius: 24px; }}"
                                "#buildCard QSplitter::handle { background: transparent; }")
        self.card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(16, 10, 16, 16)
        self.card_slot = card_layout
        body.addWidget(self.card, 1)
        self.right_panel, self.right = side_panel(RIGHT_WIDTH)
        body.addWidget(self.right_panel)
        root.addLayout(body, 1)
