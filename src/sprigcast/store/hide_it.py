"""Hide it: pixelate a source or black it out for a while, then it comes back on its own.

For a camera, a game capture or a screen: a redeem that pixelates you for 15 seconds, or a quick
privacy button (no time: stays hidden until you show it again). Hide it adds two filters, Pixelate
and Blackout, that you can also tune in a source's Filters window (block size, how dark, how long).

Trigger it from the right-click menu, a hotkey (Settings → Hotkeys → Plugins) or an app:
    {"do": "hide.pixelate", "source": "Facecam", "seconds": 15}
    {"do": "hide.blackout", "source": "Game capture"}            (its usual time)
    {"do": "hide.toggle", "source": "Facecam", "how": "blackout"} (until toggled back)
    {"do": "hide.show", "source": "Facecam"}

A SprigCast store plugin: add it from Settings → Plugins.
"""

from __future__ import annotations

from sprigcast.control.native import ApiError, register_action
from sprigcast.core.filters import Field, register_filter
from sprigcast.plugin_hooks import filter_off, filter_on, find_filter, not_here, on_air, register_hotkeys, register_item_menu

PLUGIN = {"id": "hide_it", "name": "Hide it", "version": 2, "adds": "Effect",
          "about": "Pixelate a camera, game or screen, or black it out, for a set time. Great for redeems."}

KINDS = {"pixelate": "Pixelate", "blackout": "Blackout"}
VIDEO = ("camera", "display_capture", "window_capture", "game_capture", "media", "browser", "image", "scene")

register_filter("pixelate", "Pixelate", "video", [
    Field("size", "Block size", 24, 2, 200, 1, " px"),
    Field("seconds", "Usual time", 15.0, 0.0, 600.0, 1.0, " s"),
])(None)
register_filter("blackout", "Blackout", "video", [
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


def _source(api, data):
    return api._source(data, "source")


def _seconds(data) -> float | None:
    value = data.get("seconds")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError("bad_request", "seconds must be a number.")
    return float(value)


def _pixelate(api, data):
    extra = {}
    if data.get("size") is not None:
        extra["size"] = max(2, int(data["size"]))
    return hide(api, _source(api, data), "pixelate", _seconds(data), **extra)


register_action("hide.pixelate", "Pixelate a source for a while (no seconds: its usual time; 0: until hide.show).",
                {"source": "name or id", "seconds": "?how long", "size": "?block size in pixels"}, _pixelate)
register_action("hide.blackout", "Black out a source for a while (no seconds: its usual time; 0: until hide.show).",
                {"source": "name or id", "seconds": "?how long"},
                lambda api, data: hide(api, _source(api, data), "blackout", _seconds(data)))
register_action("hide.show", "Show a hidden source again.", {"source": "name or id"},
                lambda api, data: show(api, _source(api, data)))


def _toggle(api, data):
    source = _source(api, data)
    how = data.get("how", "pixelate")
    if how not in KINDS:
        raise ApiError("bad_request", "how must be pixelate or blackout.")
    return show(api, source) if hidden(source) else hide(api, source, how, 0)


register_action("hide.toggle", "Hide a source until toggled again, or show it if it's hidden.",
                {"source": "name or id", "how": "?pixelate (default) or blackout"}, _toggle)


# Hotkeys and right-click ------------------------------------------------------------------------------

def _hotkeys(collection):
    out = []
    for source in collection.sources:
        if source.kind in ("camera", "display_capture", "window_capture", "game_capture"):
            out += [(f"hide:pixelate:{source.id}", f"Pixelate {source.name}"),
                    (f"hide:blackout:{source.id}", f"Black out {source.name}"),
                    (f"hide:show:{source.id}", f"Show {source.name} again")]
    return out


def _run_hotkey(api, hid):
    _, what, source_id = hid.split(":", 2)
    source = next((s for s in api.col.sources if s.id == source_id), None)
    if source is None:
        return "That source is gone."
    if what == "show":
        show(api, source)
        return None
    if not on_air(api.col, source.id):  # hiding what isn't on air does nothing you'd see: say so
        return not_here(source.name)
    hide(api, source, what)
    return None


register_hotkeys(_hotkeys, _run_hotkey)


def _menu(api, source, add):
    if source is None or source.kind not in VIDEO:
        return
    if hidden(source):
        add("Show it again", lambda: show(api, source))
    add(f"Pixelate for {_usual(source, 'pixelate'):g} s", lambda: hide(api, source, "pixelate"))
    add(f"Black out for {_usual(source, 'blackout'):g} s", lambda: hide(api, source, "blackout"))


register_item_menu(_menu)
