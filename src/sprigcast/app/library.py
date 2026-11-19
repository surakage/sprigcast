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

from sprigcast.app import icons, theme

ROLE_ID = Qt.ItemDataRole.UserRole  # a bloom's or sprig's scene id
ROLE_FOLDER = Qt.ItemDataRole.UserRole + 1  # a folder's id; "" on the Blooms heading, PART_ROOT on Sprigs
ROLE_ROW = Qt.ItemDataRole.UserRole + 30  # "heading", "folder", "bloom" or "sprig"
ROLE_NAME = Qt.ItemDataRole.UserRole + 31  # the name alone
ROLE_UNUSED = Qt.ItemDataRole.UserRole + 38  # a sprig no bloom uses yet: drawn dimmed
ROLE_TAG = Qt.ItemDataRole.UserRole + 32  # on the right: "on Live" (an icon), "⇄"
ROLE_COLOUR = Qt.ItemDataRole.UserRole + 33  # folder dot, or a bloom's swatch before it has a picture
ROLE_ON_AIR = Qt.ItemDataRole.UserRole + 34  # a bloom on air gets a pink dot
ROLE_THUMB = Qt.ItemDataRole.UserRole + 35  # a bloom's last on-air picture (QImage)
ROLE_SOURCE_KIND = Qt.ItemDataRole.UserRole + 37  # sound rows: the source's kind; sprigs: what they show
ROLE_NOTE = Qt.ItemDataRole.UserRole + 36  # headings: the quiet words after the title
PART_ROOT = "~sprigs"
SOUND_ROOT = "~sound"  # the Sound heading: mics, desktop sound and pages with sound, shared by both shapes
SPRIG_MIME = "application/x-sprigcast-sprig"  # a sprig dragged out of the library: its part id

HEAD_GAP = 20  # room above each heading: the gap between the Blooms and Sprigs cards
HEADING_H, FOLDER_H, ROW_H = 44 + HEAD_GAP, 34, 34
CARD_PAD = 12  # rows sit this far inside their card
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


def _row_rect(option) -> QRectF:
    """A row from its card's left padding to its right (the tree indents nothing itself)."""
    width = option.widget.viewport().width() if option.widget else option.rect.right()
    return QRectF(CARD_PAD, option.rect.y(), max(0, width - 2 * CARD_PAD), option.rect.height())


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
        full = _row_rect(option)
        row = index.data(ROLE_ROW)
        name = index.data(ROLE_NAME) or index.data(Qt.ItemDataRole.DisplayRole) or ""
        tag = index.data(ROLE_TAG) or ""
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        font = QFont(option.font)
        if row == "heading":
            self._paint_heading(p, full.adjusted(0, HEAD_GAP + 4, 0, 0), name, index.data(ROLE_NOTE) or "", index.data(ROLE_FOLDER) == PART_ROOT,
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
        else:  # sprig: an icon for what it shows (camera, chat, game...), in sprig green
            from sprigcast.app.build_stage import paint_kind_icon

            paint_kind_icon(p, QRectF(x - 2, mid - 10, 20, 20), index.data(ROLE_SOURCE_KIND) or "scene",
                            index.data(ROLE_COLOUR), selected=selected, sprig=True)
            x += 26
            if selected:
                font.setWeight(QFont.Weight.Bold)
        right = r.right() - 10
        if index.data(ROLE_ON_AIR):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.LIVE))
            p.drawEllipse(QRectF(right - 8, mid - 4, 8, 8))
            right -= 16
        if tag == "on Live":  # a folder shown on Live: its mark, not words
            icons.paint_icon(p, QRectF(right - 14, mid - 7, 14, 14), "broadcast", theme.ACCENT, 1.5)
            right -= 22
        elif tag:
            small = QFont(option.font)
            small.setPixelSize(11)
            metrics = QFontMetrics(small)
            width = metrics.horizontalAdvance(tag)
            p.setFont(small)
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(right - width, r.y(), width, r.height()), Qt.AlignmentFlag.AlignVCenter, tag)
            right -= width + 10
        if index.data(ROLE_UNUSED) and not selected:
            text_colour = QColor(theme.MUTED)
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
            icons.paint_icon(p, QRectF(x, r.center().y() - 9, 18, 18), "sprig", theme.ACCENT)
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
        theme.paint_button(p, plus, plus.height() / 2, hover)
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
            full = _row_rect(option).adjusted(0, HEAD_GAP + 4, 0, 0)
            if plus_rect(full).adjusted(-4, -4, 4, 4).contains(event.position()):
                self.plus_clicked.emit(index.data(ROLE_FOLDER) or "")
                return True
        return False


class LibraryTree(QTreeWidget):
    """The tree itself. Blooms and sprigs can be dragged to reorder them or into and out of folders;
    after a drop, `dropped` fires and the main window reads the new order back from the rows."""

    dropped = Signal()
    dot_clicked = Signal(str, object)  # a folder's id and the global point under its dot: pick a colour

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

    def event(self, event) -> bool:  # noqa: D102
        handled = theme.view_activation(self, event)  # no repaint when a pop-up takes the focus
        return super().event(event) if handled is None else handled

    def _toggle_folder(self, item: QTreeWidgetItem | None, _column: int) -> None:
        """One click opens or folds a folder (there's no arrow to aim for)."""
        if item is None:  # the row went while it was clicked (the list was made again): PC, 2026-11-19 16:47
            return
        if item.data(0, ROLE_ROW) == "folder":
            item.setExpanded(not item.isExpanded())

    def paintEvent(self, event) -> None:  # noqa: N802
        """Each heading and what's under it sit on their own rounded card (Blooms, then Sprigs)."""
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.PANEL))
        root = self.invisibleRootItem()
        for i in range(root.childCount()):
            head = root.child(i)
            if head.isHidden():
                continue
            top = self.visualItemRect(head)
            last = head
            while True:  # the last row showing under this heading
                kids = [last.child(j) for j in range(last.childCount()) if not last.child(j).isHidden()]
                if not kids or not last.isExpanded():
                    break
                last = kids[-1]
            bottom = self.visualItemRect(last).bottom()
            card = QRectF(0, top.y() + HEAD_GAP, self.viewport().width(), bottom - top.y() - HEAD_GAP + 8)
            p.drawRoundedRect(card, 22, 22)
        p.end()
        super().paintEvent(event)

    def dot_rect(self, item: QTreeWidgetItem) -> QRectF:
        """Where a folder's colour dot is drawn (a little larger, so it's easy to hit)."""
        depth, parent = 0, item.parent()
        while parent is not None:
            depth, parent = depth + 1, parent.parent()
        rect = self.visualItemRect(item)
        x = CARD_PAD + 4 + max(0, depth - 1) * INDENT + 10 + 16
        return QRectF(x - 5, rect.center().y() - 9.5, 19, 19)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        item = self.itemAt(event.position().toPoint())
        if (event.button() == Qt.MouseButton.LeftButton and item is not None
                and item.data(0, ROLE_ROW) == "folder" and self.dot_rect(item).contains(event.position())):
            self.dot_clicked.emit(item.data(0, ROLE_FOLDER), event.globalPosition().toPoint())
            event.accept()
            return
        super().mouseReleaseEvent(event)

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
