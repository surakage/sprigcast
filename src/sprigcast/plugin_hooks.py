"""What a plugin can add to SprigCast besides sources and filters: hotkeys and right-click entries.

    from sprigcast.plugin_hooks import register_hotkeys, register_item_menu

    # Hotkeys: list (id, label) pairs for Settings → Hotkeys; run(api, id) when one is pressed.
    register_hotkeys(lambda collection: [("roll:cam", "Barrel roll: Cam")], lambda api, hid: ...)
    # run may return a short note for the status bar, e.g. not_here("Cam") when it isn't on air.

    # After each saved screenshot: shots is [{"shape", "path", "image"}, ...].
    register_on_screenshot(lambda api, shots: ...)

    # Right-click on something in a bloom: add(menu_label, callback) for each entry you want there.
    register_item_menu(lambda api, source, add: add("Barrel roll", lambda: ...))

    # Look at a source's frames while it has your filter on and shows (the background remover).
    register_frame_watcher("background", lambda source_id: MyWatcher())

    # Sound levels, about 15 times a second: {source id: (peak dBFS, RMS dBFS)}, plus "master".
    register_level_watcher(lambda api, levels: ...)

    # An entry of your own in the Tools menu (Auto clip's "Suggested clips…").
    register_tool("Suggested clips…", lambda api: ...)

`api` is the SprigCast API (control/native.py NativeApi): api.run("filter.set", {...}), api.col (the
scenes and sources), api.host (the app). Plugin actions for apps go through control.native.register_action.
Hotkey ids are stored as "plugin:<id>", so keep them stable.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

PREFIX = "plugin:"
_HOTKEYS: list[tuple[Callable[[Any], list[tuple[str, str]]], Callable[[Any, str], None]]] = []
_ON_SCREENSHOT: list[Callable[[Any, list[dict]], None]] = []
_ITEM_MENUS: list[Callable[[Any, Any, Callable[[str, Callable[[], None]], None]], None]] = []


def register_hotkeys(list_fn: Callable[[Any], list[tuple[str, str]]], run_fn: Callable[[Any, str], None]) -> None:
    _HOTKEYS.append((list_fn, run_fn))


def register_item_menu(fn: Callable[[Any, Any, Callable[[str, Callable[[], None]], None]], None]) -> None:
    _ITEM_MENUS.append(fn)


class FrameWatcher:
    """Sees a source's frames while one of the plugin's filters is on it and it shows on screen.

    offer() runs on the render thread for each new frame: copy the little you need (a small picture) and
    hand it to your own thread, well under a millisecond. take() is asked every frame for a finished
    result: {"mask": H x W uint8 (255 = keep), "backdrop": h x w x 3 uint8 RGB or None}, or None when
    nothing new. close() when the filter goes, or nothing offered a frame for a while (let go of
    everything then; a new watcher is made if it's needed again).
    """

    def offer(self, frame, channel_order: str, settings: dict) -> None:  # pragma: no cover - interface
        pass

    def take(self) -> dict | None:  # pragma: no cover - interface
        return None

    def close(self) -> None:  # pragma: no cover - interface
        pass


_FRAME_WATCHERS: dict[str, Callable[[str], FrameWatcher]] = {}


def register_frame_watcher(kind: str, factory: Callable[[str], FrameWatcher]) -> None:
    """factory(source_id) makes a watcher for a source with an enabled filter of this kind."""
    _FRAME_WATCHERS[kind] = factory


def frame_watchers() -> dict[str, Callable[[str], FrameWatcher]]:
    """Filter kind -> factory, for plugins still added."""
    return {kind: fn for kind, fn in _FRAME_WATCHERS.items() if _alive(fn)}


_LEVEL_WATCHERS: list[Callable[[Any, dict], None]] = []
_TOOLS: list[tuple[str, Callable[[Any], None]]] = []


def register_level_watcher(fn: Callable[[Any, dict], None]) -> None:
    """fn(api, levels) about 15 times a second, on the UI thread, with the levels the meters show:
    {source id: (peak dBFS, RMS dBFS)} after volume, plus "master". Keep it very quick.
    A plugin loaded again (an update) replaces its own watcher rather than adding a second."""
    key = (getattr(fn, "__module__", ""), getattr(fn, "__qualname__", ""))
    _LEVEL_WATCHERS[:] = [f for f in _LEVEL_WATCHERS
                          if (getattr(f, "__module__", ""), getattr(f, "__qualname__", "")) != key] + [fn]


def levels_heard(api, levels: dict) -> None:
    for fn in list(_LEVEL_WATCHERS):
        if not _alive(fn):
            continue
        try:
            fn(api, levels)
        except Exception:
            log.exception("a plugin's level watcher failed; it won't be asked again")
            _LEVEL_WATCHERS.remove(fn)  # 15 times a second: one failure would fill the log
            return


def register_tool(label: str, fn: Callable[[Any], None]) -> None:
    """An entry in the Tools menu; fn(api) when it's picked."""
    global _TOOLS
    _TOOLS = [(lab, f) for lab, f in _TOOLS if lab != label] + [(label, fn)]  # a plugin loaded again replaces its own


def tools() -> list[tuple[str, Callable[[Any], None]]]:
    """(label, fn) for plugins still added, A-Z."""
    return sorted(((lab, fn) for lab, fn in _TOOLS if _alive(fn)), key=lambda t: t[0].lower())


def register_on_screenshot(fn: Callable[[Any, list[dict]], None]) -> None:
    """fn(api, shots) after each saved screenshot: one dict per shape with "shape", "path" and "image"
    (a QImage of what was on air). Called on the UI thread; keep it quick."""
    _ON_SCREENSHOT.append(fn)


def screenshot_taken(api, shots: list[dict]) -> None:
    for fn in _ON_SCREENSHOT:
        if not _alive(fn):
            continue
        try:
            fn(api, shots)
        except Exception:
            log.exception("a plugin's screenshot hook failed")


def _alive(fn) -> bool:
    """A removed plugin's hooks stay registered until restart; skip them once its module is gone, and
    once it's been added again (the new copy registers its own, so the old ones would show twice)."""
    import sys

    module = getattr(fn, "__module__", "") or ""
    if not module.startswith("sprigcast_plugin_"):
        return True
    loaded = sys.modules.get(module)
    if loaded is None:
        return False
    home = getattr(fn, "__globals__", None)
    return home is None or home is vars(loaded)


def hotkey_actions(collection) -> list[tuple[str, str]]:
    out = []
    for list_fn, _ in _HOTKEYS:
        if not _alive(list_fn):
            continue
        try:
            out += [(PREFIX + hid, label) for hid, label in list_fn(collection)]
        except Exception:
            log.exception("a plugin's hotkey list failed")
    return out


def run_hotkey(api, action: str, say: Callable[[str], None] | None = None) -> bool:
    """Run a plugin hotkey ("plugin:<id>"). True if a plugin knew it.

    A hotkey that can't do anything right now explains itself instead of failing silently: its
    run(api, id) returns a short note ("Not in this bloom: Cam frame"), or raises an ApiError,
    and say(note) shows it (the status bar).
    """
    if not action.startswith(PREFIX):
        return False
    hid = action[len(PREFIX):]
    for list_fn, run_fn in _HOTKEYS:
        if not _alive(run_fn):
            continue
        note = None
        try:
            if not any(h == hid for h, _ in list_fn(api.col)):
                continue
            note = run_fn(api, hid)
        except Exception as exc:
            if getattr(exc, "code", None):  # an ApiError: the plugin said no ("isn't placed yet"), not a crash
                log.info("plugin hotkey %s: %s", hid, exc)
                note = getattr(exc, "message", str(exc))
            else:
                log.exception("plugin hotkey %s failed", hid)
                note = "That hotkey's plugin hit a problem (it's in the log)."
        if isinstance(note, str) and note and say is not None:
            say(note)
        return True
    if say is not None:
        say("That hotkey's plugin isn't added any more.")
    return False


def on_air(col, source_id: str) -> bool:
    """Is this source showing in a bloom that's on air now (any shape), directly or inside a sprig?"""
    def shows(scene, seen) -> bool:
        if scene.id in seen:
            return False
        seen = seen | {scene.id}
        for item in scene.items:
            if not item.visible:
                continue
            if item.source == source_id:
                return True
            try:
                part = col.part_of(col.source(item.source))
            except StopIteration:
                continue
            if part is not None and shows(col.shape_for(part, scene.canvas), seen):
                return True
        return False

    for scene_id in col.active_scenes.values():
        try:
            if shows(col.scene(scene_id), frozenset()):
                return True
        except StopIteration:
            continue
    return False


def not_here(name: str) -> str:
    """The note a hotkey shows when what it works on isn't in the bloom on air."""
    return f"Not in this bloom: {name}"


def fill_item_menu(api, source, add: Callable[[str, Callable[[], None]], None]) -> None:
    for fn in _ITEM_MENUS:
        if not _alive(fn):
            continue
        try:
            fn(api, source, add)
        except Exception:
            log.exception("a plugin's right-click entries failed")


# Timed filters: "pixelate the camera for 15 seconds" -----------------------------------------------

_RUNS: dict[tuple[str, str], int] = {}  # (source id, filter kind) -> the newest trigger, so a re-trigger extends


def find_filter(source, kind: str):
    return next((f for f in source.filters if f.kind == kind), None)


def filter_on(api, source, kind: str, name: str, settings: dict | None = None, seconds: float = 0.0):
    """Switch on a source's filter of this kind (adding it the first time), and off again after seconds.

    seconds 0 keeps it on until filter_off. Triggering again while it's on restarts the time.
    Returns the filter.
    """
    from sprigcast.core.filters import filter_kind
    from sprigcast.core.scene import Filter

    flt = find_filter(source, kind)
    if flt is None:
        taken = {f.id for s in api.col.sources for f in s.filters}
        n = 1
        while f"filter-{n}" in taken:
            n += 1
        info = filter_kind(kind)
        flt = Filter(id=f"filter-{n}", kind=kind, name=name, settings=dict(info.defaults) if info else {})
        source.filters.append(flt)
    flt.settings = {**flt.settings, **(settings or {})}
    flt.enabled = True
    key = (source.id, kind)
    run = _RUNS.get(key, 0) + 1
    _RUNS[key] = run
    api.host.commit("filters")
    if seconds > 0:
        from PySide6.QtCore import QTimer

        def later():
            if _RUNS.get(key) != run:
                return  # triggered again (or switched off) since
            now = next((s for s in api.col.sources if s.id == key[0]), None)  # the collection may be a new copy now
            if now is not None:
                filter_off(api, now, kind)

        QTimer.singleShot(int(seconds * 1000), later)
    return flt


def filter_off(api, source, kind: str) -> bool:
    """Switch the filter off (it stays on the source with its settings). True if it was on."""
    flt = find_filter(source, kind)
    _RUNS[(source.id, kind)] = _RUNS.get((source.id, kind), 0) + 1  # a pending timer does nothing now
    if flt is None or not flt.enabled:
        return False
    flt.enabled = False
    api.host.commit("filters")
    return True
