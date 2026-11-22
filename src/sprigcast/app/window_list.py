"""The open-windows drop-down for Window capture and Game capture: each window by its app's name, icon and title.

    [ 🦊 Firefox · YouTube                    ▾ ]
     ┌──────────────────────────────────────────┐
     │ [ Search                               ] │   (when the list is long)
     │ 🦊 Firefox                               │
     │    YouTube                               │   the window's title, muted, without the app's name
     │ 🦊 Firefox                               │
     │    Twitch                                │
     │ ⚙ Settings                               │   a Store app (Windows' frame) by its own name
     └──────────────────────────────────────────┘

The box itself stays a QComboBox (its items, data and signals are unchanged); only the list it opens is drawn here,
with a search box once there are more than SEARCH_FROM entries. Search matches the app's name and the title.
Light: the list is made when it opens and let go when it closes.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFrame, QLineEdit, QListWidget, QListWidgetItem,
                               QStyle, QStyledItemDelegate, QStyleOptionViewItem, QVBoxLayout)

from sprigcast.app import theme

APP_ROLE = Qt.ItemDataRole.UserRole + 1  # the app's name ("Firefox")
DETAIL_ROLE = Qt.ItemDataRole.UserRole + 2  # which window ("YouTube"), "" when the title is only the app's name
ROW_ROLE = Qt.ItemDataRole.UserRole + 3  # in the list: the combo's row it stands for
SEARCH_FROM = 7
ICON = 20


def label(app: str, detail: str) -> str:
    """The box's text for a window: 'Firefox · YouTube', or the app alone."""
    return f"{app}  ·  {detail}" if detail else app


def add_window(box: QComboBox, icon, app: str, detail: str, data) -> None:
    box.addItem(icon, label(app, detail), data)
    row = box.count() - 1
    box.setItemData(row, app, APP_ROLE)
    box.setItemData(row, detail, DETAIL_ROLE)
    box.setItemData(row, f"{app}\n{detail}" if detail else app, Qt.ItemDataRole.ToolTipRole)


class _TwoLines(QStyledItemDelegate):
    """The app's name, and the window's title under it in muted text."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        two = bool(index.data(DETAIL_ROLE))
        return QSize(option.rect.width(), 50 if two else 36)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        r = option.rect.adjusted(2, 1, -2, -1)
        if selected or hover:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.SELECT_BG if selected else theme.RAISED))
            painter.drawRoundedRect(r, 9, 9)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        x = r.left() + 10
        if icon is not None and not icon.isNull():
            icon.paint(painter, QRect(x, r.center().y() - ICON // 2, ICON, ICON))
        x += ICON + 10
        width = r.right() - x - 8
        app = str(index.data(APP_ROLE) or index.data(Qt.ItemDataRole.DisplayRole) or "")
        detail = str(index.data(DETAIL_ROLE) or "")
        bold = QFont(option.font)
        bold.setWeight(QFont.Weight.DemiBold)
        painter.setFont(bold)
        painter.setPen(QColor(theme.SELECT_TEXT if selected else theme.TEXT_BRIGHT))
        top = r.top() + (7 if detail else 0)
        line = painter.fontMetrics().height()
        painter.drawText(QRect(x, top, width, line if detail else r.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(app, Qt.TextElideMode.ElideRight, width))
        if detail:
            painter.setFont(option.font)
            painter.setPen(QColor(theme.SELECT_TEXT if selected else theme.MUTED))
            painter.drawText(QRect(x, top + line + 1, width, line), Qt.AlignmentFlag.AlignLeft,
                             painter.fontMetrics().elidedText(detail, Qt.TextElideMode.ElideRight, width))
        painter.restore()


class WindowListPopup(QFrame):
    """The list a WindowCombo opens: picks set the combo's row."""

    def __init__(self, box: QComboBox):
        super().__init__(box, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.box = box
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        card = QFrame(self)
        card.setObjectName("windowList")
        card.setStyleSheet(f"#windowList {{ background: {theme.SECTION}; border: 1px solid {theme.SECTION_EDGE};"
                           " border-radius: 14px; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        body = QVBoxLayout(card)
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(8)
        rows = [i for i in range(box.count()) if box.itemData(i, Qt.ItemDataRole.AccessibleDescriptionRole)
                != "separator"]
        self.search: QLineEdit | None = None
        if len(rows) >= SEARCH_FROM:
            from sprigcast.app.place_picker import FIELD_STYLE

            self.search = QLineEdit()
            self.search.setPlaceholderText("Search")
            self.search.setAccessibleName("Search windows")
            self.search.setClearButtonEnabled(True)
            self.search.setStyleSheet(FIELD_STYLE)
            self.search.textChanged.connect(self._filter)
            self.search.returnPressed.connect(self._pick_current)
            body.addWidget(self.search)
        self.list = QListWidget()
        self.list.setAccessibleName("Windows")
        self.list.setFrameShape(QFrame.Shape.NoFrame)
        self.list.setStyleSheet("QListWidget { background: transparent; border: none; outline: none; }")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setMouseTracking(True)
        self.list.setItemDelegate(_TwoLines(self.list))
        for i in rows:
            item = QListWidgetItem(box.itemIcon(i), box.itemText(i))
            item.setData(APP_ROLE, box.itemData(i, APP_ROLE) or box.itemText(i))
            item.setData(DETAIL_ROLE, box.itemData(i, DETAIL_ROLE) or "")
            item.setData(ROW_ROLE, i)
            self.list.addItem(item)
            if i == box.currentIndex():
                self.list.setCurrentItem(item)
        self.list.itemClicked.connect(self._pick)
        self.list.itemActivated.connect(self._pick)
        body.addWidget(self.list)
        height = sum(self.list.sizeHintForRow(r) for r in range(min(self.list.count(), 8))) + 6
        self.list.setFixedHeight(max(40, height))
        self.setFixedWidth(max(box.width(), 360))
        self.adjustSize()

    def show_under(self) -> None:
        screen = self.box.screen().availableGeometry()
        at = self.box.mapToGlobal(QPoint(0, self.box.height() + 4))
        if at.y() + self.height() > screen.bottom():
            at.setY(self.box.mapToGlobal(QPoint(0, 0)).y() - self.height() - 4)  # no room below: above the box
        at.setX(max(screen.left(), min(at.x(), screen.right() - self.width())))
        self.move(at)
        self.show()
        (self.search or self.list).setFocus()
        if self.list.currentItem() is not None:
            self.list.scrollToItem(self.list.currentItem())

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        first = None
        for r in range(self.list.count()):
            item = self.list.item(r)
            hay = f"{item.data(APP_ROLE)} {item.data(DETAIL_ROLE)}".lower()
            shown = all(w in hay for w in words)
            item.setHidden(not shown)
            if shown and first is None:
                first = item
        current = self.list.currentItem()
        if current is None or current.isHidden():
            self.list.setCurrentItem(first)

    def keyPressEvent(self, event) -> None:  # noqa: N802  arrows move the list while typing in the search box
        if self.search is not None and self.search.hasFocus() and event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            shown = [r for r in range(self.list.count()) if not self.list.item(r).isHidden()]
            if shown:
                now = self.list.currentRow()
                at = shown.index(now) if now in shown else -1
                step = 1 if event.key() == Qt.Key.Key_Down else -1
                self.list.setCurrentRow(shown[max(0, min(len(shown) - 1, at + step))])
            return
        super().keyPressEvent(event)

    def _pick_current(self) -> None:
        item = self.list.currentItem()
        if item is not None and not item.isHidden():
            self._pick(item)

    def _pick(self, item: QListWidgetItem) -> None:
        row = int(item.data(ROW_ROLE))
        self.close()
        if row != self.box.currentIndex():
            self.box.setCurrentIndex(row)
        self.box.activated.emit(row)


class WindowCombo(QComboBox):
    """Re-reads the open windows each time it opens, and opens the two-line list with search."""

    def __init__(self, on_open: Callable[[], None]):
        super().__init__()
        self._on_open = on_open
        self.popup: WindowListPopup | None = None

    def showPopup(self) -> None:  # noqa: N802
        self._on_open()
        self.popup = WindowListPopup(self)
        self.popup.destroyed.connect(lambda *_: setattr(self, "popup", None))
        self.popup.show_under()

    def hidePopup(self) -> None:  # noqa: N802
        if self.popup is not None:
            self.popup.close()
        super().hidePopup()
