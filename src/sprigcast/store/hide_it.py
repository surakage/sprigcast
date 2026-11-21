"""Hide it: pixelate a source or black it out for a while, then it comes back on its own.

For a camera, a game capture or a screen: a redeem that pixelates you for 15 seconds, or a quick
privacy button (no time: stays hidden until you show it again). Hide it adds two filters, Pixelate
and Blackout, that you can also tune in a source's Filters window (block size, how dark, how long).

Right-click something in a bloom or sprig to hide just that one (the same camera elsewhere stays as it is);
it then has its own hotkey (Tools → Hotkeys → Plugins). Apps name the source, and "in" for just the one in a
bloom or sprig (without "in", the source everywhere it shows):
    {"do": "hide.pixelate", "source": "Facecam", "in": "Gameplay", "seconds": 15}
    {"do": "hide.pixelate", "sprig": "Elgato", "in": "Gameplay"}  (a sprig placed in there)
    {"do": "hide.pixelate", "source": "Facecam", "seconds": 15}
    {"do": "hide.blackout", "source": "Game capture"}            (its usual time)
    {"do": "hide.toggle", "source": "Facecam", "how": "blackout"} (until toggled back)
    {"do": "hide.show", "source": "Facecam"}

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

from sprigcast.control.native import ApiError, register_action
from sprigcast.core.filters import Field, register_filter
from sprigcast.plugin_hooks import (Place, filter_off, filter_on, find_filter, not_here, on_air, place_on_air,
                                    place_targets, register_hotkeys, register_place_menu, target_by_id)

PLUGIN = {"id": "hide_it", "name": "Hide it", "version": 4, "adds": "Effect",
          "about": "Pixelate a camera, game or screen, or black it out, for a set time. Great for redeems.",
          "new": "Hides only the one you right-click, not the same camera everywhere."}

KINDS = {"pixelate": "Mosaic", "blackout": "Curtain"}  # the names a filter gets when one is added
VIDEO = ("camera", "display_capture", "window_capture", "game_capture", "media", "browser", "image", "scene")

register_filter("pixelate", "Mosaic", "video", [
    Field("size", "Block size", 24, 2, 200, 1, " px"),
    Field("seconds", "Usual time", 15.0, 0.0, 600.0, 1.0, " s"),
])(None)
register_filter("blackout", "Curtain", "video", [
    Field("amount", "How dark", 100, 0, 100, 1, "%"),
    Field("seconds", "Usual time", 15.0, 0.0, 600.0, 1.0, " s"),
])(None)


def _usual(source, kind: str) -> float:
    flt = find_filter(source, kind)
    return float((flt.settings if flt else {}).get("seconds", 15.0))


def hide(api, source, kind: str, seconds: float | None = None, **settings) -> dict:
    seconds = _usual(source, kind) if seconds is None else max(0.0, float(seconds))
    for other in KINDS:  # one at a time: switching from pixelate to blackout replaces it
        if other != kind:
            filter_off(api, source, other)
    filter_on(api, source, kind, KINDS[kind], settings, seconds)
    return {"source": source.name, "hidden": kind, "seconds": seconds or None}


def show(api, source) -> dict:
    was = [kind for kind in KINDS if filter_off(api, source, kind)]
    return {"source": source.name, "hidden": None, "was": was[0] if was else None}


def hidden(source) -> str | None:
    return next((f.kind for f in source.filters if f.kind in KINDS and f.enabled), None)


def _targets(api, data) -> list:
    """The items named ("in" a bloom or sprig), else the source everywhere."""
    if data.get("in") is not None:
        return place_targets(api, data, "sprig" if data.get("sprig") is not None else "source")
    return [api._source(data, "source")]


def _each(targets, fn) -> dict:
    out = [fn(t) for t in targets]
    return out[0] if len(out) == 1 else {"done": out}


def _seconds(data) -> float | None:
    value = data.get("seconds")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError("bad_request", "seconds must be a number.")
    if not 0 <= value <= 600:
        raise ApiError("bad_request", "seconds must be 0 to 600.")
    return float(value)


def _pixelate(api, data):
    extra = {}
    if data.get("size") is not None:
        extra["size"] = max(2, int(data["size"]))
    seconds = _seconds(data)
    return _each(_targets(api, data), lambda t: hide(api, t, "pixelate", seconds, **extra))


def _blackout(api, data):
    seconds = _seconds(data)
    return _each(_targets(api, data), lambda t: hide(api, t, "blackout", seconds))


_WHERE = {"source": "?name or id", "sprig": "?with in: that sprig placed in there",
          "in": "?a bloom or sprig: only the one in there (without it: the source everywhere)"}
register_action("hide.pixelate", "Pixelate a source for a while (no seconds: its usual time; 0: until hide.show).",
                {**_WHERE, "seconds": "?how long", "size": "?block size in pixels"}, _pixelate)
register_action("hide.blackout", "Black out a source for a while (no seconds: its usual time; 0: until hide.show).",
                {**_WHERE, "seconds": "?how long"},
                lambda api, data: _blackout(api, data))
register_action("hide.show", "Show a hidden source again.", dict(_WHERE),
                lambda api, data: _each(_targets(api, data), lambda t: show(api, t)))


def _toggle(api, data):
    how = data.get("how", "pixelate")
    if how not in KINDS:
        raise ApiError("bad_request", "how must be pixelate or blackout.")
    return _each(_targets(api, data), lambda t: show(api, t) if hidden(t) else hide(api, t, how, 0))


register_action("hide.toggle", "Hide a source until toggled again, or show it if it's hidden.",
                {**_WHERE, "how": "?pixelate (default) or blackout"}, _toggle)


# Hotkeys and right-click ------------------------------------------------------------------------------

CAPTURES = ("camera", "display_capture", "window_capture", "game_capture")


def _hotkeys(collection):
    """Keys for each camera or capture in each bloom or sprig, and anything else hidden there once: each one
    hides only that one (the same camera elsewhere stays as it is)."""
    kinds = {s.id: s for s in collection.sources}
    out = []
    for scene in collection.scenes:
        for item in scene.items:
            source = kinds.get(item.source)
            if source is None or not (source.kind in CAPTURES or any(e.kind in KINDS for e in item.effects)):
                continue
            place = Place(scene, item, source)
            out += [(f"hide:pixelate:{place.id}", f"Pixelate {place.name}"),
                    (f"hide:blackout:{place.id}", f"Black out {place.name}"),
                    (f"hide:show:{place.id}", f"Show {place.name} again")]
    return out


def _run_hotkey(api, hid):
    _, what, target_id = hid.split(":", 2)
    target = target_by_id(api.col, target_id)  # an item ("scene/source"), or a source from before items had keys
    if target is None:
        return "That's gone."
    if what == "show":
        show(api, target)
        return None
    showing = place_on_air(api.col, target) if isinstance(target, Place) else on_air(api.col, target.id)
    if not showing:  # hiding what isn't on air does nothing you'd see: say so
        return not_here(target.name)
    hide(api, target, what)
    return None


register_hotkeys(_hotkeys, _run_hotkey)


def _menu(api, place, add):
    if place is None or place.kind not in VIDEO:
        return
    if hidden(place):
        add("Show it again", lambda: show(api, place))
    add(f"Pixelate for {_usual(place, 'pixelate'):g} s", lambda: hide(api, place, "pixelate"))
    add(f"Black out for {_usual(place, 'blackout'):g} s", lambda: hide(api, place, "blackout"))


register_place_menu(_menu)
