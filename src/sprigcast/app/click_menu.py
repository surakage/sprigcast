"""A menu whose submenus open on a click as well as on hover.

Qt opens a submenu after the pointer rests on it; a click on it could land while it was closing and do
nothing (+ Add → Plugins). Here a click always opens it.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMenu


class ClickMenu(QMenu):
    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        action = self.actionAt(event.position().toPoint())
        sub = action.menu() if action is not None else None
        if sub is not None and action.isEnabled():
            if not sub.isVisible():
                self.setActiveAction(action)
                sub.popup(self.mapToGlobal(self.actionGeometry(action).topRight()))
            event.accept()
            return
        super().mouseReleaseEvent(event)
