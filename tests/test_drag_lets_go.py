"""Rows are dragged through QDrag.exec (Python's lock let go while Windows runs the drag), not Qt's own startDrag,
which held it for the whole drag (stream review 2026-11-23: a 400 ms tick during a library drag)."""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QDrag  # noqa: E402
from PySide6.QtWidgets import QApplication, QTreeWidgetItem  # noqa: E402

from sprigcast.app.library import ROLE_ID, ROLE_ROW, SPRIG_MIME, LibraryTree  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_a_library_drag_carries_the_sprig_and_runs_through_exec(app, monkeypatch):
    seen = {}

    def exec_(drag, actions, default=Qt.DropAction.IgnoreAction):
        seen["mime"] = bytes(drag.mimeData().data(SPRIG_MIME)).decode()
        seen["default"] = default
        return Qt.DropAction.CopyAction

    monkeypatch.setattr(QDrag, "exec", exec_)
    tree = LibraryTree()
    tree.resize(300, 400)
    item = QTreeWidgetItem(["Webcam frame"])
    item.setData(0, ROLE_ROW, "sprig")
    item.setData(0, ROLE_ID, "part-7")
    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsSelectable)
    tree.addTopLevelItem(item)
    tree.show()
    item.setSelected(True)
    ended = []
    tree.drag_ended.connect(lambda: ended.append(True))
    tree.holding = True
    tree.startDrag(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction)
    assert seen == {"mime": "part-7", "default": Qt.DropAction.MoveAction}
    assert ended == [True] and tree.holding is False
    assert tree.topLevelItemCount() == 1  # nothing removed after the drop


def test_nothing_selected_starts_no_drag(app, monkeypatch):
    monkeypatch.setattr(QDrag, "exec", lambda *a: pytest.fail("no drag"))
    tree = LibraryTree()
    tree.startDrag(Qt.DropAction.MoveAction)
