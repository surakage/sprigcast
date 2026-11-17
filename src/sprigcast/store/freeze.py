"""Freeze: hold a sprig (or anything in a bloom) still on one frame for a while, then it moves on its own again.

Freeze your game for a 10-second redeem, stop the camera mid-laugh, or hold the whole scene for a beat.
Viewers see the picture from the moment you froze it; underneath, everything keeps running, so when the
time is up it jumps straight back to live. Pick the usual time in the Freeze filter (a source's Filters
window), or per trigger:
    {"do": "freeze.start", "sprig": "Gameplay", "seconds": 10}
    {"do": "freeze.start", "source": "Facecam"}                  (its usual time)
    {"do": "freeze.toggle", "sprig": "Gameplay"}                 (until toggled back)
    {"do": "freeze.stop", "sprig": "Gameplay"}

Right-click it in a bloom, give it a hotkey (Tools → Hotkeys → Plugins), or trigger it from an app.
Freezing again while it's frozen restarts the time and keeps the same picture.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import time

from sprigcast.control.native import ApiError, register_action
from sprigcast.core.filters import Field, register_filter
from sprigcast.plugin_hooks import filter_off, filter_on, find_filter, not_here, on_air, register_hotkeys, register_item_menu

PLUGIN = {"id": "freeze", "name": "Freeze", "version": 1, "adds": "Effect",
          "about": "Freeze a sprig, game or camera on one frame for a set time, then it's live again. Great for redeems."}

USUAL = 5.0
_TIMERS: dict[str, int] = {}  # source id -> the newest freeze, so an older one's timer doesn't end a newer one

register_filter("freeze", "Freeze", "video", [
    Field("seconds", "Usual time", USUAL, 0.0, 600.0, 0.5, " s"),
])(None)


def _usual(source) -> float:
    flt = find_filter(source, "freeze")
    return float((flt.settings if flt else {}).get("seconds", USUAL))


def frozen(source) -> bool:
    flt = find_filter(source, "freeze")
    if flt is None or not flt.enabled:
        return False
    until = float(flt.settings.get("until", 0) or 0)
    return not until or time.time() < until


def freeze(api, sources, seconds: float | None = None) -> dict:
    """Freeze each source (or sprig placement) for seconds (None: its usual time; 0: until unfrozen). One that's
    frozen already keeps its picture and gets the new time."""
    if not sources:
        raise ApiError("not_allowed", "That sprig isn't placed in any bloom yet.")
    done, secs = [], 0.0
    for source in sources:
        secs = _usual(source) if seconds is None else max(0.0, float(seconds))
        flt = find_filter(source, "freeze")
        run = int((flt.settings if flt else {}).get("run", 0) or 0)
        if not frozen(source):
            run += 1  # a new freeze: it catches the picture showing now
        until = time.time() + secs if secs > 0 else 0
        filter_on(api, source, "freeze", "Freeze", {"run": run, "until": until})
        _end_later(api, source.id, secs)
        done.append(source.name)
    return {"frozen": done, "seconds": secs or None}


def _end_later(api, source_id: str, seconds: float) -> None:
    """Let it go after seconds (0: never). Its end time is cleared then, so switching the filter on by hand in
    the Filters window later freezes it again. (The end time is only there so a freeze saved on when SprigCast
    closed mid-freeze doesn't hold forever after a restart.)"""
    n = _TIMERS[source_id] = _TIMERS.get(source_id, 0) + 1
    if seconds <= 0:
        return
    from PySide6.QtCore import QTimer

    def later():
        if _TIMERS.get(source_id) != n:
            return  # frozen again (or let go) since
        source = next((s for s in api.col.sources if s.id == source_id), None)  # the collection may be a new copy
        if source is not None:
            unfreeze(api, [source])

    QTimer.singleShot(int(seconds * 1000), later)


def unfreeze(api, sources) -> dict:
    was = []
    for source in sources:
        _TIMERS[source.id] = _TIMERS.get(source.id, 0) + 1
        flt = find_filter(source, "freeze")
        if flt is not None:
            flt.settings = {**flt.settings, "until": 0}
        if filter_off(api, source, "freeze"):
            was.append(source.name)
    return {"frozen": [], "was": was}


def _placed_in(col, sprig):
    """The sources that place this sprig in blooms (every shape)."""
    ids = {s.id for s in col.shapes(sprig)} | {sprig.id}
    out = []
    for source in col.sources:
        if source.kind != "scene":
            continue
        part = col.part_of(source)
        if source.settings.get("scene") in ids or (part is not None and part.id in ids):
            out.append(source)
    return out


def _targets(api, data):
    if data.get("sprig") is not None:
        return _placed_in(api.col, api._sprig(data))
    if data.get("source") is not None:
        return [api._source(data, "source")]
    raise ApiError("bad_request", "Give sprig or source.")


def _seconds(data) -> float | None:
    value = data.get("seconds")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError("bad_request", "seconds must be a number.")
    if not 0 <= value <= 600:
        raise ApiError("bad_request", "seconds must be 0 to 600.")
    return float(value)


def _start(api, data):
    seconds = _seconds(data)
    return freeze(api, _targets(api, data), seconds)


def _toggle(api, data):
    sources = _targets(api, data)
    if any(frozen(s) for s in sources):
        return unfreeze(api, sources)
    return freeze(api, sources, 0)


register_action("freeze.start", "Freeze a sprig (every place it's shown) or one thing in a bloom on its current frame "
                "for a while (no seconds: its usual time; 0: until freeze.stop).",
                {"sprig": "?name or id", "source": "?name or id (instead of sprig)", "seconds": "?how long"}, _start)
register_action("freeze.stop", "Let a frozen sprig or source move again.",
                {"sprig": "?name or id", "source": "?name or id (instead of sprig)"},
                lambda api, data: unfreeze(api, _targets(api, data)))
register_action("freeze.toggle", "Freeze a sprig or source until toggled again, or let it go if it's frozen.",
                {"sprig": "?name or id", "source": "?name or id (instead of sprig)"}, _toggle)


# Hotkeys and right-click ------------------------------------------------------------------------------

def _hotkeys(collection):
    """A Freeze key for each sprig that's placed in a bloom, and for each capture and camera."""
    out = [(f"freeze:sprig:{part.id}", f"Freeze {part.name}") for part in collection.parts()
           if _placed_in(collection, part)]
    out += [(f"freeze:source:{source.id}", f"Freeze {source.name}") for source in collection.sources
            if source.kind in ("camera", "display_capture", "window_capture", "game_capture")]
    return out


def _run_hotkey(api, hid):
    _, what, ref = hid.split(":", 2)
    if what == "sprig":
        sprig = next((p for p in api.col.parts() if p.id == ref), None)
        if sprig is None:
            return "That sprig is gone."
        sources, name = _placed_in(api.col, sprig), sprig.name
    else:
        source = next((s for s in api.col.sources if s.id == ref), None)
        if source is None:
            return "That source is gone."
        sources, name = [source], source.name
    if not any(on_air(api.col, s.id) for s in sources):  # freezing what isn't on air does nothing you'd see
        return not_here(name)
    freeze(api, sources)
    return None


register_hotkeys(_hotkeys, _run_hotkey)


def _menu(api, source, add):
    if source is None or source.kind in ("audio_input", "audio_output", "app_audio"):
        return
    if frozen(source):
        add("Unfreeze", lambda: unfreeze(api, [source]))
    else:
        usual = _usual(source)
        add(f"Freeze for {usual:g} s" if usual > 0 else "Freeze", lambda: freeze(api, [source]))


register_item_menu(_menu)
