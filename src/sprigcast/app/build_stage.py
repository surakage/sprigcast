"""Build mode's own screen: every bloom and sprig on the left, the canvas in the middle, and what's
inside the picked one on the right with the inspector under it.

    ┌ Library ──────┐ ┌ Just Chatting ───────────── ▶ Put on air ┐ ┌ Inside ──────┐
    │ ⌕ Find…       │ │  the shape you edit            [twin]    │ │ ❧ Webcam     │
    │ Blooms     +  │ │                                          │ │ ▣ Game       │
    │  ▾ Main Scenes│ │                                          │ ├──────────────┤
    │ ❧ Sprigs   +  │ │                                          │ │ Place  X  Y  │
    └───────────────┘ └──────────────────────────────────────────┘ └──────────────┘

Scenes (blooms and sprigs) are on the left, sources on the right, so the two never mix. The big
canvas is the shape being edited; the other shape sits beside it as a small "twin" you can't edit
by accident: click it to swap. The main window builds the widgets that go in the slots.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, QSettings, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.library import SPRIG_MIME
from sprigcast.app.live_stage import heading
from sprigcast.app.terms import PART_MARK

LEFT_WIDTH = 290
RIGHT_WIDTH = 320


class BuildBar(QWidget):
    """Above the canvas: what you're editing on the left, tools (Put on air, Link, Safe zones) on the right."""

    def __init__(self):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 0, 0, 0)
        row.setSpacing(8)
        self.where = heading("", 15)
        self.note = QLabel("")
        self.note.setStyleSheet(f"color: {theme.MUTED};")
        row.addWidget(self.where)
        row.addWidget(self.note)
        row.addStretch(1)
        self.tools = QHBoxLayout()
        self.tools.setSpacing(6)
        row.addLayout(self.tools)
        self.setFixedHeight(40)

    def show_place(self, title: str, note: str, sprig: bool = False) -> None:
        self.where.setText(f"{PART_MARK} {title}" if sprig and title else title)
        self.note.setText(note)


def column(width: int) -> tuple[QWidget, QVBoxLayout]:
    panel = QWidget()
    panel.setFixedWidth(width)
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    return panel, layout


class BuildStage(QWidget):
    """The whole Build screen. `rail`, `card_slot`, `left` (Inside) and `right` (inspector) are filled
    by the main window."""

    def __init__(self, library: QWidget, bar: BuildBar):
        super().__init__()
        self.setObjectName("buildStage")
        self.setStyleSheet(f"#buildStage {{ background: {theme.CHROME}; }}")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        body = QHBoxLayout(self)
        body.setContentsMargins(20, 8, 20, 16)
        body.setSpacing(22)
        self.bar = bar
        # Left: every bloom and sprig.
        self.rail_panel, self.rail = column(LEFT_WIDTH)
        self.rail.addWidget(library, 3)
        body.addWidget(self.rail_panel)
        # Middle: what you're editing, then the canvas.
        middle = QVBoxLayout()
        middle.setSpacing(10)
        middle.addWidget(bar)
        self.card = QFrame()
        self.card.setObjectName("buildCard")
        self.card.setStyleSheet(f"#buildCard {{ background: {theme.PANEL}; border: none; border-radius: 28px; }}"
                                "#buildCard QSplitter::handle { background: transparent; }")
        self.card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(10, 6, 10, 10)
        self.card_slot = card_layout
        middle.addWidget(self.card, 1)
        body.addLayout(middle, 1)
        # Right: what's inside, then the inspector for the picked thing. Drag the gap to share the room.
        self.right_panel = QSplitter(Qt.Orientation.Vertical)
        self.right_panel.setFixedWidth(RIGHT_WIDTH)
        self.right_panel.setChildrenCollapsible(False)
        self.right_panel.setHandleWidth(18)
        self.right_panel.setStyleSheet(
            f"QSplitter::handle {{ background: transparent; border-top: 1px solid {theme.EDGE};"
            " margin: 8px 12px; }")
        inside = QWidget()
        self.left = QVBoxLayout(inside)  # "left" for history: Inside used to be the left column
        self.left.setContentsMargins(0, 0, 0, 0)
        self.left.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(heading("Inside", 15))
        self.inside_note = QLabel("")
        self.inside_note.setStyleSheet(f"color: {theme.MUTED}; font-size: 11px;")
        top.addWidget(self.inside_note, 1)
        self.add_slot = QHBoxLayout()
        top.addLayout(self.add_slot)
        self.left.addLayout(top)
        inspector = QWidget()
        self.right = QVBoxLayout(inspector)
        self.right.setContentsMargins(0, 0, 0, 0)
        self.right.setSpacing(10)
        self.right_panel.addWidget(inside)
        self.right_panel.addWidget(inspector)
        self.right_panel.setStretchFactor(0, 2)
        self.right_panel.setStretchFactor(1, 3)
        # A sound picked in the library swaps the right column for its controls (sound has no place on a canvas).
        self.right_stack = QStackedWidget()
        self.right_stack.setFixedWidth(RIGHT_WIDTH)
        self.sound_page = QWidget()
        self.sound_slot = QVBoxLayout(self.sound_page)
        self.sound_slot.setContentsMargins(0, 0, 0, 0)
        self.sound_slot.setSpacing(12)
        saved = QSettings().value("build/inside_split")
        if saved is not None:
            self.right_panel.restoreState(saved)  # where you last left the gap
        self.right_panel.splitterMoved.connect(
            lambda *_: QSettings().setValue("build/inside_split", self.right_panel.saveState()))
        self.right_stack.addWidget(self.right_panel)
        self.right_stack.addWidget(self.sound_page)
        body.addWidget(self.right_stack)


# Inside: the bloom's contents, sprigs shown open ---------------------------------------------

ROLE_NAME = Qt.ItemDataRole.UserRole + 20  # the name alone, without the [kind] tag
ROLE_TAG = Qt.ItemDataRole.UserRole + 21  # quiet text on the right: "2 hidden here", "filters", "no signal"
ROLE_SPRIG = Qt.ItemDataRole.UserRole + 22  # True for a sprig placement
ROLE_CHILDREN = Qt.ItemDataRole.UserRole + 23  # [(depth, name, is_sprig)]: what's inside a sprig
ROLE_KIND = Qt.ItemDataRole.UserRole + 24  # the source's kind, for its little icon
ROLE_SWATCH = Qt.ItemDataRole.UserRole + 25  # a colour source's colour
ROW_H, CHILD_H, EYE_W = 34, 24, 30
ICON = 22

KIND_NAMES = {"display_capture": "screen", "window_capture": "window", "camera": "camera", "color": "colour",
              "image": "picture", "text": "text", "browser": "web page", "audio_input": "mic",
              "audio_output": "desktop sound", "scene": "sprig"}


def paint_kind_icon(p: QPainter, r: QRectF, kind: str, colour: str | None = None, selected: bool = False) -> None:
    """A small drawn icon on a soft chip saying what a source is: screen, window, camera, picture..."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(theme.RAISED_HOVER if selected else theme.RAISED))
    p.drawRoundedRect(r, 7, 7)
    ink = QColor(theme.ACCENT if kind == "scene" else theme.SELECT_TEXT if selected else theme.TEXT)
    pen = QPen(ink, 1.4)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    c = r.center()
    cx, cy = c.x(), c.y()
    if kind == "display_capture":
        p.drawRoundedRect(QRectF(cx - 6.5, cy - 5, 13, 8.5), 1.5, 1.5)
        p.drawLine(QPointF(cx - 3, cy + 5.5), QPointF(cx + 3, cy + 5.5))
    elif kind == "window_capture":
        p.drawRoundedRect(QRectF(cx - 6.5, cy - 5.5, 13, 11), 1.5, 1.5)
        p.drawLine(QPointF(cx - 6.5, cy - 2), QPointF(cx + 6.5, cy - 2))
    elif kind == "camera":
        p.drawRoundedRect(QRectF(cx - 7, cy - 4.5, 10, 9), 2, 2)
        path = QPainterPath()
        path.moveTo(cx + 3, cy - 1.5)
        path.lineTo(cx + 7, cy - 4)
        path.lineTo(cx + 7, cy + 4)
        path.lineTo(cx + 3, cy + 1.5)
        p.drawPath(path)
    elif kind == "color":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(str(colour or "#888888")[:7]))
        p.drawRoundedRect(QRectF(cx - 5.5, cy - 5.5, 11, 11), 3, 3)
    elif kind == "image":
        p.drawRoundedRect(QRectF(cx - 6.5, cy - 5.5, 13, 11), 2, 2)
        path = QPainterPath()
        path.moveTo(cx - 5, cy + 4)
        path.lineTo(cx - 1.5, cy)
        path.lineTo(cx + 1, cy + 2.5)
        path.lineTo(cx + 3, cy + 0.5)
        path.lineTo(cx + 5, cy + 4)
        p.drawPath(path)
        p.drawPoint(QPointF(cx + 2.5, cy - 2.5))
    elif kind == "text":
        p.drawLine(QPointF(cx - 5, cy - 5), QPointF(cx + 5, cy - 5))
        p.drawLine(QPointF(cx, cy - 5), QPointF(cx, cy + 5.5))
    elif kind == "browser":
        p.drawEllipse(QRectF(cx - 6, cy - 6, 12, 12))
        p.drawEllipse(QRectF(cx - 2.5, cy - 6, 5, 12))
        p.drawLine(QPointF(cx - 6, cy), QPointF(cx + 6, cy))
    elif kind in ("audio_input", "audio_output"):
        if kind == "audio_input":  # a mic
            p.drawRoundedRect(QRectF(cx - 2.5, cy - 6.5, 5, 8.5), 2.5, 2.5)
            p.drawArc(QRectF(cx - 5, cy - 3, 10, 7), 180 * 16, 180 * 16)
            p.drawLine(QPointF(cx, cy + 4), QPointF(cx, cy + 6.5))
        else:  # level bars
            for i, h in enumerate((4, 9, 6, 11, 5)):
                x = cx - 6 + i * 3
                p.drawLine(QPointF(x, cy + h / 2), QPointF(x, cy - h / 2))
    elif kind == "scene":
        font = p.font()
        font.setPixelSize(13)
        p.setFont(font)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, PART_MARK)
    else:  # plugins and anything new: a little puzzle-ish dot
        p.drawEllipse(QRectF(cx - 4, cy - 4, 8, 8))
        p.drawPoint(QPointF(cx + 5.5, cy))
    p.restore()


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
        kind = "scene" if sprig else (index.data(ROLE_KIND) or "")
        if kind:
            paint_kind_icon(p, QRectF(x, top.center().y() - ICON / 2, ICON, ICON), kind, index.data(ROLE_SWATCH),
                            selected)
            p.setFont(font)
            x += ICON + 10
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
    sprig_dropped = Signal(str)  # a sprig dragged in from the library: its part id

    def __init__(self):
        super().__init__()
        self.setItemDelegate(InsideDelegate(self))
        self.setMouseTracking(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSpacing(1)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(SPRIG_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(SPRIG_MIME):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        """Work out where the row was dropped and let the window reorder the bloom (it rebuilds the list)."""
        if event.mimeData().hasFormat(SPRIG_MIME):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.sprig_dropped.emit(bytes(event.mimeData().data(SPRIG_MIME)).decode())
            return
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
