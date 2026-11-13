"""Command palette: Ctrl+K, type a few letters, Enter. Every action, scene, setting and dock in one list.

`rank()` is a forgiving matcher: letters must appear in order, and matches at the start of words or
in a row score higher ("gl" finds "Go live", "set str" finds "Settings: Stream").
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSettings, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeyEvent, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme

RECENT_KEY = "palette/recent"
RECENT_MAX = 8
RECENT_ROLE = Qt.ItemDataRole.UserRole + 1
ROW_HEIGHT = 38
GROUP_ICONS = {"Scene": "🎬", "Action": "⚡", "Settings": "⚙", "Dock": "🪟", "Add": "➕", "Sprig": "🌱",
               "Mode": "🧭", "File": "📁"}


@dataclass
class Command:
    title: str
    group: str  # "Scene", "Action", "Settings", "Dock", "Add", "Sprig", "Mode", "File"
    run: Callable[[], None]
    shortcut: str = ""
    checked: bool | None = None  # for on/off commands: shows a tick when on

    @property
    def key(self) -> str:
        return f"{self.group}:{self.title}"


def score(text: str, query: str) -> float | None:
    """How well query matches text (higher is better), or None when it doesn't."""
    query = "".join(query.lower().split())
    if not query:
        return 0.0
    low = text.lower()
    total, pos, last = 0.0, 0, -2
    for ch in query:
        i = low.find(ch, pos)
        if i < 0:
            return None
        gain = 1.0
        if i == 0 or not low[i - 1].isalnum():
            gain += 2.0  # start of a word
        if i == last + 1:
            gain += 1.5  # right after the previous letter
        total += gain - 0.02 * (i - pos)  # small cost for skipping letters
        pos, last = i + 1, i
    return total - 0.01 * len(text)  # shorter titles win ties


def rank(commands: list[Command], query: str, recent: list[str] | None = None) -> list[Command]:
    """Matching commands, best first. With no query: recent ones first, then the rest in order."""
    recent = recent or []
    if not query.strip():
        order = {key: i for i, key in enumerate(recent)}
        return sorted(commands, key=lambda c: order.get(c.key, len(order)))
    scored = []
    for i, c in enumerate(commands):
        s = score(f"{c.group} {c.title}", query)
        own = score(c.title, query)
        best = max(v for v in (s, own) if v is not None) if (s is not None or own is not None) else None
        if best is not None:
            bonus = 0.5 if c.key in recent else 0.0
            scored.append((-(best + bonus), i, c))
    return [c for *_, c in sorted(scored)]


STYLE = f"""
QDialog#palette {{ background: transparent; }}
QFrame#paletteCard {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE_STRONG};
                     border-top-color: {theme.HIGHLIGHT}; border-radius: 16px; }}
QLineEdit#paletteSearch {{ background: {theme.CHROME}; border: 1px solid {theme.EDGE_STRONG};
                          border-radius: 10px; padding: 10px 14px; font-size: 13pt; color: {theme.TEXT_BRIGHT}; }}
QLineEdit#paletteSearch:focus {{ border: 1px solid {theme.ACCENT}; }}
QListWidget#paletteList {{ background: transparent; border: none; outline: none; }}
QLabel#paletteHint {{ color: {theme.MUTED}; font-size: 8pt; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {theme.EDGE_STRONG}; border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


class CommandPalette(QDialog):
    """A floating search box near the top of the window. Enter runs, Esc closes, arrows move."""

    def __init__(self, commands: list[Command], parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Popup)
        self.setObjectName("palette")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(STYLE)
        self.commands = commands
        self.ran: Command | None = None
        self.recent = [str(k) for k in (QSettings().value(RECENT_KEY, []) or [])]
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 10, 18, 24)  # room for the shadow
        card = QFrame()
        card.setObjectName("paletteCard")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 190))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)
        self.search = QLineEdit()
        self.search.setObjectName("paletteSearch")
        self.search.setPlaceholderText("Type a command, scene or setting…")
        self.search.textChanged.connect(self._filter)
        self.search.installEventFilter(self)
        layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.setObjectName("paletteList")
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setItemDelegate(_RowPainter(self.list))
        self.list.setMouseTracking(True)
        self.list.itemActivated.connect(lambda item: self._run(item))
        self.list.itemClicked.connect(lambda item: self._run(item))
        layout.addWidget(self.list)
        self.hint = QLabel("↑↓ to move  ·  Enter to run  ·  Esc to close")
        self.hint.setObjectName("paletteHint")
        layout.addWidget(self.hint)
        self.setFixedWidth(600)
        self._filter("")

    def showEvent(self, event) -> None:  # noqa: N802
        parent = self.parentWidget()
        if parent is not None:
            top = parent.mapToGlobal(parent.rect().topLeft())
            self.move(top.x() + (parent.width() - self.width()) // 2, top.y() + 70)
        self.search.setFocus()
        super().showEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.search and isinstance(event, QKeyEvent) and event.type() == QKeyEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                step = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1, Qt.Key.Key_PageDown: 8, Qt.Key.Key_PageUp: -8}[key]
                row = max(0, min(self.list.count() - 1, self.list.currentRow() + step))
                self.list.setCurrentRow(row)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run(self.list.currentItem())
                return True
            if key == Qt.Key.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(obj, event)

    def shown(self) -> list[Command]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def _filter(self, text: str) -> None:
        self.list.clear()
        matches = rank(self.commands, text, self.recent)
        for command in matches[:60]:
            item = QListWidgetItem(command.title)
            item.setData(Qt.ItemDataRole.UserRole, command)
            item.setData(RECENT_ROLE, command.key in self.recent and not text.strip())
            item.setSizeHint(QSize(0, ROW_HEIGHT))
            self.list.addItem(item)
        if not matches:
            item = QListWidgetItem("Nothing matches")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setSizeHint(QSize(0, ROW_HEIGHT))
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        rows = min(9, max(1, self.list.count()))
        self.list.setFixedHeight(rows * ROW_HEIGHT + 6)
        self.resize(self.width(), self.sizeHint().height())

    def _run(self, item: QListWidgetItem | None) -> None:
        command = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if command is None:
            return
        self.ran = command
        self.recent = [command.key] + [k for k in self.recent if k != command.key][: RECENT_MAX - 1]
        QSettings().setValue(RECENT_KEY, self.recent)
        self.accept()
        command.run()


class _RowPainter(QStyledItemDelegate):
    """Draws a row: icon, title (with a tick when on), then recent tag, shortcut and group chip on the right."""

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(2, 1, -2, -1)
        command = index.data(Qt.ItemDataRole.UserRole)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(79, 216, 143, 34) if selected else QColor(255, 255, 255, 13))
            painter.drawRoundedRect(QRectF(rect), 9, 9)
        if selected:
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawRoundedRect(QRectF(rect.left(), rect.top() + 8, 3, rect.height() - 16), 1.5, 1.5)
        font = QFont(option.font)
        if command is None:
            painter.setPen(QColor(theme.MUTED))
            painter.drawText(rect.adjusted(14, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter, index.data())
            painter.restore()
            return
        x = rect.left() + 12
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(QRectF(x, rect.top(), 24, rect.height()), Qt.AlignmentFlag.AlignCenter,
                         GROUP_ICONS.get(command.group, "•"))
        x += 34
        right = rect.right() - 10
        small = QFont(font)
        small.setPointSizeF(max(7.0, font.pointSizeF() * 0.8))
        right = self._pill(painter, small, command.group, right, rect, QColor(255, 255, 255, 18), QColor(theme.MUTED))
        if command.shortcut:
            right = self._pill(painter, small, command.shortcut, right - 6, rect, QColor(0, 0, 0, 0),
                               QColor(theme.TEXT), QColor(255, 255, 255, 50))
        if index.data(RECENT_ROLE):
            painter.setFont(small)
            painter.setPen(QColor(theme.MUTED))
            width = QFontMetrics(small).horizontalAdvance("recent")
            painter.drawText(QRectF(right - 8 - width, rect.top(), width, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter, "recent")
            right -= width + 8
        title_font = QFont(font)
        title_font.setPointSizeF(font.pointSizeF() * 1.05)
        if selected:
            title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        title = ("✓  " if command.checked else "") + command.title
        room = right - 12 - x
        title = QFontMetrics(title_font).elidedText(title, Qt.TextElideMode.ElideRight, room)
        painter.setPen(QColor(theme.SELECT_TEXT if selected else theme.TEXT))
        painter.drawText(QRectF(x, rect.top(), room, rect.height()), Qt.AlignmentFlag.AlignVCenter, title)
        painter.restore()

    @staticmethod
    def _pill(painter, font, text, right, rect, fill, ink, border=None) -> int:
        metrics = QFontMetrics(font)
        width = metrics.horizontalAdvance(text) + 16
        height = metrics.height() + 4
        box = QRectF(right - width, rect.center().y() - height / 2 + 0.5, width, height)
        painter.setPen(border if border is not None else Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(box, height / 2, height / 2)
        painter.setFont(font)
        painter.setPen(ink)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        return int(box.left())
