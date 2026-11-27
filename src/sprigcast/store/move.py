"""Move: send something in a bloom somewhere else, smoothly, when you press a key, a Stream Deck button or an
app asks. Not the Move transition (that moves things between two blooms): this moves one thing inside one.

Add Move as a filter (a source's Filters window) and set where it goes: by how much from where it is, or to a
spot; its size and turn; how long and with which easing; and whether it stays there or goes back after a
moment. Then play it with its hotkey (Tools → Hotkeys → Plugins) or from an app:
    {"do": "move.play", "source": "Cam", "in": "Main"}
    {"do": "move.play", "source": "Logo"}             every place Logo shows on air
    {"do": "move.play", "source": "Logo", "back": true}   back to where it started

The engine draws the trip frame by frame (both engines), so it stays smooth while the window is busy. The
thing itself is put at the new spot at once: Build mode shows where it ends up.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import math

from sprigcast.control.native import ApiError, register_action
from sprigcast.core.filters import MOVE_EASINGS, Field, register_filter
from sprigcast.plugin_hooks import (Place, filter_on, find_filter, not_here, place_on_air, place_targets,
                                    register_hotkeys, target_by_id)

PLUGIN = {"id": "move", "name": "Move", "version": 1, "adds": "Effect",
          "about": "Slide, grow or turn something in a bloom when you press a key, a Stream Deck button or an app asks.",
          "new": "Move things in a bloom with a hotkey, Stream Deck or the Control API, with easing."}

EASING_NAMES = {"back": "Back", "bounce": "Bounce", "ease_in": "Ease in", "ease_out": "Ease out",
                "elastic": "Elastic", "linear": "Linear", "smooth": "Smooth"}

register_filter("move", "Move", "video", [
    Field("relative", "From where it is", True),
    Field("x", "Across", 0.0, -8000.0, 8000.0, 1.0, " px"),
    Field("y", "Down", 0.0, -8000.0, 8000.0, 1.0, " px"),
    Field("size", "Size", 100, 1, 1000, 1, "%"),
    Field("turn", "Turn", 0.0, -3600.0, 3600.0, 1.0, "°"),
    Field("seconds", "How long", 1.0, 0.05, 60.0, 0.05, " s"),
    Field("easing", "Easing", "smooth", choices=tuple(sorted(((k, v) for k, v in EASING_NAMES.items() if k in MOVE_EASINGS),
                                                             key=lambda kv: kv[1]))),
    Field("then", "Then", "stay", choices=(("back", "Go back"), ("stay", "Stay there"))),
    Field("hold", "Go back after", 2.0, 0.0, 600.0, 0.5, " s", show_when=("then", ("back",))),
])(None)

_PLAYING: dict[str, int] = {}  # place id -> its newest play, so an old "go back" timer does nothing


def _settings(place: Place) -> dict:
    """The move's settings: the item's own Move filter, else its source's."""
    flt = find_filter(place, "move") or find_filter(place.source, "move")
    from sprigcast.core.filters import settings_for

    return settings_for("move", dict(flt.settings) if flt else {})


def _corners(item, size):
    quad = item.quad(size) if size else None
    if not quad:
        return None
    cx = sum(p[0] for p in quad) / 4
    cy = sum(p[1] for p in quad) / 4
    width = math.dist(quad[0][:2], quad[1][:2])
    return cx, cy, width


def target_of(item, s: dict) -> tuple[float, float, float, float | None, float]:
    """Where a play sends the item: (x, y, scale, scale_y, rotation)."""
    grow = max(0.01, float(s["size"]) / 100)
    if s["relative"]:
        return (item.x + float(s["x"]), item.y + float(s["y"]), item.scale * grow,
                None if item.scale_y is None else item.scale_y * grow, item.rotation + float(s["turn"]))
    return float(s["x"]), float(s["y"]), grow, None, float(s["turn"])


def play(api, place: Place, back: bool = False) -> str:
    """Move one item: put it at its new spot now and have the engines draw the trip there. Returns its name."""
    item = place.item
    s = _settings(place)
    own = find_filter(place, "move")
    saved = dict(own.settings) if own else {}
    before = (item.x, item.y, item.scale, item.scale_y, item.rotation)
    if back:
        home = saved.get("home")
        if not home or len(home) != 5:
            return place.name  # never moved: already home
        target = tuple(home)
    else:
        target = target_of(item, s)
    sizes = getattr(api.host, "source_sizes", dict)() or {}
    size = sizes.get(item.source)
    start = _corners(item, size)
    item.x, item.y, item.scale, item.scale_y, item.rotation = target
    end = _corners(item, size)
    if start and end and end[2] > 0:
        offset = [start[0] - end[0], start[1] - end[1], start[2] / end[2], math.radians(before[4] - target[4])]
    else:  # its size isn't known yet: the turn and size still ease, the slide jumps
        ratio = (before[2] / target[2]) if target[2] else 1.0
        offset = [0.0, 0.0, ratio, math.radians(before[4] - target[4])]
    run = int(saved.get("run", 0) or 0) + 1
    seconds = float(s["seconds"])
    update = {**{k: s[k] for k in s}, "from": offset, "run": run}
    if not back and not saved.get("home"):
        update["home"] = list(before)  # where "go back" returns to, kept until it does
    if back:
        update["home"] = None
    filter_on(api, place, "move", "Move", update, seconds + 0.2)
    plays = _PLAYING[place.id] = _PLAYING.get(place.id, 0) + 1
    if not back and s["then"] == "back":
        from PySide6.QtCore import QTimer

        def home_again():
            if _PLAYING.get(place.id) != plays:
                return  # played again since: that play decides
            now = target_by_id(api.col, place.id)
            if now is not None:
                play(api, now, back=True)

        QTimer.singleShot(int((seconds + float(s["hold"])) * 1000), home_again)
    return place.name


def _places_of(api, source) -> list[Place]:
    """Every item showing this source in a bloom or sprig on air now."""
    out = []
    for scene in api.col.scenes:
        for item in scene.items:
            if item.source == source.id:
                place = Place(scene, item, source)
                if place_on_air(api.col, place):
                    out.append(place)
    return out


def _play_action(api, data):
    back = data.get("back", False)
    if not isinstance(back, bool):
        raise ApiError("bad_request", "back must be true or false.")
    if data.get("source") is None:
        raise ApiError("bad_request", "Give source (and in: a bloom or sprig, for only the one in there).")
    if data.get("in") is not None:
        places = place_targets(api, data, "source")
    else:
        places = _places_of(api, api._source(data, "source"))
        if not places:
            raise ApiError("not_allowed", "That isn't showing in a bloom on air now: give in to move one anyway.")
    return {"moving": [play(api, place, back=back) for place in places]}


register_action("move.play", "Move something in a bloom the way its Move filter says (or back where it was).",
                {"source": "name or id", "in": "?a bloom or sprig: only the one in there",
                 "back": "?true: back to where it was before it moved"}, _play_action)


def _hotkeys(collection):
    """A Move key, and a Move back key, for each item whose source (or the item itself) has a Move filter."""
    out = []
    sources = {s.id: s for s in collection.sources}
    for scene in collection.scenes:
        for item in scene.items:
            source = sources.get(item.source)
            if source is None:
                continue
            if any(f.kind == "move" for f in item.effects) or any(f.kind == "move" for f in source.filters):
                place = Place(scene, item, source)
                out.append((f"move:{place.id}", f"Move {place.name}"))
                out.append((f"move-back:{place.id}", f"Move back {place.name}"))
    return out


def _run_hotkey(api, hid):
    kind, ref = hid.split(":", 1)
    place = target_by_id(api.col, ref)
    if not isinstance(place, Place):
        return "That's gone."
    if not place_on_air(api.col, place):
        return not_here(place.name)
    play(api, place, back=kind == "move-back")
    return None


register_hotkeys(_hotkeys, _run_hotkey)
