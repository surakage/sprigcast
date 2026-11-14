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

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QListWidget,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.live_stage import heading
from sprigcast.app.terms import PART_MARK

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
        root.setContentsMargins(20, 6, 20, 16)
        root.setSpacing(14)
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
        card_layout.setContentsMargins(10, 6, 10, 10)
        self.card_slot = card_layout
        body.addWidget(self.card, 1)
        self.right_panel, self.right = side_panel(RIGHT_WIDTH)
        body.addWidget(self.right_panel)
        root.addLayout(body, 1)


# Inside: the bloom's contents, sprigs shown open ---------------------------------------------

ROLE_NAME = Qt.ItemDataRole.UserRole + 20  # the name alone, without the [kind] tag
ROLE_TAG = Qt.ItemDataRole.UserRole + 21  # quiet text on the right: "2 hidden here", "filters", "no signal"
ROLE_SPRIG = Qt.ItemDataRole.UserRole + 22  # True for a sprig placement
ROLE_CHILDREN = Qt.ItemDataRole.UserRole + 23  # [(depth, name, is_sprig)]: what's inside a sprig
ROW_H, CHILD_H, EYE_W = 34, 24, 30


class InsideDelegate(QStyledItemDelegate):
    """Draws an Inside row: a visibility dot, the name (❧ for sprigs), a quiet tag, then the sprig's
    own contents underneath on branch lines, like the mockup."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        children = index.data(ROLE_CHILDREN) or []
        return QSize(option.rect.width(), ROW_H + CHILD_H * len(children) + (4 if children else 0))

    def paint(self, p: QPainter, option, index) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(0, 1, 0, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.SELECT_BG if selected else theme.PANEL))
            p.drawRoundedRect(r, 12, 12)
        top = QRectF(r.x(), r.y(), r.width(), ROW_H - 2)
        shown = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value or \
            index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        eye = QRectF(top.x() + 10, top.center().y() - 5, 10, 10)
        p.setPen(QPen(QColor(theme.ACCENT if shown else theme.MUTED), 1.5))
        p.setBrush(QColor(theme.ACCENT) if shown else Qt.BrushStyle.NoBrush)
        p.drawEllipse(eye)
        name = index.data(ROLE_NAME) or index.data(Qt.ItemDataRole.DisplayRole) or ""
        sprig = bool(index.data(ROLE_SPRIG))
        x = top.x() + EYE_W
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold if sprig or selected else QFont.Weight.Normal)
        p.setFont(font)
        metrics = QFontMetrics(font)
        if sprig:
            p.setPen(QColor(theme.ACCENT))
            p.drawText(QRectF(x, top.y(), 16, top.height()), Qt.AlignmentFlag.AlignVCenter, PART_MARK)
            x += 18
        tag = index.data(ROLE_TAG) or ""
        small = QFont(option.font)
        small.setPixelSize(11)
        small_metrics = QFontMetrics(small)
        cap = int(top.width() * 0.4)  # the name always keeps most of the row
        tag = small_metrics.elidedText(tag, Qt.TextElideMode.ElideRight, cap - 12) if tag else ""
        tag_w = small_metrics.horizontalAdvance(tag) + 12 if tag else 0
        colour = theme.SELECT_TEXT if selected else theme.TEXT if shown else theme.MUTED
        p.setPen(QColor(colour))
        width = int(top.right() - x - tag_w - 8)
        p.drawText(QRectF(x, top.y(), width, top.height()), Qt.AlignmentFlag.AlignVCenter,
                   metrics.elidedText(name, Qt.TextElideMode.ElideRight, width))
        if tag:
            p.setFont(small)
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(top.right() - tag_w - 4, top.y(), tag_w, top.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, tag)
        children = index.data(ROLE_CHILDREN) or []
        y = top.bottom()
        p.setFont(QFont(option.font))
        for depth, child, child_sprig in children:
            indent = top.x() + 24 + depth * 16
            p.setPen(QPen(QColor(theme.EDGE_STRONG), 2))
            p.drawLine(int(indent), int(y), int(indent), int(y + CHILD_H))
            tx = indent + 12
            if child_sprig:
                p.setPen(QColor(theme.ACCENT))
                p.drawText(QRectF(tx, y, 16, CHILD_H), Qt.AlignmentFlag.AlignVCenter, PART_MARK)
                tx += 18
            p.setPen(QColor(theme.MUTED if not selected else theme.SELECT_TEXT))
            p.drawText(QRectF(tx, y, top.right() - tx - 6, CHILD_H), Qt.AlignmentFlag.AlignVCenter, child)
            y += CHILD_H
        p.restore()

    def editorEvent(self, event, model, option, index) -> bool:  # noqa: N802
        """A click on the dot shows or hides the thing (like the old checkbox)."""
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            if pos.x() - option.rect.x() < EYE_W and pos.y() - option.rect.y() < ROW_H:
                state = index.data(Qt.ItemDataRole.CheckStateRole)
                on = state in (Qt.CheckState.Checked, Qt.CheckState.Checked.value)
                model.setData(index, Qt.CheckState.Unchecked if on else Qt.CheckState.Checked,
                              Qt.ItemDataRole.CheckStateRole)
                return True
        return False


class InsideList(QListWidget):
    """The Inside list: drag a row to put it in front of or behind the others."""

    moved = Signal(int, int)  # from row, to row (rows are top-most first)

    def __init__(self):
        super().__init__()
        self.setItemDelegate(InsideDelegate(self))
        self.setMouseTracking(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSpacing(1)

    def dropEvent(self, event) -> None:  # noqa: N802
        """Work out where the row was dropped and let the window reorder the bloom (it rebuilds the list)."""
        source = self.currentRow()
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            target = self.count() - 1
        else:
            target = index.row()
            below = self.dropIndicatorPosition() == QAbstractItemView.DropIndicatorPosition.BelowItem
            if below and target < source:
                target += 1
            elif not below and target > source:
                target -= 1
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        if source >= 0 and target != source:
            self.moved.emit(source, max(0, min(self.count() - 1, target)))
