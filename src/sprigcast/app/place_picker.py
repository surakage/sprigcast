"""Show all: every place to stream to, A to Z, with a search box.

The five everyday places are in the menus themselves (output/stream.py TOP_SERVICES); this is where the rest
are found. One column that never scrolls sideways; type to narrow it, Enter or a double click picks.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QAbstractItemView, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from sprigcast.app import platform_logos, theme
from sprigcast.app.transitions_panel import PopUp
from sprigcast.output.stream import all_services

FIELD_STYLE = (f"QLineEdit {{ background: {theme.FIELD}; border: 1px solid {theme.FIELD_EDGE}; border-radius: 12px;"
               f" padding: 7px 12px; color: {theme.TEXT_BRIGHT}; }}"
               f"QLineEdit:focus {{ border-color: {theme.ACCENT}; }}")
LIST_STYLE = (f"QListWidget {{ background: {theme.SECTION}; border: 1px solid {theme.SECTION_EDGE};"
              " border-radius: 14px; padding: 6px; outline: none; }"
              f"QListWidget::item {{ padding: 8px 12px; border-radius: 9px; color: {theme.TEXT}; }}"
              f"QListWidget::item:hover {{ background: {theme.RAISED}; }}"
              f"QListWidget::item:selected {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}")


class PlacePicker(PopUp):
    """picked: the service key chosen, or None when closed without one."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Places to stream", parent)
        self.picked: str | None = None
        self.done_button.setText("Add")
        self.done_button.clicked.disconnect()
        self.done_button.clicked.connect(self._pick)
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.setAccessibleName("Search places")
        self.search.setClearButtonEnabled(True)
        self.search.setStyleSheet(FIELD_STYLE)
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._pick)
        box.addWidget(self.search)
        self.list = QListWidget()
        self.list.setAccessibleName("Places")
        self.list.setStyleSheet(LIST_STYLE)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setIconSize(QSize(24, 24))
        for key, label in all_services():
            item = QListWidgetItem(platform_logos.icon(key, 24, label[:2]), label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.list.addItem(item)
        self.list.itemDoubleClicked.connect(lambda _item: self._pick())
        self.list.itemSelectionChanged.connect(self._show_ready)
        box.addWidget(self.list, 1)
        self.stack.addWidget(page)
        self.setMinimumSize(380, 420)
        self.resize(420, 560)
        self._show_ready()
        self.search.setFocus()

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        first = None
        for row in range(self.list.count()):
            item = self.list.item(row)
            shown = all(word in item.text().lower() for word in words)
            item.setHidden(not shown)
            if shown and first is None:
                first = item
        current = self.list.currentItem()
        if current is None or current.isHidden():
            self.list.setCurrentItem(first)  # the top match: Enter takes it
        self._show_ready()

    def _show_ready(self) -> None:
        item = self.list.currentItem()
        self.done_button.setEnabled(item is not None and not item.isHidden())

    def keyPressEvent(self, event) -> None:  # noqa: N802  arrows move the list while you type
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self.search.hasFocus():
            shown = [r for r in range(self.list.count()) if not self.list.item(r).isHidden()]
            if shown:
                now = self.list.currentRow()
                at = shown.index(now) if now in shown else -1
                step = 1 if event.key() == Qt.Key.Key_Down else -1
                self.list.setCurrentRow(shown[max(0, min(len(shown) - 1, at + step))])
            return
        super().keyPressEvent(event)

    def _pick(self) -> None:
        item = self.list.currentItem()
        if item is None or item.isHidden():
            return
        self.picked = item.data(Qt.ItemDataRole.UserRole)
        self.accept()


def pick_place(parent: QWidget | None = None) -> str | None:
    """Open the list; the service key picked, or None."""
    from sprigcast.app.modal import modal

    picker = PlacePicker(parent)
    modal(picker)
    return picker.picked
