"""Barrel Roll: spin a sprig (or anything in a bloom) round and round, then land exactly where it was.

It eases in, spins whole turns and eases out, so it always ends the right way up. Pick how long it
spins and how many turns in the Barrel roll filter (a source's Filters window), or per trigger:
    {"do": "roll.spin", "sprig": "Cam frame", "seconds": 15}
    {"do": "roll.spin", "source": "Logo", "turns": 3, "seconds": 2, "direction": "anticlockwise"}

Right-click it in a bloom, give it a hotkey (Tools → Hotkeys → Plugins), or trigger it from an app.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

from sprigcast.control.native import ApiError, register_action
from sprigcast.core.filters import Field, register_filter
from sprigcast.plugin_hooks import filter_on, find_filter, not_here, on_air, register_hotkeys, register_item_menu

PLUGIN = {"id": "barrel_roll", "name": "Barrel Roll", "version": 3, "adds": "Effect",
          "about": "Spin a sprig round and round for a few seconds, then it lands back exactly where it was.",
          "new": "Apps can't ask for more than 10 minutes."}

TURNS_PER_SECOND = 1.0  # when a trigger gives seconds but no turns

register_filter("barrel_roll", "Barrel roll", "video", [
    Field("seconds", "Spin for", 3.0, 0.5, 120.0, 0.5, " s"),
    Field("turns", "Turns", 3, 1, 200, 1),
    Field("direction", "Direction", "clockwise", choices=(("clockwise", "Clockwise"), ("anticlockwise", "Anticlockwise"))),
])(None)


def _placements(api, sprig):
    """The sources that place this sprig in blooms (every shape)."""
    return _placed_in(api.col, sprig)


def _placed_in(col, sprig):
    ids = {s.id for s in col.shapes(sprig)} | {sprig.id}
    out = []
    for source in col.sources:
        if source.kind != "scene":
            continue
        part = col.part_of(source)
        if source.settings.get("scene") in ids or (part is not None and part.id in ids):
            out.append(source)
    return out


def spin(api, sources, seconds: float | None = None, turns: int | None = None, direction: str | None = None) -> dict:
    if not sources:
        raise ApiError("not_allowed", "That sprig isn't placed in any bloom yet.")
    done = []
    for source in sources:
        flt = find_filter(source, "barrel_roll")
        usual = flt.settings if flt else {}
        secs = float(seconds if seconds is not None else usual.get("seconds", 3.0))
        if not 0 < secs <= 600:
            raise ApiError("bad_request", "seconds must be more than 0 and at most 600.")
        if turns is None:
            n = int(usual.get("turns", 3)) if seconds is None else max(1, round(secs * TURNS_PER_SECOND))
        else:
            n = max(1, int(turns))
        settings = {"seconds": secs, "turns": n, "run": int(usual.get("run", 0)) + 1}
        if direction in ("clockwise", "anticlockwise"):
            settings["direction"] = direction
        filter_on(api, source, "barrel_roll", "Barrel roll", settings, secs + 0.25)  # off just after it lands
        done.append(source.name)
    return {"spinning": done, "seconds": secs, "turns": n}


def _spin_action(api, data):
    seconds, turns = data.get("seconds"), data.get("turns")
    for key, value in (("seconds", seconds), ("turns", turns)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ApiError("bad_request", f"{key} must be a number.")
    direction = data.get("direction")
    if direction is not None and direction not in ("clockwise", "anticlockwise"):
        raise ApiError("bad_request", "direction must be clockwise or anticlockwise.")
    if data.get("sprig") is not None:
        sources = _placements(api, api._sprig(data))
    elif data.get("source") is not None:
        sources = [api._source(data, "source")]
    else:
        raise ApiError("bad_request", "Give sprig or source.")
    return spin(api, sources, seconds, turns, direction)


register_action("roll.spin", "Barrel roll a sprig (every place it's shown) or one thing in a bloom.",
                {"sprig": "?name or id", "source": "?name or id (instead of sprig)", "seconds": "?how long",
                 "turns": "?whole turns (default: one a second)", "direction": "?clockwise or anticlockwise"},
                _spin_action)


def _hotkeys(collection):
    """A Barrel roll key for each sprig that's placed in a bloom (an unused sprig has nothing to spin)."""
    return [(f"roll:{part.id}", f"Barrel roll {part.name}") for part in collection.parts() if _placed_in(collection, part)]


def _run_hotkey(api, hid):
    part_id = hid.split(":", 1)[1]
    sprig = next((p for p in api.col.parts() if p.id == part_id), None)
    placed = _placements(api, sprig) if sprig is not None else []
    if sprig is None:
        return "That sprig is gone."
    if not any(on_air(api.col, p.id) for p in placed):  # nothing on air would move: say so
        return not_here(sprig.name)
    spin(api, placed)
    return None


register_hotkeys(_hotkeys, _run_hotkey)


def _menu(api, source, add):
    if source is None or source.kind in ("audio_input", "audio_output", "app_audio"):
        return
    flt = find_filter(source, "barrel_roll")
    secs = float((flt.settings if flt else {}).get("seconds", 3.0))
    add(f"Barrel roll ({secs:g} s)", lambda: spin(api, [source]))


register_item_menu(_menu)
