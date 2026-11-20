"""Rebuild a menu only when what it lists has changed, and tell screen readers when it does.

Menus that cleared and refilled themselves on every aboutToShow deleted the very items a screen reader
(UI Automation on Windows) had just been told about as the menu opened, and announced nothing new: the
Gardens menu showed no items to it at all. Now an unchanged menu keeps its items, and a changed one is
rebuilt and announced with a reorder event, so assistive tools read the new items.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable

from PySide6.QtWidgets import QMenu

KEY = "sprigcastBuiltFor"  # a Qt property, so it lives with the menu itself


def announce(menu: QMenu) -> None:
    """Tell assistive tools the menu's items changed (UIA: a structure-changed event)."""
    try:
        from PySide6.QtGui import QAccessible, QAccessibleEvent

        QAccessible.updateAccessibility(QAccessibleEvent(menu, QAccessible.Event.ObjectReorder))
    except (ImportError, AttributeError, TypeError, RuntimeError):  # no accessibility in this Qt build
        pass


def refresh(menu: QMenu, key: Hashable, build: Callable[[], None]) -> bool:
    """Run `build` (which fills `menu` from scratch) unless the menu was last built for this same `key`.
    True if it rebuilt."""
    stamp = repr(key)
    if menu.property(KEY) == stamp:
        return False
    build()
    menu.setProperty(KEY, stamp)
    announce(menu)
    return True
