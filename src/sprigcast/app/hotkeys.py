"""Hotkeys that work even while a game or another app has focus.

Combos are stored the way Qt writes them ("Ctrl+Alt+1") and handed to pynput's global
keyboard hook. If pynput can't start (no permission, unsupported platform), the same combos
become ordinary shortcuts that work while SprigCast's window is focused.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QWidget

from sprigcast.core.scene import SceneCollection

log = logging.getLogger(__name__)

TOGGLE_STREAM = "toggle_stream"
TOGGLE_RECORD = "toggle_record"
SCENE_PREFIX = "scene:"

_MODIFIERS = {"ctrl": "<ctrl>", "alt": "<alt>", "shift": "<shift>", "meta": "<cmd>"}
_NAMED = {
    "space": "<space>", "tab": "<tab>", "esc": "<esc>", "return": "<enter>", "enter": "<enter>",
    "ins": "<insert>", "del": "<delete>", "home": "<home>", "end": "<end>",
    "pgup": "<page_up>", "pgdown": "<page_down>", "left": "<left>", "right": "<right>",
    "up": "<up>", "down": "<down>", "pause": "<pause>", "print": "<print_screen>",
    "scrolllock": "<scroll_lock>", "backspace": "<backspace>",
}


def actions(collection: SceneCollection) -> list[tuple[str, str]]:
    """(action id, label) for everything a hotkey can do."""
    items = [(TOGGLE_STREAM, "Go live / End stream"), (TOGGLE_RECORD, "Start / Stop recording")]
    for canvas in collection.canvases:
        for scene in collection.scenes_for(canvas.id):
            items.append((SCENE_PREFIX + scene.id, f"Show scene: {scene.name} ({canvas.id})"))
    return items


def to_pynput(combo: str) -> str | None:
    """Qt's "Ctrl+Alt+F5" as pynput's "<ctrl>+<alt>+<f5>". None if a key can't be expressed."""
    if not combo:
        return None
    parts = [p for p in combo.replace(" ", "").split("+") if p] if combo != "+" else ["+"]
    if combo.endswith("++"):
        parts.append("+")
    out = []
    for part in parts:
        low = part.lower()
        if low in _MODIFIERS:
            out.append(_MODIFIERS[low])
        elif low in _NAMED:
            out.append(_NAMED[low])
        elif low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
            out.append(f"<{low}>")
        elif len(part) == 1:
            out.append(low)
        else:
            return None
    if not out or all(p in _MODIFIERS.values() for p in out):
        return None  # modifiers alone are not a hotkey
    return "+".join(out)


class Hotkeys(QObject):
    """Turns the collection's hotkeys into `triggered(action)` signals on the UI thread."""

    triggered = Signal(str)

    def __init__(self, window: QWidget):
        super().__init__(window)
        self.window = window
        self.mode = "off"  # "global", "window" (fallback) or "off"
        self._listener = None
        self._shortcuts: list[QShortcut] = []

    def apply(self, hotkeys: dict[str, str]) -> None:
        self.stop()
        bindings = {combo: action for action, combo in hotkeys.items() if combo}
        if not bindings:
            return
        try:
            from pynput import keyboard

            mapping = {}
            for combo, action in bindings.items():
                key = to_pynput(combo)
                if key is None:
                    log.warning("hotkey %r for %s can't be used globally", combo, action)
                    continue
                # pynput calls these on its own thread; emitting a signal hands them to the UI thread.
                mapping[key] = lambda a=action: self.triggered.emit(a)
            self._listener = keyboard.GlobalHotKeys(mapping)
            self._listener.daemon = True
            self._listener.start()
            self.mode = "global"
        except Exception as exc:  # pynput missing or not supported here: fall back to in-app shortcuts
            log.info("global hotkeys unavailable (%s); using window shortcuts", exc)
            self._listener = None
            for combo, action in bindings.items():
                shortcut = QShortcut(QKeySequence(combo), self.window)
                shortcut.setContext(shortcut.context().ApplicationShortcut)
                shortcut.activated.connect(lambda a=action: self.triggered.emit(a))
                self._shortcuts.append(shortcut)
            self.mode = "window"

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        for shortcut in self._shortcuts:
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        self._shortcuts = []
        self.mode = "off"
