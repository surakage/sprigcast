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
PAUSE_RECORD = "pause_record"  # pause the recording / carry on in the same file
CHAPTER_MARK = "chapter_mark"  # a chapter mark in the recording at this moment
SAVE_REPLAY = "save_replay"  # save the replay buffer's last seconds as clips
SCREENSHOT = "screenshot"  # a PNG of each shape on air, in the recordings folder
TOGGLE_CLIPS = "toggle_clips"  # keep clips ready (replay buffer) on / off
TOGGLE_MODE = "toggle_mode"  # switch between Build and Live
MUTE_ALL = "mute_all"  # mute every sound, or unmute them all when everything is muted
SCENE_PREFIX = "scene:"
SOUND_PREFIX = "sound:"  # "sound:<source id>:<toggle|mute|unmute>"
MEDIA_PREFIX = "media:"  # "media:<source id>:<toggle|restart|stop>"
STATE_PREFIX = "state:"  # "state:<sprig id>:<state name>"; an empty name goes back to as built
GAME_PREFIX = "game:"  # "game:<source id>": a game capture takes the window in front

_MODIFIERS = {"ctrl": "<ctrl>", "alt": "<alt>", "shift": "<shift>", "meta": "<cmd>"}
_NAMED = {
    "space": "<space>", "tab": "<tab>", "esc": "<esc>", "return": "<enter>", "enter": "<enter>",
    "ins": "<insert>", "del": "<delete>", "home": "<home>", "end": "<end>",
    "pgup": "<page_up>", "pgdown": "<page_down>", "left": "<left>", "right": "<right>",
    "up": "<up>", "down": "<down>", "pause": "<pause>", "print": "<print_screen>",
    "scrolllock": "<scroll_lock>", "backspace": "<backspace>",
}


AUDIO_KINDS = ("audio_input", "audio_output", "app_audio")


def _no_shelf() -> list[tuple[str, str]]:
    return []


# "garden:<id>" / "path:<id>" actions, set by the window's GardenControl (app/gardens_menu.py).
shelf_actions = _no_shelf


def sections(collection: SceneCollection) -> list[tuple[str, list[tuple[str, str]]]]:
    """Everything a hotkey can do, grouped as the Hotkeys page shows it: (title, [(action id, label)])."""
    groups: list[tuple[str, list[tuple[str, str]]]] = [
        ("Going live", [(TOGGLE_STREAM, "Go live / End stream"), (TOGGLE_RECORD, "Start / Stop recording"),
                        (PAUSE_RECORD, "Pause / Resume recording"), (CHAPTER_MARK, "Add a chapter mark")]),
        ("Clips and screenshots", [(SAVE_REPLAY, "Save a clip"), (TOGGLE_CLIPS, "Keep clips ready on / off"),
                                   (SCREENSHOT, "Take a screenshot")]),
    ]
    blooms = [(SCENE_PREFIX + scene.id, f"{scene.name} ({canvas.id})")
              for canvas in collection.canvases for scene in collection.scenes_for(canvas.id)]
    groups.append(("Show a bloom", blooms))
    sounds = [(MUTE_ALL, "Mute everything / unmute")]
    for source in collection.sources:
        if source.kind in AUDIO_KINDS:
            sounds += [(f"{SOUND_PREFIX}{source.id}:toggle", f"{source.name}: mute / unmute"),
                       (f"{SOUND_PREFIX}{source.id}:mute", f"{source.name}: mute"),
                       (f"{SOUND_PREFIX}{source.id}:unmute", f"{source.name}: unmute")]
    groups.append(("Sound", sounds))
    looks = []
    for part in collection.parts():
        if part.states:
            looks.append((f"{STATE_PREFIX}{part.id}:", f"{part.name}: as built"))
        for state in part.states:
            looks.append((f"{STATE_PREFIX}{part.id}:{state.name}", f"{part.name}: {state.name}"))
    groups.append(("Sprig looks", looks))
    media = []
    for source in collection.sources:
        if source.kind == "media":
            for what, label in (("toggle", "play / pause"), ("restart", "start over"), ("stop", "stop")):
                media.append((f"{MEDIA_PREFIX}{source.id}:{what}", f"{source.name}: {label}"))
    groups.append(("Videos", media))
    games = [(f"{GAME_PREFIX}{source.id}", f"{source.name}: capture the window in front")
             for source in collection.sources if source.kind == "game_capture"
             and source.settings.get("mode") == "hotkey"]
    groups.append(("Game capture", games))
    from sprigcast import plugin_hooks

    groups.append(("Plugins", plugin_hooks.hotkey_actions(collection)))
    groups.append(("Gardens and paths", shelf_actions()))
    groups.append(("SprigCast", [(TOGGLE_MODE, "Switch Build / Live")]))
    return [(title, items) for title, items in groups if items]


def actions(collection: SceneCollection) -> list[tuple[str, str]]:
    """(action id, label) for everything a hotkey can do."""
    return [item for _title, items in sections(collection) for item in items]


# Keys Windows or every app already uses: a hotkey there would fire each time you copy, paste or switch apps.
TAKEN = {
    "alt+tab": "Windows uses it to switch apps", "alt+f4": "Windows uses it to close apps",
    "ctrl+alt+del": "Windows keeps it for itself", "ctrl+shift+esc": "Windows uses it for Task Manager",
    "ctrl+esc": "Windows uses it for Start", "alt+esc": "Windows uses it to switch apps",
    "ctrl+c": "every app uses it to copy", "ctrl+v": "every app uses it to paste",
    "ctrl+x": "every app uses it to cut", "ctrl+z": "every app uses it to undo", "ctrl+y": "every app uses it to redo",
    "ctrl+a": "every app uses it to select all", "ctrl+s": "every app uses it to save",
    "ctrl+f": "every app uses it to find", "ctrl+w": "apps use it to close a tab", "ctrl+t": "browsers use it",
    "ctrl+tab": "apps use it to switch tabs", "ctrl+p": "every app uses it to print", "f1": "apps use it for help",
}


def taken_by(combo: str) -> str:
    """Why this key is already Windows' or every app's ("" when it's free). Any Windows-key combo is."""
    low = combo.replace(" ", "").lower()
    if low.startswith("meta+") or "+meta+" in low:
        return "Windows keeps Windows-key combos for itself"
    return TAKEN.get(low, "")


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


class PushKey:
    """The push-to-talk key: calls on_change(True) when the whole combo is down and on_change(False) once any of it
    is let go. It only says when; the mixer opens and closes the mic per block (with "Let go after").

    Heard globally with pynput, so it works while a game has focus. Without pynput there is no push to talk:
    `mode` says "off" and the mic stays as it is."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.mode = "off"
        self.combo = ""
        self._listener = None
        self._down = False

    def apply(self, combo: str, active: bool = True) -> None:
        if combo == self.combo and active == (self._listener is not None):
            return
        self.stop()
        self.combo = combo
        key = to_pynput(combo) if active else None
        if key is None:
            return
        try:
            from pynput import keyboard

            wanted = set(keyboard.HotKey.parse(key))
            held: set = set()

            def changed() -> None:
                down = wanted <= held
                if down != self._down:
                    self._down = down
                    self.on_change(down)

            def press(k) -> None:
                held.add(listener.canonical(k))
                changed()

            def release(k) -> None:
                held.discard(listener.canonical(k))
                changed()

            listener = keyboard.Listener(on_press=press, on_release=release)
            listener.daemon = True
            listener.start()
            self._listener = listener
            self.mode = "global"
        except Exception as exc:  # pynput missing or not supported here
            log.info("push to talk unavailable (%s)", exc)
            self._listener = None
            self.mode = "off"

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
        if self._down:  # a key held as it stops counts as let go
            self._down = False
            self.on_change(False)
        self.mode = "off"
        self.combo = ""
