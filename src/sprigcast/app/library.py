"""Build's left rail: every bloom and sprig, grouped in folders you can fold away.

    ⌕ Find a bloom or sprig
    Blooms  your scenes                    (+)
      ▾ ● Main Scenes                  on Live
          ▭ Starting soon
          ▭ Just Chatting                    ●
      ▸ ● Extras                             4
    ❧ Sprigs  scenes you reuse             (+)
      ▾ ● Nested Scenes                      3
          ❧ Webcam frame                  in 4

Scenes live here; what's inside the one you picked is listed on the right, so the two never mix.
The main window fills the tree (it owns the folders, menus and drag and drop rules); this file only
draws it and filters it.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLineEdit,
    QStyle,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.terms import PART_MARK

ROLE_ID = Qt.ItemDataRole.UserRole  # a bloom's or sprig's scene id
ROLE_FOLDER = Qt.ItemDataRole.UserRole + 1  # a folder's id; "" on the Blooms heading, PART_ROOT on Sprigs
ROLE_ROW = Qt.ItemDataRole.UserRole + 30  # "heading", "folder", "bloom" or "sprig"
ROLE_NAME = Qt.ItemDataRole.UserRole + 31  # the name alone
ROLE_TAG = Qt.ItemDataRole.UserRole + 32  # quiet text on the right: "on Live", "4", "in 3"
ROLE_COLOUR = Qt.ItemDataRole.UserRole + 33  # folder dot, or a bloom's swatch before it has a picture
ROLE_ON_AIR = Qt.ItemDataRole.UserRole + 34  # a bloom on air gets a pink dot
ROLE_THUMB = Qt.ItemDataRole.UserRole + 35  # a bloom's last on-air picture (QImage)
ROLE_SOURCE_KIND = Qt.ItemDataRole.UserRole + 37  # sound rows: the source's kind, for its icon
ROLE_NOTE = Qt.ItemDataRole.UserRole + 36  # headings: the quiet words after the title
PART_ROOT = "~sprigs"
SOUND_ROOT = "~sound"  # the Sound heading: mics, desktop sound and pages with sound, shared by both shapes
SPRIG_MIME = "application/x-sprigcast-sprig"  # a sprig dragged out of the library: its part id

HEADING_H, FOLDER_H, ROW_H = 44, 34, 34
SWATCH = (34, 20)
PLUS = 26  # the round + on a heading
INDENT = 14


def heading_item(title: str, note: str, folder_key: str) -> QTreeWidgetItem:
    item = QTreeWidgetItem([title])
    item.setData(0, ROLE_ROW, "heading")
    item.setData(0, ROLE_FOLDER, folder_key)
    item.setData(0, ROLE_NAME, title)
    item.setData(0, ROLE_NOTE, note)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled)
    return item


def plus_rect(rect) -> QRectF:
    r = QRectF(rect)
    return QRectF(r.right() - PLUS - 6, r.center().y() - PLUS / 2, PLUS, PLUS)


class LibraryDelegate(QStyledItemDelegate):
    """Draws headings, folders, blooms and sprigs. Indentation is drawn here, not by the tree."""

    plus_clicked = Signal(str)  # the heading's ROLE_FOLDER: "" for blooms, PART_ROOT for sprigs

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        row = index.data(ROLE_ROW)
        return QSize(option.rect.width(), HEADING_H if row == "heading" else FOLDER_H if row == "folder" else ROW_H)

    @staticmethod
    def _depth(index: QModelIndex) -> int:
        depth, parent = 0, index.parent()
        while parent.isValid():
            depth, parent = depth + 1, parent.parent()
        return depth

    def paint(self, p: QPainter, option, index) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # The tree indents children itself; draw from the viewport's left edge so rows line up.
        full = QRectF(0, option.rect.y(), option.widget.viewport().width() if option.widget else option.rect.right(),
                      option.rect.height())
        row = index.data(ROLE_ROW)
        name = index.data(ROLE_NAME) or index.data(Qt.ItemDataRole.DisplayRole) or ""
        tag = index.data(ROLE_TAG) or ""
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        font = QFont(option.font)
        if row == "heading":
            self._paint_heading(p, full, name, index.data(ROLE_NOTE) or "", index.data(ROLE_FOLDER) == PART_ROOT,
                                font, hover)
            p.restore()
            return
        x = full.x() + 4 + max(0, self._depth(index) - 1) * INDENT
        r = QRectF(x, full.y() + 2, full.right() - x - 2, full.height() - 4)
        if selected or (hover and row != "folder"):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.SELECT_BG if selected else theme.RAISED))
            p.drawRoundedRect(r, 12, 12)
        x = r.x() + 10
        mid = r.center().y()
        text_colour = QColor(theme.SELECT_TEXT if selected else theme.TEXT)
        if row == "folder":
            open_ = bool(option.state & QStyle.StateFlag.State_Open)
            p.setPen(QColor(theme.MUTED))
            small = QFont(font)
            small.setPixelSize(10)
            p.setFont(small)
            p.drawText(QRectF(x - 2, r.y(), 12, r.height()), Qt.AlignmentFlag.AlignCenter, "▾" if open_ else "▸")
            x += 16
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(index.data(ROLE_COLOUR) or theme.MUTED))
            p.drawEllipse(QRectF(x, mid - 4.5, 9, 9))
            x += 17
            font.setWeight(QFont.Weight.Bold)
        elif row == "bloom":
            self._paint_swatch(p, QRectF(x, mid - SWATCH[1] / 2, *SWATCH), index.data(ROLE_COLOUR),
                               index.data(ROLE_THUMB))
            x += SWATCH[0] + 10
            if selected:
                font.setWeight(QFont.Weight.Bold)
        elif row == "sound":
            from sprigcast.app.build_stage import paint_kind_icon

            paint_kind_icon(p, QRectF(x - 2, mid - 10, 20, 20), index.data(ROLE_SOURCE_KIND) or "audio_input",
                            selected=selected)
            x += 26
            if selected:
                font.setWeight(QFont.Weight.Bold)
        else:  # sprig
            p.setPen(QColor(theme.ACCENT))
            p.setFont(font)
            p.drawText(QRectF(x, r.y(), 16, r.height()), Qt.AlignmentFlag.AlignVCenter, PART_MARK)
            x += 20
            if selected:
                font.setWeight(QFont.Weight.Bold)
        right = r.right() - 10
        if index.data(ROLE_ON_AIR):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.LIVE))
            p.drawEllipse(QRectF(right - 8, mid - 4, 8, 8))
            right -= 16
        if tag:
            small = QFont(option.font)
            small.setPixelSize(11)
            metrics = QFontMetrics(small)
            width = metrics.horizontalAdvance(tag)
            p.setFont(small)
            p.setPen(QColor(theme.ACCENT if tag == "on Live" else theme.MUTED))
            p.drawText(QRectF(right - width, r.y(), width, r.height()), Qt.AlignmentFlag.AlignVCenter, tag)
            right -= width + 10
        p.setFont(font)
        p.setPen(text_colour)
        metrics = QFontMetrics(font)
        p.drawText(QRectF(x, r.y(), right - x, r.height()), Qt.AlignmentFlag.AlignVCenter,
                   metrics.elidedText(name, Qt.TextElideMode.ElideRight, int(right - x)))
        p.restore()

    @staticmethod
    def _paint_heading(p: QPainter, r: QRectF, title: str, note: str, sprigs: bool, font: QFont, hover: bool) -> None:
        big = QFont(font)
        big.setPixelSize(17)
        big.setWeight(QFont.Weight.Bold)
        x = r.x() + 6
        if sprigs:
            p.setPen(QColor(theme.ACCENT))
            p.setFont(big)
            p.drawText(QRectF(x, r.y(), 20, r.height()), Qt.AlignmentFlag.AlignVCenter, PART_MARK)
            x += 22
        p.setPen(QColor(theme.TEXT_BRIGHT))
        p.setFont(big)
        p.drawText(QRectF(x, r.y(), r.width(), r.height()), Qt.AlignmentFlag.AlignVCenter, title)
        x += QFontMetrics(big).horizontalAdvance(title) + 8
        small = QFont(font)
        small.setPixelSize(11)
        p.setFont(small)
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(x, r.y() + 2, r.right() - x - PLUS - 12, r.height()), Qt.AlignmentFlag.AlignVCenter, note)
        plus = plus_rect(r)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.RAISED_HOVER if hover else theme.RAISED))
        p.drawEllipse(plus)
        p.setPen(QPen(QColor(theme.TEXT), 1.6))
        c = plus.center()
        p.drawLine(int(c.x() - 5), int(c.y()), int(c.x() + 5), int(c.y()))
        p.drawLine(int(c.x()), int(c.y() - 5), int(c.x()), int(c.y() + 5))

    @staticmethod
    def _paint_swatch(p: QPainter, r: QRectF, colour: str | None, image: QImage | None) -> None:
        path = QPainterPath()
        path.addRoundedRect(r, 6, 6)
        p.save()
        p.setClipPath(path)
        base = QColor(theme.STAGE_EMPTY)
        if colour:  # a quiet wash of the folder's colour, so twenty swatches don't shout
            tint = QColor(colour)
            base = QColor(int(base.red() * 0.7 + tint.red() * 0.3), int(base.green() * 0.7 + tint.green() * 0.3),
                          int(base.blue() * 0.7 + tint.blue() * 0.3))
        p.fillRect(r, base)
        if isinstance(image, QImage) and not image.isNull():
            size = QSize(image.width(), image.height()).scaled(r.size().toSize(),
                                                                Qt.AspectRatioMode.KeepAspectRatioByExpanding)
            p.drawImage(QRectF(r.center().x() - size.width() / 2, r.center().y() - size.height() / 2,
                               size.width(), size.height()), image)
        p.restore()

    def editorEvent(self, event, model, option, index) -> bool:  # noqa: N802
        """The round + on a heading adds a bloom or a sprig."""
        if (event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton
                and index.data(ROLE_ROW) == "heading"):
            full = QRectF(0, option.rect.y(), option.widget.viewport().width() if option.widget else
                          option.rect.right(), option.rect.height())
            if plus_rect(full).adjusted(-4, -4, 4, 4).contains(event.position()):
                self.plus_clicked.emit(index.data(ROLE_FOLDER) or "")
                return True
        return False


class LibraryTree(QTreeWidget):
    """The tree itself. Blooms and sprigs can be dragged to reorder them or into and out of folders;
    after a drop, `dropped` fires and the main window reads the new order back from the rows."""

    dropped = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("library")
        self.setHeaderHidden(True)
        self.setRootIsDecorated(False)
        self.setIndentation(0)  # the delegate indents
        self.setMouseTracking(True)
        self.setUniformRowHeights(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setDropIndicatorShown(True)
        self.setExpandsOnDoubleClick(False)
        self.invisibleRootItem().setFlags(Qt.ItemFlag.ItemIsEnabled)  # nothing lands at the very top level
        self.delegate = LibraryDelegate(self)
        self.setItemDelegate(self.delegate)
        self.setStyleSheet(
            "#library { background: transparent; border: none; outline: none; }"
            "#library::item, #library::item:hover, #library::item:selected { background: transparent; }"
            "#library::branch { background: transparent; image: none; border: none; }")
        self.itemClicked.connect(self._toggle_folder)

    def _toggle_folder(self, item: QTreeWidgetItem, _column: int) -> None:
        """One click opens or folds a folder (there's no arrow to aim for)."""
        if item.data(0, ROLE_ROW) == "folder":
            item.setExpanded(not item.isExpanded())

    def mimeData(self, items):  # noqa: N802 (Qt naming)
        """A dragged sprig also carries its id, so the canvas or Inside can take it."""
        data = super().mimeData(items)
        sprigs = [i for i in items if i.data(0, ROLE_ROW) == "sprig"]
        if data is not None and len(sprigs) == 1:
            data.setData(SPRIG_MIME, str(sprigs[0].data(0, ROLE_ID)).encode())
        return data

    def set_thumb(self, scene_id: str, image: QImage) -> None:
        """A fresh picture for one bloom's swatch, without rebuilding the list."""
        stack = [self.invisibleRootItem()]
        while stack:
            item = stack.pop()
            if item.data(0, ROLE_ROW) == "bloom" and item.data(0, ROLE_ID) == scene_id:
                item.setData(0, ROLE_THUMB, image)
                return
            stack.extend(item.child(i) for i in range(item.childCount()))

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().dropEvent(event)
        self.dropped.emit()


class Library(QWidget):
    """The search box and the tree."""

    def __init__(self, tree: LibraryTree):
        super().__init__()
        self.tree = tree
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("⌕  Find a bloom or sprig")
        self.search.setClearButtonEnabled(True)
        self.search.setStyleSheet(
            f"QLineEdit {{ background: {theme.PANEL}; border: none; border-radius: 16px; padding: 9px 14px;"
            f" color: {theme.TEXT}; }}"
            f"QLineEdit:focus {{ background: {theme.RAISED}; }}")
        layout.addWidget(self.search)
        layout.addWidget(tree, 1)

    def apply_filter(self) -> None:
        """Hide blooms and sprigs whose names don't match, and folders left empty. Call after a refresh."""
        words = self.search.text().strip().lower()
        root = self.tree.invisibleRootItem()
        self.tree.blockSignals(True)  # opening folders to show matches isn't the user folding them
        try:
            for i in range(root.childCount()):
                self._filter(root.child(i), words)
        finally:
            self.tree.blockSignals(False)

    def _filter(self, item: QTreeWidgetItem, words: str) -> bool:
        row = item.data(0, ROLE_ROW)
        if row in ("bloom", "sprig", "sound"):
            match = not words or words in (item.data(0, ROLE_NAME) or "").lower()
            item.setHidden(not match)
            return match
        any_match = False
        for i in range(item.childCount()):
            any_match = self._filter(item.child(i), words) or any_match
        if row == "folder":
            item.setHidden(bool(words) and not any_match)
            if words and any_match:
                item.setExpanded(True)
        return any_match
