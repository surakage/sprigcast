"""Move: send something in a bloom somewhere else, smoothly, when you press a key, a Stream Deck button, an app
asks, or something happens (its bloom shows, you go live, its video ends...). Not the Move transition (that
moves things between two blooms): this moves things inside one.

Four filters, each added from a source's Filters window:
  Move        where it goes (by how much from where it is, or to a spot), its size and turn, or trade places
              with another item; how long, with which easing; what starts it; whether it stays or goes back;
              and something else to do too (show it, hide it at the end, bring it to the front, play its video...).
  Move value  slide one of its settings, or one of a filter's (blur strength, a text's size, a color).
  Sound move  bounce, grow or wobble while a mic or another sound is loud.
  Camera move turn a webcam's own pan, tilt and zoom (cameras that can, on Windows).
Each one plays with its hotkey (Tools → Hotkeys → Plugins) or from an app:
    {"do": "move.play", "source": "Cam", "in": "Main"}
    {"do": "move.play", "source": "Logo"}                every place Logo shows on air
    {"do": "move.play", "source": "Logo", "back": true}  back to where it started
    {"do": "move.value", "source": "Title"}  /  {"do": "move.camera", "source": "Cam"}

The engine draws a trip frame by frame (both engines), so it stays smooth while the window is busy. The thing
itself is put at the new spot at once: Build mode shows where it ends up.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import math
import threading
import time

from sprigcast.control.native import ApiError, register_action
from sprigcast.core.filters import MOVE_EASINGS, Field, ease, register_filter
from sprigcast.plugin_hooks import (Place, filter_on, find_filter, not_here, output_since, place_on_air,
                                    place_targets, register_hotkeys, register_level_watcher, target_by_id)

PLUGIN = {"id": "move", "name": "Move", "version": 2, "adds": "Effect",
          "about": "Slide, grow or turn something in a bloom, slide a setting, bounce to your voice or turn a "
                   "webcam, when you press a key, a Stream Deck button, an app asks or something happens.",
          "new": "Moves start on their own too (a bloom shows, going live, a video ends...), trade places, slide "
                 "settings, bounce to a mic and turn webcams."}

EASING_NAMES = {"back": "Back", "bounce": "Bounce", "ease_in": "Ease in", "ease_out": "Ease out",
                "elastic": "Elastic", "linear": "Linear", "smooth": "Smooth"}

# What starts a move besides its hotkey and apps (which always can). A-Z by what the list shows.
WHEN = (("after", "Another move ends"), ("asked", "Hotkey or app only"), ("live", "Going live"),
        ("hidden", "Its bloom goes off air"), ("shown", "Its bloom shows"), ("media_end", "Its video ends"),
        ("media_start", "Its video starts"), ("off", "It's switched off"), ("on", "It's switched on"),
        ("record", "Recording starts"), ("live_end", "Stopping live"))
ALSO = (("front", "Bring it to the front"), ("hide", "Hide it at the end"), ("mute", "Mute it at the end"),
        ("none", "Nothing"), ("pause", "Pause its video at the end"), ("play", "Play its video"),
        ("restart", "Play its video from the start"), ("back_layer", "Send it to the back"),
        ("show", "Show it first"), ("unmute", "Unmute it"))


def _easings():
    return tuple(sorted(((k, v) for k, v in EASING_NAMES.items() if k in MOVE_EASINGS), key=lambda kv: kv[1]))


def _timing() -> list[Field]:
    """How long, easing, what starts it, and stay or go back: the same on every kind of move."""
    return [
        Field("seconds", "How long", 1.0, 0.05, 60.0, 0.05, " s"),
        Field("easing", "Easing", "smooth", choices=_easings()),
        Field("when", "Starts when", "asked", choices=WHEN),
        Field("after", "After the move of", "", show_when=("when", ("after",))),
        Field("then", "Then", "stay", choices=(("back", "Go back"), ("stay", "Stay there"))),
        Field("hold", "Go back after", 2.0, 0.0, 600.0, 0.5, " s", show_when=("then", ("back",))),
    ]


register_filter("move", "Move", "video", [
    Field("relative", "From where it is", True),
    Field("x", "Across", 0.0, -8000.0, 8000.0, 1.0, " px"),
    Field("y", "Down", 0.0, -8000.0, 8000.0, 1.0, " px"),
    Field("size", "Size", 100, 1, 1000, 1, "%"),
    Field("turn", "Turn", 0.0, -3600.0, 3600.0, 1.0, "°"),
    Field("swap", "Trade places with", ""),
    *_timing(),
    Field("also", "Also", "none", choices=ALSO),
])(None)

register_filter("move_value", "Move value", "video", [
    Field("filter", "Filter", ""),
    Field("setting", "Setting", ""),
    Field("to", "To", 0.0, -100000.0, 100000.0, 0.1),
    Field("to_color", "To color", "#ffffff"),
    *_timing(),
])(None)

register_filter("move_sound", "Sound move", "video", [
    Field("sound_id", "Listen to", ""),
    Field("react", "Reacts by", "bounce", choices=(("bounce", "Bouncing"), ("grow", "Growing"),
                                                   ("wobble", "Wobbling"))),
    Field("amount", "How much", 12.0, 1.0, 400.0, 1.0),
    Field("loud", "Reacts above", -30.0, -60.0, 0.0, 1.0, " dB"),
])(None)

register_filter("move_camera", "Camera move", "video", [
    Field("use_pan", "Pan", False),
    Field("pan", "Pan to", 0.0, -100.0, 100.0, 1.0, "%", show_when=("use_pan", (True,))),
    Field("use_tilt", "Tilt", False),
    Field("tilt", "Tilt to", 0.0, -100.0, 100.0, 1.0, "%", show_when=("use_tilt", (True,))),
    Field("use_zoom", "Zoom", True),
    Field("zoom", "Zoom to", 50.0, 0.0, 100.0, 1.0, "%", show_when=("use_zoom", (True,))),
    *_timing(),
])(None)

KINDS = ("move", "move_value", "move_camera")  # the ones that play (a sound move reacts by itself)
_NAMES = {"move": "Move", "move_value": "Move value", "move_camera": "Camera move"}
_ENDS: dict[str, float] = {}  # a source's name -> when its last move ended
_PLAYING: dict[tuple[str, str], int] = {}  # (place id, kind) -> its newest play, so an old timer does nothing


def _filter_of(place: Place, kind: str):
    """The item's own filter of this kind, else its source's."""
    return find_filter(place, kind) or find_filter(place.source, kind)


def _settings(place: Place, kind: str = "move") -> dict:
    flt = _filter_of(place, kind)
    from sprigcast.core.filters import settings_for

    return settings_for(kind, dict(flt.settings) if flt else {})


def _new_play(place: Place, kind: str) -> int:
    key = (place.id, kind)
    _PLAYING[key] = _PLAYING.get(key, 0) + 1
    return _PLAYING[key]


def _still(place_id: str, kind: str, play_no: int) -> bool:
    return _PLAYING.get((place_id, kind)) == play_no


def _later(seconds: float, fn) -> None:
    from PySide6.QtCore import QTimer

    QTimer.singleShot(max(0, int(seconds * 1000)), fn)


def _ended(api, place: Place, kind: str, seconds: float, play_no: int) -> None:
    """When the trip is over: moves that start after this one do (Another move ends)."""
    def done():
        if not _still(place.id, kind, play_no):
            return
        name = place.source.name.strip().lower()
        now = time.monotonic()
        if now - _ENDS.get(name, -1.0) < 0.1:
            return  # its other shape (or another place) ended at the same moment: one start is enough
        _ENDS[name] = now
        _after_move(api, name)

    _later(seconds, done)


# Moves ----------------------------------------------------------------------------------------------------

def _corners(item, size):
    quad = item.quad(size) if size else None
    if not quad:
        return None
    cx = sum(p[0] for p in quad) / 4
    cy = sum(p[1] for p in quad) / 4
    width = math.dist(quad[0][:2], quad[1][:2])
    return cx, cy, width


def _spot(item) -> tuple:
    return item.x, item.y, item.scale, item.scale_y, item.rotation


def target_of(item, s: dict) -> tuple[float, float, float, float | None, float]:
    """Where a play sends the item: (x, y, scale, scale_y, rotation)."""
    grow = max(0.01, float(s["size"]) / 100)
    if s["relative"]:
        return (item.x + float(s["x"]), item.y + float(s["y"]), item.scale * grow,
                None if item.scale_y is None else item.scale_y * grow, item.rotation + float(s["turn"]))
    return float(s["x"]), float(s["y"]), grow, None, float(s["turn"])


def _travel(api, place: Place, target: tuple, s: dict, extra: dict | None = None, commit: bool = True) -> tuple:
    """Put the item at target now and have the engines draw the trip there. Returns where it was."""
    item = place.item
    before = _spot(item)
    size = (getattr(api.host, "source_sizes", dict)() or {}).get(item.source)
    start = _corners(item, size)
    item.x, item.y, item.scale, item.scale_y, item.rotation = target
    end = _corners(item, size)
    if start and end and end[2] > 0:
        offset = [start[0] - end[0], start[1] - end[1], start[2] / end[2], math.radians(before[4] - target[4])]
    else:  # its size isn't known yet: the turn and size still ease, the slide jumps
        ratio = (before[2] / target[2]) if target[2] else 1.0
        offset = [0.0, 0.0, ratio, math.radians(before[4] - target[4])]
    own = find_filter(place, "move")
    run = int((own.settings if own else {}).get("run", 0) or 0) + 1
    update = {**(extra if extra is not None else s), "from": offset, "run": run}
    filter_on(api, place, "move", "Move", update, float(s["seconds"]) + 0.2, commit=commit)
    return before


def _swap_partner(api, place: Place, name: str) -> Place | None:
    name = name.strip().lower()
    if not name:
        return None
    for item in place.scene.items:
        source = api.col.source(item.source)
        if source is not None and item is not place.item and source.name.lower() == name:
            return Place(place.scene, item, source)
    return None


def play(api, place: Place, back: bool = False) -> str:
    """Move one item the way its Move filter says (or back where it was). Returns its name."""
    s = _settings(place)
    play_no = _new_play(place, "move")
    partner = _swap_partner(api, place, str(s.get("swap", "")))
    if partner is not None:  # trade places: each goes where the other was (again to go back)
        here, there = _spot(place.item), _spot(partner.item)
        timing = {"seconds": s["seconds"], "easing": s["easing"], "swap_partner": True}
        _travel(api, partner, here, s, extra=timing, commit=False)
        _travel(api, place, there, s)
        moved_back = back
    else:
        own = find_filter(place, "move")
        saved = dict(own.settings) if own else {}
        if back:
            home = saved.get("home")
            if not home or len(home) != 5:
                return place.name  # never moved: already home
            target = tuple(home)
        else:
            target = target_of(place.item, s)
        update = dict(s)
        if not back and not saved.get("home"):
            update["home"] = list(_spot(place.item))  # where "go back" returns to, kept until it does
        if back:
            update["home"] = None
        _travel(api, place, target, s, extra=update)
        moved_back = back
    seconds = float(s["seconds"])
    if not moved_back:
        _also(api, place, s["also"], seconds, play_no)
    _ended(api, place, "move", seconds, play_no)
    if not back and s["then"] == "back":
        def home_again():
            if not _still(place.id, "move", play_no):
                return  # played again since: that play decides
            now = target_by_id(api.col, place.id)
            if now is not None:
                play(api, now, back=True)

        _later(seconds + float(s["hold"]), home_again)
    return place.name


def _also(api, place: Place, also: str, seconds: float, play_no: int) -> None:
    """The extra thing a move does: now (show, order, play) or when it lands (hide, mute, pause)."""
    item, scene = place.item, place.scene
    if also == "show" and not item.visible:
        item.visible = True
        api.host.commit("items")
    elif also in ("front", "back_layer") and item in scene.items:
        scene.items.remove(item)
        scene.items.insert(len(scene.items) if also == "front" else 0, item)  # index 0 draws first (bottom)
        api.host.commit("items")
    elif also in ("play", "restart", "unmute"):
        _quietly(api, *_ALSO_ACTIONS[also], place.source.id)
    elif also in ("hide", "mute", "pause"):
        def at_the_end():
            if not _still(place.id, "move", play_no):
                return
            now = target_by_id(api.col, place.id)
            if now is None:
                return
            if also == "hide":
                now.item.visible = False
                api.host.commit("items")
            else:
                _quietly(api, *_ALSO_ACTIONS[also], now.source.id)

        _later(seconds, at_the_end)


_ALSO_ACTIONS = {"play": ("media.play", "media", {}), "restart": ("media.restart", "media", {}),
                 "pause": ("media.pause", "media", {}), "mute": ("sound.mute", "sound", {"muted": True}),
                 "unmute": ("sound.mute", "sound", {"muted": False})}


def _quietly(api, action: str, key: str, extra: dict, source_id: str) -> None:
    try:
        api.run(action, {key: source_id, **extra})
    except ApiError:
        pass  # not a video, or no sound: nothing to do


# Value moves ----------------------------------------------------------------------------------------------

_SLIDING: dict[tuple, int] = {}  # (source id, filter id or "", setting) -> its newest slide
STEP_S = 0.05  # 20 looks a second: smooth for a number, and light (each look sends only what changed)


def _value_home(api, place: Place, s: dict):
    """Where the setting lives: (holder dict owner, its settings, filter or None)."""
    name = str(s["filter"]).strip().lower()
    if not name:
        return place.source, None
    for flt in list(place.item.effects) + list(place.source.filters):
        if flt.name.lower() == name or flt.kind == name:
            return place.source, flt
    raise ApiError("not_found", f"{place.source.name} has no filter called {s['filter']}.")


def _color(text) -> tuple[int, int, int] | None:
    text = str(text).strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _between(start, end, e: float):
    if isinstance(start, tuple):
        return "#" + "".join(f"{max(0, min(255, round(a + (b - a) * e))):02x}" for a, b in zip(start, end))
    return start + (end - start) * e


def play_value(api, place: Place, back: bool = False) -> str:
    """Slide one setting of the thing (or of one of its filters) the way its Move value filter says."""
    s = _settings(place, "move_value")
    key = str(s["setting"]).strip()
    if not key:
        raise ApiError("bad_request", "Give the Move value filter a setting to slide.")
    source, flt = _value_home(api, place, s)
    if flt is None:
        values = source.settings
    else:
        from sprigcast.core.filters import settings_for

        values = {**settings_for(flt.kind, flt.settings), **flt.settings}
    if key not in values:
        raise ApiError("not_found", f"There's no setting {key}.")
    now = values[key]
    own = _filter_of(place, "move_value")
    homes = own.settings.setdefault("homes", {}) if own is not None else {}
    if isinstance(now, bool):
        raise ApiError("not_allowed", f"{key} is on or off: there's nothing in between to slide.")
    start = _color(now) if isinstance(now, str) else (float(now) if isinstance(now, (int, float)) else None)
    if start is None:
        raise ApiError("not_allowed", f"{key} isn't a number or a color.")
    if back:
        if key not in homes:
            return place.name
        end = _color(homes.pop(key)) if isinstance(start, tuple) else float(homes.pop(key))
    else:
        end = _color(s["to_color"]) if isinstance(start, tuple) else float(s["to"])
        homes.setdefault(key, now)
    if end is None:
        raise ApiError("bad_request", "To color must look like #ff8800.")
    whole = isinstance(now, int) and not isinstance(now, bool)
    slide = (source.id, flt.id if flt else "", key)
    _SLIDING[slide] = number = _SLIDING.get(slide, 0) + 1
    seconds, easing = float(s["seconds"]), MOVE_EASINGS.index(s["easing"]) if s["easing"] in MOVE_EASINGS else 0
    began = time.monotonic()
    play_no = _new_play(place, "move_value")

    def put(value, last: bool):
        here = target_by_id(api.col, source.id)
        if here is None:
            return
        value = round(value) if whole and not isinstance(value, str) else value
        if flt is None:
            here.settings = {**here.settings, key: value}
            api.host.commit_settings([here]) if hasattr(api.host, "commit_settings") else api.host.commit("sources")
            return
        target = next((f for f in list(place.item.effects) + list(here.filters) if f.id == flt.id), None)
        if target is None:
            return
        target.settings = {**target.settings, key: value}
        quiet = getattr(api.host, "commit_quiet", None)
        if last or quiet is None:
            api.host.commit("filters")
        else:
            quiet()

    def step():
        if _SLIDING.get(slide) != number:
            return  # slid again since
        t = min(1.0, (time.monotonic() - began) / seconds)
        put(_between(start, end, ease(t, easing)), t >= 1.0)
        if t < 1.0:
            _later(STEP_S, step)

    step()
    _ended(api, place, "move_value", seconds, play_no)
    if not back and s["then"] == "back":
        def home_again():
            if _still(place.id, "move_value", play_no):
                now_place = target_by_id(api.col, place.id)
                if now_place is not None:
                    play_value(api, now_place, back=True)

        _later(seconds + float(s["hold"]), home_again)
    return place.name


# Camera moves ---------------------------------------------------------------------------------------------

def _open_camera(name: str, path: str, index):
    from sprigcast.sources.camera_control import open_camera

    return open_camera(name, path, index)


_CAMERA_HOMES: dict[str, dict[int, int]] = {}  # place id -> where pan / tilt / zoom were before it moved
_CAMERA_RUNS: dict[str, int] = {}


def _camera_device(source):
    from sprigcast.sources.camera import find_camera

    name, path = str(source.settings.get("name", "")), str(source.settings.get("path", ""))
    try:
        found = find_camera(name, path)
    except Exception:  # noqa: BLE001 listing cameras can fail on a driver hiccup
        found = None
    return name, path, (found.index if found is not None else None)


def camera_targets(s: dict) -> dict[int, tuple[float, float]]:
    """{property: (share of its range 0..1)} for the controls this camera move turns."""
    from sprigcast.sources.camera_control import PAN, TILT, ZOOM

    out = {}
    if s["use_pan"]:
        out[PAN] = (float(s["pan"]) + 100) / 200
    if s["use_tilt"]:
        out[TILT] = (float(s["tilt"]) + 100) / 200
    if s["use_zoom"]:
        out[ZOOM] = float(s["zoom"]) / 100
    return out


def play_camera(api, place: Place, back: bool = False) -> str:
    """Turn the webcam's own pan, tilt and zoom the way its Camera move filter says (on a worker: a camera
    can take a moment to answer)."""
    if place.source.kind != "camera":
        raise ApiError("not_allowed", f"{place.source.name} isn't a camera.")
    s = _settings(place, "move_camera")
    wants = camera_targets(s)
    if back:
        wants = None  # where it was
    elif not wants:
        raise ApiError("bad_request", "Switch on Pan, Tilt or Zoom in the Camera move filter.")
    name, path, index = _camera_device(place.source)
    seconds, easing = float(s["seconds"]), MOVE_EASINGS.index(s["easing"]) if s["easing"] in MOVE_EASINGS else 0
    hold = float(s["hold"]) if s["then"] == "back" and not back else None
    _CAMERA_RUNS[place.id] = run = _CAMERA_RUNS.get(place.id, 0) + 1
    play_no = _new_play(place, "move_camera")
    threading.Thread(target=_camera_trip, args=(place.id, name, path, index, wants, seconds, easing, hold, run),
                     name="camera-move", daemon=True).start()
    _ended(api, place, "move_camera", seconds, play_no)
    return place.name


def _camera_trip(place_id, name, path, index, wants, seconds, easing, hold, run) -> None:
    camera = _open_camera(name, path, index)
    if camera is None:
        import logging

        logging.getLogger(__name__).info("camera move: %s has no controls Windows can turn", name or "the camera")
        return
    try:
        if wants is None:
            ends = dict(_CAMERA_HOMES.pop(place_id, {}))
        else:
            ends = {}
            for prop, share in wants.items():
                reach = camera.range(prop)
                if reach is not None:
                    lo, hi, step, _ = reach
                    ends[prop] = lo + round(share * (hi - lo) / step) * step
        starts = {p: camera.get(p) for p in ends}
        starts = {p: v for p, v in starts.items() if v is not None}
        if wants is not None:
            homes = _CAMERA_HOMES.setdefault(place_id, {})
            for p, v in starts.items():
                homes.setdefault(p, v)
        _turn(camera, starts, ends, seconds, easing, lambda: _CAMERA_RUNS.get(place_id) == run)
        if hold is not None:
            time.sleep(hold)
            if _CAMERA_RUNS.get(place_id) == run:
                _turn(camera, ends, dict(_CAMERA_HOMES.pop(place_id, {})), seconds, easing,
                      lambda: _CAMERA_RUNS.get(place_id) == run)
    finally:
        camera.close()


def _turn(camera, starts: dict, ends: dict, seconds: float, easing: int, current) -> None:
    """Ease each control from where it is to where it goes, about 15 times a second."""
    began = time.monotonic()
    last: dict[int, int] = {}
    while current():
        t = min(1.0, (time.monotonic() - began) / max(0.05, seconds))
        e = ease(t, easing)
        for prop, end in ends.items():
            start = starts.get(prop, end)
            value = round(start + (end - start) * e)
            if last.get(prop) != value:
                camera.set(prop, value)
                last[prop] = value
        if t >= 1.0:
            return
        time.sleep(1 / 15)


# Sound moves ----------------------------------------------------------------------------------------------

PULSE_S = 0.25  # one bounce; a long loud bit bounces again every PULSE_S
_PULSED: dict[tuple[str, str], float] = {}  # (holder id, filter id) -> when it last bounced


def _sound_moves(col) -> list[tuple[Place, object]]:
    out = []
    for scene in col.scenes:
        for item in scene.items:
            source = col.source(item.source)
            if source is None:
                continue
            for flt in list(item.effects) + list(source.filters):
                if flt.kind == "move_sound" and flt.enabled:
                    out.append((Place(scene, item, source), flt))
    return out


def pulse_of(s: dict) -> list[float]:
    """Where a bounce starts from (the engines ease it back to rest): [dx, dy, size, turn]."""
    amount = float(s["amount"])
    if s["react"] == "grow":
        return [0.0, 0.0, 1 + amount / 100, 0.0]
    if s["react"] == "wobble":
        return [0.0, 0.0, 1.0, math.radians(amount)]
    return [0.0, -amount, 1.0, 0.0]


def _hear(api, levels: dict, now: float) -> None:
    from sprigcast.core.filters import settings_for

    moved = False
    seen = set()
    for place, flt in _WATCH.get("sound", []):
        s = settings_for("move_sound", flt.settings)
        level = levels.get(str(s["sound_id"]))
        key = (place.id if flt in place.item.effects else place.source.id, flt.id)
        if level is None or key in seen:
            continue
        seen.add(key)
        if level[1] < float(s["loud"]) or now - _PULSED.get(key, 0.0) < PULSE_S:
            continue
        _PULSED[key] = now
        flt.settings = {**flt.settings, "from": pulse_of(s), "run": int(flt.settings.get("run", 0) or 0) + 1,
                        "seconds": PULSE_S, "easing": "ease_out"}
        moved = True
        _later(PULSE_S * 2, lambda f=flt, k=key, at=now: _settle(api, f, k, at))
    if moved:
        _send_quietly(api)


def _settle(api, flt, key, at: float) -> None:
    """Quiet again: nothing left to draw, so a still picture stays still (and costs nothing)."""
    if _PULSED.get(key) == at and flt.settings.get("from"):
        flt.settings = {**flt.settings, "from": None}
        _send_quietly(api)


def _send_quietly(api) -> None:
    quiet = getattr(api.host, "commit_quiet", None)
    (quiet or (lambda: api.host.commit("filters")))()


# What starts a move -------------------------------------------------------------------------------------

_WATCH: dict = {"tick": 0}
LOOK_EVERY = 3  # levels come ~15 times a second: what's on air is looked at ~5 times a second


def _plays(kind: str):
    return {"move": play, "move_value": play_value, "move_camera": play_camera}[kind]


def _start(api, place: Place, kind: str, back: bool = False) -> None:
    try:
        _plays(kind)(api, place, back=back)
    except ApiError:
        pass  # a value move with no setting yet, a camera that can't: nothing to do


def _triggered(col) -> list[tuple[Place, str, dict]]:
    """Every item with a move that starts by itself: (place, kind, settings)."""
    from sprigcast.core.filters import settings_for

    out = []
    for scene in col.scenes:
        for item in scene.items:
            source = col.source(item.source)
            if source is None:
                continue
            for kind in KINDS:
                flt = find_filter(item_place := Place(scene, item, source), kind) or find_filter(source, kind)
                if flt is None or flt.settings.get("swap_partner"):
                    continue
                s = settings_for(kind, flt.settings)
                if s.get("when", "asked") != "asked":
                    out.append((item_place, kind, s))
    return out


def _after_move(api, name: str) -> None:
    from sprigcast.plugin_hooks import _walk_on_air

    name = name.strip().lower()
    air = _walk_on_air(api.col)[1]
    for place, kind, s in _triggered(api.col):
        if s["when"] == "after" and str(s["after"]).strip().lower() == name and place.scene.id in air:
            if place.source.name.lower() != name:  # never itself, round and round
                _start(api, place, kind)


def _media_state(api, source_id: str):
    try:
        return api._media_state(source_id)
    except Exception:  # noqa: BLE001 not a video
        return None


def _watch(api, levels: dict) -> None:
    """~15 times a second with the sound levels: sound moves react; what's on air is looked at now and then."""
    now = time.monotonic()
    _WATCH["tick"] += 1
    look = _WATCH["tick"] % LOOK_EVERY == 0
    if look or "sound" not in _WATCH:
        _WATCH["sound"] = _sound_moves(api.col)
    if _WATCH["sound"]:
        _hear(api, levels, now)
    if not look:
        return
    from sprigcast.plugin_hooks import _walk_on_air

    col = api.col
    moves = _triggered(col)
    outputs = {"stream": output_since("stream") is not None, "record": output_since("record") is not None}
    first = "air" not in _WATCH
    if not moves:
        _WATCH.update(air={}, outputs=outputs, media={})
        return
    air_now = _walk_on_air(col)[1]
    before_air = _WATCH.get("air", {})
    before_out = _WATCH.get("outputs", outputs)
    before_media = _WATCH.get("media", {})
    air, media = {}, {}
    for place, kind, s in moves:
        showing = place.scene.id in air_now
        on = showing and place.item.visible
        air[place.id] = (showing, on)
        when = s["when"]
        if when in ("media_start", "media_end"):
            media[place.id] = _media_state(api, place.source.id)
        if first or place.id not in before_air:
            continue
        was_showing, was_on = before_air[place.id]
        fire = ((when == "shown" and showing and not was_showing) or
                (when == "hidden" and was_showing and not showing) or
                (when == "on" and on and not was_on) or (when == "off" and was_on and not on) or
                (when == "live" and outputs["stream"] and not before_out["stream"]) or
                (when == "live_end" and before_out["stream"] and not outputs["stream"]) or
                (when == "record" and outputs["record"] and not before_out["record"]) or
                (when == "media_start" and showing and media.get(place.id) == "playing"
                 and before_media.get(place.id) != "playing") or
                (when == "media_end" and showing and media.get(place.id) == "ended"
                 and before_media.get(place.id) != "ended"))
        if fire:
            _start(api, place, kind)
        elif kind == "move" and s["then"] == "stay" and when in ("shown", "on") and was_on and not on:
            _home_quietly(api, place)  # gone from view: ready to come in again the next time
    _WATCH.update(air=air, outputs=outputs, media=media)


def _home_quietly(api, place: Place) -> None:
    own = find_filter(place, "move")
    home = (own.settings if own else {}).get("home")
    if home and len(home) == 5:
        place.item.x, place.item.y, place.item.scale, place.item.scale_y, place.item.rotation = home
        own.settings = {**own.settings, "home": None, "from": None}
        api.host.commit("items")


register_level_watcher(_watch)


# Control API ----------------------------------------------------------------------------------------------

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


def _play_action(kind: str):
    def run(api, data):
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
        if kind != "move":  # one setting or one camera, however many places show it
            places = places[:1]
        return {"moving": [_plays(kind)(api, place, back=back) for place in places]}

    return run


_ARGS = {"source": "name or id", "in": "?a bloom or sprig: only the one in there",
         "back": "?true: back to where it was before it moved"}
register_action("move.play", "Move something in a bloom the way its Move filter says (or back where it was).",
                _ARGS, _play_action("move"))
register_action("move.value", "Slide a setting the way the thing's Move value filter says (or back).",
                _ARGS, _play_action("move_value"))
register_action("move.camera", "Turn a webcam's pan, tilt and zoom the way its Camera move filter says (or back).",
                _ARGS, _play_action("move_camera"))


# Hotkeys --------------------------------------------------------------------------------------------------

_KEY_NAMES = {"move": "move", "move_value": "move-value", "move_camera": "move-camera"}


def _hotkeys(collection):
    """A key, and a key back, for each item with a move (its own, or its source's)."""
    out = []
    sources = {s.id: s for s in collection.sources}
    for scene in collection.scenes:
        for item in scene.items:
            source = sources.get(item.source)
            if source is None:
                continue
            place = Place(scene, item, source)
            for kind in KINDS:
                flt = find_filter(place, kind) or find_filter(source, kind)
                if flt is None or flt.settings.get("swap_partner"):
                    continue
                name = _NAMES[kind]
                out.append((f"{_KEY_NAMES[kind]}:{place.id}", f"{name} {place.name}"))
                out.append((f"{_KEY_NAMES[kind]}-back:{place.id}", f"{name} back {place.name}"))
    return out


def _run_hotkey(api, hid):
    head, ref = hid.split(":", 1)
    back = head.endswith("-back")
    kind = {v: k for k, v in _KEY_NAMES.items()}.get(head.removesuffix("-back"), "move")
    place = target_by_id(api.col, ref)
    if not isinstance(place, Place):
        return "That's gone."
    if not place_on_air(api.col, place):
        return not_here(place.name)
    try:
        _plays(kind)(api, place, back=back)
    except ApiError as exc:
        return str(exc)
    return None


register_hotkeys(_hotkeys, _run_hotkey)
