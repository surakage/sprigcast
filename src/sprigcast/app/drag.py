"""Dragging rows out of a list without holding up the picture.

Qt's own QAbstractItemView.startDrag runs the whole drag (Windows' drag loop, until the drop) inside one call that
PySide makes with Python's lock held, so the render thread could not take a tick until the mouse came up: a
library drag cost a 400 ms tick with "sprig editor 170.9 ms" (stream review 2026-11-23). QDrag.exec lets go of the
lock while it waits, so the drag is started here instead: the rows' data and a picture of them, then exec.

Every drop target in SprigCast takes the rows itself (a list's own dropEvent moves them; the canvas and Inside
take a sprig as a copy), so nothing is removed here after a move, unlike Qt's own startDrag.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QDrag

DRAG_PICTURE_MAX = 320  # px tall at most: a picture of a few rows, not of the whole list


def start_drag(view, actions) -> Qt.DropAction:
    """Drag the selected rows of `view` (a QAbstractItemView) with `actions`. Returns the drop's action."""
    indexes = [i for i in view.selectedIndexes() if view.model().flags(i) & Qt.ItemFlag.ItemIsDragEnabled]
    if not indexes:
        return Qt.DropAction.IgnoreAction
    data = view.model().mimeData(indexes)
    if data is None:
        return Qt.DropAction.IgnoreAction
    drag = QDrag(view)
    drag.setMimeData(data)
    rect = QRect()
    for index in indexes:
        rect = rect.united(view.visualRect(index))
    rect = rect.intersected(view.viewport().rect())
    if not rect.isEmpty():
        rect.setHeight(min(rect.height(), DRAG_PICTURE_MAX))
        drag.setPixmap(view.viewport().grab(rect))
        cursor = view.viewport().mapFromGlobal(view.cursor().pos())
        drag.setHotSpot(QPoint(max(0, min(cursor.x() - rect.x(), rect.width())),
                               max(0, min(cursor.y() - rect.y(), rect.height()))))
    default = view.defaultDropAction()
    if default == Qt.DropAction.IgnoreAction or not (actions & default):
        default = Qt.DropAction.CopyAction if actions & Qt.DropAction.CopyAction else Qt.DropAction.MoveAction
    return drag.exec(actions, default)  # Python's lock is let go while Windows runs the drag
