"""What a plugin can add to SprigCast besides sources and filters: hotkeys and right-click entries.

    from sprigcast.plugin_hooks import register_hotkeys, register_item_menu

    # Hotkeys: list (id, label) pairs for Settings → Hotkeys; run(api, id) when one is pressed.
    register_hotkeys(lambda collection: [("roll:cam", "Barrel roll: Cam")], lambda api, hid: ...)

    # After each saved screenshot: shots is [{"shape", "path", "image"}, ...].
    register_on_screenshot(lambda api, shots: ...)

    # Right-click on something in a bloom: add(menu_label, callback) for each entry you want there.
    register_item_menu(lambda api, source, add: add("Barrel roll", lambda: ...))

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
    """A removed plugin's hooks stay registered until restart; skip them once its module is gone."""
    import sys

    module = getattr(fn, "__module__", "") or ""
    return not module.startswith("sprigcast_plugin_") or module in sys.modules


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


def run_hotkey(api, action: str) -> bool:
    """Run a plugin hotkey ("plugin:<id>"). True if a plugin knew it."""
    if not action.startswith(PREFIX):
        return False
    hid = action[len(PREFIX):]
    for list_fn, run_fn in _HOTKEYS:
        if not _alive(run_fn):
            continue
        try:
            if any(h == hid for h, _ in list_fn(api.col)):
                run_fn(api, hid)
                return True
        except Exception:
            log.exception("plugin hotkey %s failed", hid)
            return True
    return False


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

        QTimer.singleShot(int(seconds * 1000), lambda: _RUNS.get(key) == run and filter_off(api, source, kind))
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
