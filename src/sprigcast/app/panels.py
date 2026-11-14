"""Panels: SprigCast's calmer take on docks.

Nothing floats or snaps around. Each screen has one fixed place for extra panels: a rounded card at
the bottom of Live's right column and of Build's left rail. Panels that share it get pill tabs
(Chat | Activity | Transitions). Any panel can pop out (↗) to its own window, say on a second
monitor, and goes back to its card when that window closes. Live and Build each remember which
panels they show, so chat can sit on Live while Build stays clear.

    ┌ Chat  Activity      ↗  × ┐
    │                          │
    │   (the panel's page)     │
    └──────────────────────────┘
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme

SETTINGS_KEY = "panels/state"
MODES = ("live", "build")
WHERE = {"live": "Live: right column", "build": "Build: left rail"}

TAB_STYLE = (
    f"QPushButton {{ background: transparent; color: {theme.MUTED}; border: none; border-radius: 12px;"
    f" padding: 5px 12px; font-weight: 600; }}"
    f"QPushButton:hover {{ color: {theme.TEXT}; background: {theme.RAISED}; }}"
    f"QPushButton:checked {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}"
)
TOOL_STYLE = (
    f"QPushButton {{ background: transparent; color: {theme.MUTED}; border: none; border-radius: 12px;"
    f" padding: 3px 8px; font-size: 14px; }}"
    f"QPushButton:hover {{ color: {theme.TEXT}; background: {theme.RAISED}; }}"
)


@dataclass
class Panel:
    key: str
    title: str
    widget: QWidget
    modes: tuple[str, ...] = MODES
    on_show: Callable[[], None] | None = None  # e.g. load a web page the first time it's seen


@dataclass
class State:
    shown: dict[str, list[str]] = field(default_factory=lambda: {m: [] for m in MODES})
    active: dict[str, str] = field(default_factory=dict)
    popped: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "State":
        try:
            raw = json.loads(str(QSettings().value(SETTINGS_KEY, "") or "{}"))
        except ValueError:
            raw = {}
        state = cls()
        for mode in MODES:
            state.shown[mode] = [str(k) for k in raw.get("shown", {}).get(mode, [])]
        state.active = {str(m): str(k) for m, k in raw.get("active", {}).items()}
        state.popped = [str(k) for k in raw.get("popped", [])]
        return state

    def save(self) -> None:
        QSettings().setValue(SETTINGS_KEY, json.dumps(
            {"shown": self.shown, "active": self.active, "popped": self.popped}))


class PanelCard(QFrame):
    """The fixed place on one screen: pill tabs, ↗ and ×, and the chosen panel below."""

    def __init__(self, manager: "Panels", mode: str):
        super().__init__()
        self.manager, self.mode = manager, mode
        self.setObjectName("panelCard")
        self.setStyleSheet(f"#panelCard {{ background: {theme.PANEL}; border: none; border-radius: 18px; }}")
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(4)
        self.tabs = QHBoxLayout()
        self.tabs.setSpacing(4)
        top.addLayout(self.tabs)
        top.addStretch(1)
        self.pop = QPushButton("↗")
        self.pop.setToolTip("Pop out to its own window")
        self.close_button = QPushButton("×")
        self.close_button.setToolTip("Hide this panel (Panels menu brings it back)")
        for button in (self.pop, self.close_button):
            button.setStyleSheet(TOOL_STYLE)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            top.addWidget(button)
        self.pop.clicked.connect(lambda: self.current and manager.pop_out(self.current))
        self.close_button.clicked.connect(lambda: self.current and manager.set_shown(self.current, False, mode))
        root.addLayout(top)
        self.stack = QStackedWidget()
        self.stack.setStyleSheet("QStackedWidget { background: transparent; }")
        root.addWidget(self.stack, 1)
        self.setMinimumHeight(240)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        self.current: str | None = None
        self.hide()

    def show_panels(self, panels: list[Panel], active: str | None) -> None:
        for button in self.buttons.values():
            self.group.removeButton(button)
            self.tabs.removeWidget(button)
            button.hide()
            button.deleteLater()
        self.buttons.clear()
        while self.stack.count():
            widget = self.stack.widget(0)
            self.stack.removeWidget(widget)
            self.manager.shelve(widget)
        for panel in panels:
            button = QPushButton(panel.title)
            button.setCheckable(True)
            button.setStyleSheet(TAB_STYLE)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, k=panel.key: self.manager.select(k, self.mode))
            self.group.addButton(button)
            self.tabs.addWidget(button)
            self.buttons[panel.key] = button
            self.stack.addWidget(panel.widget)
            panel.widget.show()
        self.current = None
        self.setVisible(bool(panels))
        if panels:
            keys = [p.key for p in panels]
            self.choose(active if active in keys else keys[0])

    def choose(self, key: str) -> None:
        panel = self.manager.panels.get(key)
        if panel is None or key not in self.buttons:
            return
        self.current = key
        self.buttons[key].setChecked(True)
        self.stack.setCurrentWidget(panel.widget)
        if panel.on_show is not None and self.isVisible():
            panel.on_show()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().showEvent(event)
        if self.current is not None:
            panel = self.manager.panels.get(self.current)
            if panel is not None and panel.on_show is not None:
                panel.on_show()


class PanelWindow(QWidget):
    """A popped-out panel in its own window. Closing it puts the panel back in its card."""

    def __init__(self, manager: "Panels", panel: Panel):
        super().__init__(None, Qt.WindowType.Window)
        self.manager, self.key = manager, panel.key
        self.setWindowTitle(f"{panel.title} · SprigCast")
        self.setObjectName("panelWindow")
        self.setStyleSheet(f"#panelWindow {{ background: {theme.BG}; }}")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(panel.widget)
        panel.widget.show()
        geometry = QSettings().value(f"panels/window/{panel.key}")
        if geometry is None or not self.restoreGeometry(geometry):
            self.resize(380, 620)
        self._closing_quietly = False

    def take_back(self) -> None:
        """Close without docking back (the manager is moving the panel itself)."""
        self._closing_quietly = True
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        QSettings().setValue(f"panels/window/{self.key}", self.saveGeometry())
        super().closeEvent(event)
        if not self._closing_quietly:
            self.manager.dock_back(self.key)


class Panels(QObject):
    """Every panel, where each one shows, and the cards that show them."""

    changed = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.panels: dict[str, Panel] = {}
        self.cards: dict[str, PanelCard] = {m: PanelCard(self, m) for m in MODES}
        self.windows: dict[str, PanelWindow] = {}
        self.state = State.load()
        self.mode = "build"
        self._shelf = QWidget(parent)  # panels not on screen wait here, hidden
        self._shelf.hide()

    # Panels ---------------------------------------------------------------------------

    def add(self, panel: Panel, shown_in: tuple[str, ...] = ()) -> None:
        """Register a panel. `shown_in`: screens to show it on the first time it's ever added."""
        new = panel.key not in self.panels and not any(panel.key in keys for keys in self.state.shown.values())
        self.panels[panel.key] = panel
        self.shelve(panel.widget)
        if new:
            for mode in shown_in:
                if mode in panel.modes and panel.key not in self.state.shown[mode]:
                    self.state.shown[mode].append(panel.key)
            self.state.save()
        self.refresh()

    def remove(self, key: str) -> None:
        panel = self.panels.pop(key, None)
        if key in self.windows:
            self._close_window(key)
        for keys in self.state.shown.values():
            if key in keys:
                keys.remove(key)
        if key in self.state.popped:
            self.state.popped.remove(key)
        self.state.save()
        self.refresh()
        if panel is not None:
            panel.widget.deleteLater()

    def shelve(self, widget: QWidget) -> None:
        widget.hide()
        widget.setParent(self._shelf)

    # Where they show ---------------------------------------------------------------------

    def is_shown(self, key: str, mode: str | None = None) -> bool:
        return key in self.state.shown.get(mode or self.mode, [])

    def is_popped(self, key: str) -> bool:
        return key in self.windows

    def set_shown(self, key: str, shown: bool, mode: str | None = None) -> None:
        mode = mode or self.mode
        keys = self.state.shown[mode]
        if shown and key not in keys:
            keys.append(key)
            self.state.active[mode] = key
        elif not shown and key in keys:
            keys.remove(key)
        self.state.save()
        self.refresh()

    def select(self, key: str, mode: str | None = None) -> None:
        mode = mode or self.mode
        self.state.active[mode] = key
        self.state.save()
        self.cards[mode].choose(key)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.refresh()

    def pop_out(self, key: str) -> None:
        panel = self.panels.get(key)
        if panel is None or key in self.windows:
            return
        if key not in self.state.popped:
            self.state.popped.append(key)
            self.state.save()
        self.refresh()  # take it out of its card first
        window = PanelWindow(self, panel)
        self.windows[key] = window
        window.show()
        if panel.on_show is not None:
            panel.on_show()
        self.changed.emit()

    def dock_back(self, key: str) -> None:
        window = self.windows.pop(key, None)
        if window is not None:
            window.deleteLater()
        if key in self.state.popped:
            self.state.popped.remove(key)
            self.state.save()
        panel = self.panels.get(key)
        if panel is not None:
            self.shelve(panel.widget)
        self.refresh()

    def reset(self) -> None:
        for key in list(self.windows):
            self._close_window(key)
        self.state = State()
        self.state.save()
        for panel in self.panels.values():
            self.shelve(panel.widget)
        self.refresh()

    def restore_popped(self) -> None:
        """Open the windows that were popped out when SprigCast last closed."""
        for key in list(self.state.popped):
            if key in self.panels and key not in self.windows:
                self.state.popped.remove(key)
                self.pop_out(key)

    def close_windows(self) -> None:
        """On quit: close pop-outs but remember them for next time."""
        for key in list(self.windows):
            self._close_window(key)

    def refresh(self) -> None:
        for mode, card in self.cards.items():
            if mode != self.mode:
                card.show_panels([], None)
                continue
            panels = [self.panels[k] for k in self.state.shown[mode]
                      if k in self.panels and k not in self.windows and k not in self.state.popped
                      and mode in self.panels[k].modes]
            card.show_panels(panels, self.state.active.get(mode))
        self.changed.emit()

    # The Panels menu ---------------------------------------------------------------------

    def fill_menu(self, menu, extra_actions=()) -> None:
        menu.clear()
        where = menu.addAction(f"Shown on {WHERE[self.mode]}")
        where.setEnabled(False)
        offered = [p for p in self.panels.values() if self.mode in p.modes]
        for panel in offered:
            action = menu.addAction(panel.title + ("  (popped out)" if self.is_popped(panel.key) else ""))
            action.setCheckable(True)
            action.setChecked(self.is_shown(panel.key) or self.is_popped(panel.key))
            action.toggled.connect(lambda on, k=panel.key: self._menu_toggle(k, on))
        if not offered:
            empty = menu.addAction("No panels yet: add a web panel below")
            empty.setEnabled(False)
        shown = [p for p in offered if self.is_shown(p.key) and not self.is_popped(p.key)]
        if shown:
            pop = menu.addMenu("Pop out")
            for panel in shown:
                pop.addAction(panel.title).triggered.connect(lambda _=False, k=panel.key: self.pop_out(k))
        menu.addSeparator()
        for action in extra_actions:
            menu.addAction(action)

    def _menu_toggle(self, key: str, on: bool) -> None:
        if not on and key in self.windows:
            self._close_window(key)
            if key in self.state.popped:
                self.state.popped.remove(key)
            self.shelve(self.panels[key].widget)
        self.set_shown(key, on)

    def _close_window(self, key: str) -> None:
        window = self.windows.pop(key)
        window.take_back()
        window.deleteLater()

