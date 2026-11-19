"""A menu made ready ahead of time never turns its parent into a native window.

winId() on a parented menu marked the menu native, which made its parent (and the parent's other children)
native windows too; opening the menu then warned "QWidgetWindow(...ChatPanelClassWindow) must be a top level
window" (PC soak, 2026-11-18). The menus are given their window with create() instead.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import Qt, qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _native(widget: QWidget) -> bool:
    return widget.testAttribute(Qt.WidgetAttribute.WA_NativeWindow)


def _open_quietly(app, holder: QWidget, ready, menu_of) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    old = qInstallMessageHandler(lambda _kind, _where, text: warnings.append(text))
    try:
        top = QWidget()
        QVBoxLayout(top).addWidget(holder)
        top.show()
        app.processEvents()
        ready()
        menu = menu_of()
        assert menu.windowHandle() is not None  # still made ready ahead
        menu.popup(holder.mapToGlobal(holder.rect().center()))
        app.processEvents()
        menu.hide()
        native = [type(w).__name__ for w in (holder, *holder.findChildren(QWidget))
                  if not w.isWindow() and (_native(w) or w.windowHandle() is not None)]
        top.close()
    finally:
        qInstallMessageHandler(old)
    return native, warnings


def test_chat_menu_keeps_the_panel_a_plain_widget(app):
    from sprigcast.app import chat_panel

    panel = chat_panel.ChatPanel()
    native, warnings = _open_quietly(app, panel, panel._ready_menu, lambda: panel._menu)
    assert native == []
    assert not [w for w in warnings if "top level window" in w]


def test_add_menu_keeps_live_side_a_plain_widget(app):
    from sprigcast.app.live_stage import LiveSide

    side = LiveSide(lambda: None, lambda: None, lambda: None, lambda _id: None)
    native, warnings = _open_quietly(app, side, side._ready_add_menu, lambda: side.add_menu)
    assert native == []
    assert not [w for w in warnings if "top level window" in w]
