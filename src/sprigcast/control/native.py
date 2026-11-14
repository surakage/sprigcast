"""The SprigCast API: SprigCast's own control language, in its own words.

Connect a WebSocket to ws://127.0.0.1:<port>/sprigcast (the port in Settings → Connections).

    server → {"hello": {"app": "SprigCast", "version": "0.1.0", "api": 1, "auth": {"salt": "…", "challenge": "…"}}}
    app    → {"auth": "<answer>", "events": ["bloom", "sound"]}       # auth only when a password is set
    server → {"welcome": {"api": 1, "events": ["bloom", "sound"]}}
    app    → {"id": "7", "do": "bloom.show", "bloom": "BRB", "shape": "both"}
    server → {"id": "7", "ok": true, "result": {...}}
             {"id": "7", "ok": false, "error": {"code": "not_found", "message": "No bloom called 'BRB'."}}
    server → {"event": "bloom.shown", "bloom": "BRB", "shape": "horizontal", ...}

The answer to the password challenge is the same as obs-websocket's, so client libraries can reuse it:
base64(sha256(base64(sha256(password + salt)) + challenge)).

Words: a bloom is a show you put on air; a sprig is a reusable part placed inside blooms (with
optional Live states); an item is one thing inside a bloom or sprig; a sound is anything in the mixer;
a place is somewhere you stream to; a shape is a canvas (horizontal, vertical). Things are named by
name or id. Every action that changes a bloom takes shape: "both" (default) or one shape.

"app.actions" lists every action with its fields, so plugins can build menus without hard-coding.
The OBS-compatible door (control/api.py) is frozen: new features land here.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from sprigcast import __version__
from sprigcast.control.protocol import Close, auth_string
from sprigcast.core.scene import PART_KIND, TRANSITION_KINDS, Filter, Scene, SceneItem, SourceConfig

API_VERSION = 1
EVENT_GROUPS = ("bloom", "item", "sprig", "sound", "filter", "source", "output", "studio", "transition", "custom")
HIGH_VOLUME = ("meters",)  # only when asked for by name: about 15 a second
SOUND_KINDS = ("audio_input", "audio_output")


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _bad(message: str) -> ApiError:
    return ApiError("bad_request", message)


def _missing(what: str) -> ApiError:
    return ApiError("not_found", what)


@dataclass
class Action:
    name: str
    fields: dict[str, str]  # field -> short description ("?" first = optional)
    about: str
    run: Callable[[dict[str, Any]], Any]


def action(name: str, about: str, /, **fields: str):
    """Mark a NativeApi method as an action (fields: name -> description; start with ? when optional)."""

    def wrap(fn):
        fn._action = (name, about, fields)
        return fn

    return wrap


def _get(data: dict[str, Any], key: str, kind: type | tuple[type, ...], optional: bool = False, default: Any = None):
    """data[key] checked against kind (float accepts whole numbers; bools are never numbers)."""
    if data.get(key) is None:
        if optional:
            return default
        raise _bad(f"{key} is missing.")
    value = data[key]
    if kind in (float, (int, float), int):
        if isinstance(value, bool) or not isinstance(value, int if kind is int else (int, float)):
            raise _bad(f"{key} must be a number.")
        return value if kind is int else float(value)
    if not isinstance(value, kind):
        raise _bad(f"{key} has the wrong type.")
    return value


_PLUGIN_ACTIONS: dict[str, Action] = {}


def register_action(name: str, about: str, fields: dict[str, str], run: Callable[[Any, dict[str, Any]], Any]) -> None:
    """For plugins: add an action to the SprigCast API. Name it "<plugin>.<thing>".

    run(api, data) gets the NativeApi (api.col is the scene collection, api.host.commit("items")
    saves and redraws after a change) and the fields the app sent; raise ApiError to fail.
    """
    if "." not in name:
        raise ValueError("name plugin actions <plugin>.<thing>")
    _PLUGIN_ACTIONS[name] = Action(name, dict(fields), about, run)


class NativeApi:
    def __init__(self, host):
        self.host = host
        self.actions: dict[str, Action] = {}
        for attr in dir(self):
            fn = getattr(self, attr)
            meta = getattr(fn, "_action", None)
            if meta:
                name, about, fields = meta
                self.actions[name] = Action(name, fields, about, fn)
        for name, extra in _PLUGIN_ACTIONS.items():
            if name not in self.actions:
                self.actions[name] = Action(name, extra.fields, extra.about,
                                            lambda data, run=extra.run: run(self, data))
        self._last: dict[str, Any] | None = None
        self.custom_event: Callable[[dict[str, Any]], None] = lambda data: None
        self.last_clip: list[str] = []

    @property
    def col(self):
        return self.host.collection

    def run(self, name: str, data: dict[str, Any]) -> Any:
        found = self.actions.get(name)
        if found is None:
            raise ApiError("unknown_action", f"No action {name!r}. app.actions lists them all.")
        return found.run(data)

    # Lookups ---------------------------------------------------------------------------

    def _shapes(self) -> list[str]:
        return [c.id for c in self.col.canvases]

    def _shape(self, data: dict[str, Any]) -> str:
        shape = _get(data, "shape", str, optional=True, default="both")
        if shape != "both" and shape not in self._shapes():
            raise _bad(f"shape must be both or one of {', '.join(self._shapes())}.")
        return shape

    def _bloom(self, data: dict[str, Any], key: str = "bloom") -> Scene:
        """A bloom by id or name (any shape's name); returns the main one (the first canvas's when linked)."""
        wanted = _get(data, key, str)
        shows = [s for s in self.col.scenes if s.kind == "show"]
        scene = next((s for s in shows if s.id == wanted), None) or next((s for s in shows if s.name == wanted), None)
        if scene is None:
            if any(s.kind == "part" and (s.id == wanted or s.name == wanted) for s in self.col.scenes):
                raise ApiError("not_allowed", f"{wanted!r} is a sprig: sprigs show inside blooms.")
            raise _missing(f"No bloom called {wanted!r}.")
        return self._main_shape(scene)

    def _main_shape(self, scene: Scene) -> Scene:
        family = self.col.linked_with(scene.id)
        order = {cid: i for i, cid in enumerate(self._shapes())}
        return min(family, key=lambda s: order.get(s.canvas, 99))

    def _in_shape(self, bloom: Scene, shape: str) -> list[Scene]:
        family = self.col.linked_with(bloom.id)
        if shape == "both":
            return family
        found = [s for s in family if s.canvas == shape]
        if not found:
            raise _missing(f"{bloom.name!r} has no {shape} shape.")
        return found

    def _sprig(self, data: dict[str, Any], key: str = "sprig") -> Scene:
        wanted = _get(data, key, str)
        parts = self.col.parts()
        part = next((p for p in parts if p.id == wanted), None) or next((p for p in parts if p.name == wanted), None)
        if part is None:
            raise _missing(f"No sprig called {wanted!r}.")
        return part

    def _holder(self, data: dict[str, Any]) -> tuple[Scene, list[Scene]]:
        """The bloom or sprig named by "bloom" or "sprig", and its layouts in the asked shape(s)."""
        shape = self._shape(data)
        if data.get("sprig") is not None:
            part = self._sprig(data)
            shapes = self.col.shapes(part)
            if shape != "both":
                shapes = [self.col.shape_for(part, shape)]
            return part, shapes
        bloom = self._bloom(data)
        return bloom, self._in_shape(bloom, shape)

    def _source(self, data: dict[str, Any], key: str) -> SourceConfig:
        wanted = _get(data, key, str)
        sources = [s for s in self.col.sources if s.kind != PART_KIND]
        source = next((s for s in sources if s.id == wanted), None) or next((s for s in sources if s.name == wanted), None)
        if source is None:
            raise _missing(f"No source called {wanted!r}.")
        return source

    @staticmethod
    def _has_sound(source: SourceConfig) -> bool:
        if source.kind in SOUND_KINDS:
            return True
        if source.kind == "browser":
            from sprigcast.sources.browser import sound_to_mixer

            return sound_to_mixer(source.settings)
        return False

    def _sound(self, data: dict[str, Any]) -> SourceConfig:
        source = self._source(data, "sound")
        if not self._has_sound(source):
            raise ApiError("not_allowed", f"{source.name!r} has no sound.")
        return source

    def _item_name(self, source_id: str) -> str:
        source = next((s for s in self.col.sources if s.id == source_id), None)
        if source is None:
            return source_id
        part = self.col.part_of(source)
        return part.name if part is not None else source.name

    def _item_key(self, source_id: str) -> str:
        """An item's id: its source's id, or for a placed sprig the sprig's id (the same in every shape)."""
        source = next((s for s in self.col.sources if s.id == source_id), None)
        part = self.col.part_of(source) if source is not None else None
        return part.id if part is not None else source_id

    def _matching_items(self, layouts: list[Scene], wanted: str) -> list[SceneItem]:
        out = []
        for layout in layouts:
            for item in layout.items:
                if wanted in (self._item_key(item.source), item.source, self._item_name(item.source)):
                    out.append(item)
        return out

    def _filter(self, source: SourceConfig, data: dict[str, Any]) -> Filter:
        wanted = _get(data, "filter", str)
        found = next((f for f in source.filters if f.id == wanted), None) \
            or next((f for f in source.filters if f.name == wanted), None) \
            or next((f for f in source.filters if f.kind == wanted), None)
        if found is None:
            raise _missing(f"{source.name!r} has no filter {wanted!r}.")
        return found

    # Describing -------------------------------------------------------------------------

    def _bloom_info(self, bloom: Scene) -> dict[str, Any]:
        family = self.col.linked_with(bloom.id)
        on_air = [s.canvas for s in family if self.col.active_scenes.get(s.canvas) == s.id]
        folder = next((f for f in self.col.folders if f.id == bloom.folder), None)
        return {"id": bloom.id, "name": bloom.name, "shapes": {s.canvas: s.id for s in family},
                "onAir": on_air, "folder": folder.name if folder else None, "live": bool(folder and folder.live)}

    def _blooms(self) -> list[Scene]:
        seen: set[str] = set()
        out = []
        for canvas_id in self._shapes():
            for scene in self.col.scenes_for(canvas_id):
                if scene.id in seen:
                    continue
                main = self._main_shape(scene)
                seen.update(s.id for s in self.col.linked_with(scene.id))
                out.append(main)
        return out

    def _sound_info(self, source: SourceConfig) -> dict[str, Any]:
        st = source.settings
        return {"id": source.id, "name": source.name, "kind": source.kind, "muted": bool(st.get("muted", False)),
                "volumeDb": float(st.get("volume_db", 0.0)), "balance": float(st.get("balance", 0.0) or 0.0)}

    def _filter_info(self, f: Filter) -> dict[str, Any]:
        from sprigcast.core.filters import settings_for

        return {"id": f.id, "name": f.name, "kind": f.kind, "on": f.enabled, "settings": settings_for(f.kind, f.settings)}

    # App --------------------------------------------------------------------------------

    @action("app.info", "Version, API version and the shapes this setup has.")
    def app_info(self, data):
        return {"app": "SprigCast", "version": __version__, "api": API_VERSION, "shapes": self._shapes(),
                "events": list(EVENT_GROUPS + HIGH_VOLUME)}

    @action("app.actions", "Every action with what it does and its fields (fields starting with ? are optional).")
    def app_actions(self, data):
        return {"actions": [{"do": a.name, "about": a.about, "fields": a.fields}
                            for a in sorted(self.actions.values(), key=lambda a: a.name)]}

    @action("app.stats", "Frames per second, render time and memory.")
    def app_stats(self, data):
        return self.host.stats()

    @action("app.broadcast", "Send your own event to every connected app.", data="any object")
    def app_broadcast(self, data):
        self.custom_event(_get(data, "data", dict))

    @action("hotkey.run", "Do what a hotkey does (hotkey.list shows them).", hotkey="hotkey action id")
    def hotkey_run(self, data):
        wanted = _get(data, "hotkey", str)
        if wanted not in {a for a, _ in self.host.hotkeys()}:
            raise _missing(f"No hotkey action {wanted!r}.")
        self.host.trigger_hotkey(wanted)

    @action("hotkey.list", "Hotkey actions and what they do.")
    def hotkey_list(self, data):
        return {"hotkeys": [{"id": a, "about": label} for a, label in self.host.hotkeys()]}

    # Blooms -----------------------------------------------------------------------------

    @action("blooms.list", "Every bloom, its shapes and which shapes it's on air in.")
    def blooms_list(self, data):
        return {"blooms": [self._bloom_info(b) for b in self._blooms()],
                "onAir": {cid: sid for cid, sid in self.col.active_scenes.items()}}

    @action("bloom.show", "Put a bloom on air.", bloom="name or id", shape="?both (default), horizontal or vertical")
    def bloom_show(self, data):
        bloom = self._bloom(data)
        for scene in self._in_shape(bloom, self._shape(data)):
            self.col.active_scenes[scene.canvas] = scene.id
        self.host.commit("program")
        return self._bloom_info(bloom)

    @action("bloom.current", "The bloom on air in each shape.")
    def bloom_current(self, data):
        out = {}
        for cid in self._shapes():
            scene = self.col.active_scene(cid)
            out[cid] = None if scene is None else {"id": scene.id, "name": scene.name}
        return {"onAir": out}

    @action("studio.set", "Studio mode on or off (blooms line up in Next before going on air).", on="true or false")
    def studio_set(self, data):
        self.host.set_studio(_get(data, "on", bool))

    @action("bloom.lineUp", "Studio mode: line a bloom up in Next (turns Studio mode on).", bloom="name or id")
    def bloom_line_up(self, data):
        bloom = self._bloom(data)
        if not self.host.studio():
            self.host.set_studio(True)
        self.host.line_up(bloom.id)

    @action("bloom.go", "Studio mode: send what's lined up in Next on air (Bloom it).")
    def bloom_go(self, data):
        if not self.host.staged():
            raise ApiError("not_allowed", "Nothing is lined up in Next.")
        self.host.bloom_it()

    # Items and sprigs -------------------------------------------------------------------------

    @action("items.list", "What's inside a bloom or sprig, per shape.", bloom="?name or id", sprig="?name or id")
    def items_list(self, data):
        _, layouts = self._holder({**data, "shape": "both"})
        return {"shapes": {layout.canvas: [
            {"id": self._item_key(i.source), "name": self._item_name(i.source), "visible": i.visible,
             "locked": i.locked, "sprig": self._item_key(i.source) != i.source}
            for i in reversed(layout.items)] for layout in layouts}}  # top of the list first, as in Build

    def _set_items(self, data, visible: bool | None):
        holder, layouts = self._holder(data)
        wanted = _get(data, "item", str)
        items = self._matching_items(layouts, wanted)
        if not items:
            raise _missing(f"{holder.name!r} has no {wanted!r}.")
        on = (not items[0].visible) if visible is None else visible
        for item in items:
            item.visible = on
        self.host.commit("items")
        return {"visible": on}

    @action("item.show", "Show something inside a bloom or sprig.", item="name or id", bloom="?bloom it's in",
            sprig="?sprig it's in", shape="?both (default), horizontal or vertical")
    def item_show(self, data):
        return self._set_items(data, True)

    @action("item.hide", "Hide something inside a bloom or sprig.", item="name or id", bloom="?bloom it's in",
            sprig="?sprig it's in", shape="?both (default), horizontal or vertical")
    def item_hide(self, data):
        return self._set_items(data, False)

    @action("item.toggle", "Show it if hidden, hide it if shown.", item="name or id", bloom="?bloom it's in",
            sprig="?sprig it's in", shape="?both (default), horizontal or vertical")
    def item_toggle(self, data):
        return self._set_items(data, None)

    @action("sprigs.list", "Every sprig, its Live states and the state showing.")
    def sprigs_list(self, data):
        return {"sprigs": [{"id": p.id, "name": p.name, "state": p.state, "states": [s.name for s in p.states],
                            "shapes": [s.canvas for s in self.col.shapes(p)]} for p in self.col.parts()]}

    @action("sprig.state", "Switch a sprig to one of its Live states (null: as built).", sprig="name or id",
            state="state name or null")
    def sprig_state(self, data):
        part = self._sprig(data)
        name = data.get("state") or None
        if name is not None and all(s.name != name for s in part.states):
            raise _missing(f"{part.name!r} has no state {name!r}.")
        self.col.set_state(part.id, name)
        self.host.commit("states")

    # Sounds ------------------------------------------------------------------------------------

    @action("sounds.list", "Every sound in the mixer with its volume and mute.")
    def sounds_list(self, data):
        return {"sounds": [self._sound_info(s) for s in self.col.sources if self._has_sound(s)]}

    def _set_sound(self, source: SourceConfig, changes: dict[str, Any]):
        source.settings = {**source.settings, **changes}
        self.host.commit("sound")
        return self._sound_info(source)

    @action("sound.mute", "Mute or unmute a sound (no muted: toggle).", sound="name or id", muted="?true or false")
    def sound_mute(self, data):
        source = self._sound(data)
        muted = _get(data, "muted", bool, optional=True)
        return self._set_sound(source, {"muted": not source.settings.get("muted", False) if muted is None else muted})

    @action("sound.volume", "Set a sound's volume in dB, or change it by some dB.", sound="name or id",
            db="?new volume, -100 to 26", change="?dB to add (negative to lower)")
    def sound_volume(self, data):
        source = self._sound(data)
        now = float(source.settings.get("volume_db", 0.0))
        if data.get("db") is not None:
            db = _get(data, "db", float)
        elif data.get("change") is not None:
            db = now + _get(data, "change", float)
        else:
            raise _bad("db or change is missing.")
        return self._set_sound(source, {"volume_db": round(max(-100.0, min(26.0, db)), 2)})

    @action("sound.balance", "Left/right: -1 left, 0 middle, 1 right.", sound="name or id", balance="-1 to 1")
    def sound_balance(self, data):
        source = self._sound(data)
        balance = _get(data, "balance", float)
        if not -1 <= balance <= 1:
            raise _bad("balance must be -1 to 1.")
        return self._set_sound(source, {"balance": balance})

    @action("sound.micFirst", "Lower everything else while your mic talks (ducking).", on="true or false")
    def sound_mic_first(self, data):
        self.col.audio.ducking = _get(data, "on", bool)
        self.host.commit("sound")

    # Filters -------------------------------------------------------------------------------------

    @action("filters.kinds", "Filter kinds you can add, with their settings and limits.")
    def filters_kinds(self, data):
        from sprigcast.core.filters import filter_kinds

        return {"kinds": [{"kind": k.kind, "label": k.label, "media": k.media,
                           "settings": [{"key": f.key, "label": f.label, "default": f.default, "type": f.kind,
                                         **({"min": f.minimum, "max": f.maximum} if f.kind in ("int", "float") else {}),
                                         **({"choices": [c for c, _ in f.choices]} if f.choices else {})}
                                        for f in k.fields]} for k in filter_kinds()]}

    @action("filters.list", "A source's filters, top to bottom.", source="name or id")
    def filters_list(self, data):
        source = self._source(data, "source")
        return {"filters": [self._filter_info(f) for f in source.filters]}

    @action("filter.set", "Turn a filter on or off and/or change some of its settings (others stay).",
            source="name or id", filter="filter name, id or kind", on="?true or false", settings="?{key: value}")
    def filter_set(self, data):
        source = self._source(data, "source")
        found = self._filter(source, data)
        on = _get(data, "on", bool, optional=True)
        settings = _get(data, "settings", dict, optional=True)
        if on is None and settings is None:
            raise _bad("Give on, settings or both.")
        if on is not None:
            found.enabled = on
        if settings:
            from sprigcast.core.filters import filter_kind

            kind = filter_kind(found.kind)
            if kind is not None:
                unknown = set(settings) - {f.key for f in kind.fields}
                if unknown:
                    raise _bad(f"{found.kind} has no setting {', '.join(sorted(unknown))}.")
            found.settings = {**found.settings, **settings}
        self.host.commit("filters")
        return self._filter_info(found)

    @action("filter.toggle", "Turn a filter on if off, off if on.", source="name or id", filter="name, id or kind")
    def filter_toggle(self, data):
        source = self._source(data, "source")
        found = self._filter(source, data)
        found.enabled = not found.enabled
        self.host.commit("filters")
        return self._filter_info(found)

    @action("filter.add", "Add a filter to a source.", source="name or id", kind="kind from filters.kinds",
            name="?its name", settings="?{key: value}", on="?true (default) or false")
    def filter_add(self, data):
        from sprigcast.core.filters import filter_kind

        source = self._source(data, "source")
        kind = filter_kind(_get(data, "kind", str))
        if kind is None:
            raise _missing("No such filter kind (filters.kinds lists them).")
        if kind.media == "video" and any(f.kind == kind.kind for f in source.filters):
            raise ApiError("not_allowed", f"{source.name!r} already has a {kind.label} filter.")
        name = _get(data, "name", str, optional=True, default=kind.label)
        settings = {**kind.defaults, **_get(data, "settings", dict, optional=True, default={})}
        taken = {f.id for s in self.col.sources for f in s.filters}
        n = 1
        while f"filter-{n}" in taken:
            n += 1
        new = Filter(id=f"filter-{n}", kind=kind.kind, name=name, settings=settings,
                     enabled=_get(data, "on", bool, optional=True, default=True))
        source.filters.append(new)
        self.host.commit("filters")
        return self._filter_info(new)

    @action("filter.remove", "Remove a filter from a source.", source="name or id", filter="name, id or kind")
    def filter_remove(self, data):
        source = self._source(data, "source")
        found = self._filter(source, data)
        source.filters = [f for f in source.filters if f is not found]
        self.host.commit("filters")

    @action("filter.move", "Move a filter to a new place in the list (0 = top).", source="name or id",
            filter="name, id or kind", index="new position")
    def filter_move(self, data):
        source = self._source(data, "source")
        found = self._filter(source, data)
        index = int(_get(data, "index", float))
        rest = [f for f in source.filters if f is not found]
        rest.insert(max(0, min(len(rest), index)), found)
        source.filters = rest
        self.host.commit("filters")

    # Sources -------------------------------------------------------------------------------------

    @action("sources.list", "Every source (cameras, text, browser pages, images, sounds...).")
    def sources_list(self, data):
        return {"sources": [{"id": s.id, "name": s.name, "kind": s.kind, "filters": len(s.filters)}
                            for s in self.col.sources if s.kind != PART_KIND]}

    @action("source.get", "A source's settings.", source="name or id")
    def source_get(self, data):
        source = self._source(data, "source")
        return {"id": source.id, "name": source.name, "kind": source.kind, "settings": dict(source.settings)}

    @action("source.set", "Change some of a source's settings: text, a browser page's url, an image file, a colour...",
            source="name or id", settings="{key: value}")
    def source_set(self, data):
        source = self._source(data, "source")
        settings = _get(data, "settings", dict)
        source.settings = {**source.settings, **settings}
        self.host.commit("sources")
        return {"id": source.id, "settings": dict(source.settings)}

    @action("source.rename", "Give a source a new name.", source="name or id", name="new name")
    def source_rename(self, data):
        source = self._source(data, "source")
        name = _get(data, "name", str).strip()
        if not name:
            raise _bad("name is empty.")
        source.name = name
        self.host.commit("items")

    # Stream, record, clips -------------------------------------------------------------------------

    def _status(self, status: dict[str, Any] | None) -> dict[str, Any]:
        if status is None:
            return {"on": False}
        return {"on": True, "seconds": round(float(status.get("seconds", 0.0)), 1),
                **{k: v for k, v in status.items() if k != "seconds"}}

    @action("stream.status", "Live or not, for how long, and dropped frames.")
    def stream_status(self, data):
        return self._status(self.host.stream_status())

    @action("stream.start", "Go live to every place that's switched on.")
    def stream_start(self, data):
        if self.host.stream_status() is not None:
            raise ApiError("busy", "Already live.")
        problem = self.host.start_stream()
        if problem:
            raise ApiError("not_allowed", problem)

    @action("stream.stop", "End the stream.")
    def stream_stop(self, data):
        if self.host.stream_status() is None:
            raise ApiError("not_allowed", "Not live.")
        self.host.stop_stream()

    @action("stream.toggle", "Go live, or end the stream.")
    def stream_toggle(self, data):
        if self.host.stream_status() is None:
            self.stream_start(data)
            return {"on": True}
        self.host.stop_stream()
        return {"on": False}

    @action("places.list", "Where the stream goes (Twitch, YouTube...), each one's shape and if it's on.")
    def places_list(self, data):
        return {"places": [{"id": o.id, "service": o.service, "shape": o.canvas, "on": o.enabled,
                            "bitrateKbps": o.bitrate_kbps} for o in self.col.outputs if o.type == "stream"]}

    @action("place.set", "Switch a place on or off for the next Go live.", place="id from places.list", on="true or false")
    def place_set(self, data):
        wanted = _get(data, "place", str)
        output = next((o for o in self.col.outputs if o.type == "stream" and wanted in (o.id, o.service)), None)
        if output is None:
            raise _missing(f"No place {wanted!r}.")
        if self.host.stream_status() is not None:
            raise ApiError("busy", "End the stream to change where it goes.")
        output.enabled = _get(data, "on", bool)
        self.host.commit("places")

    @action("record.status", "Recording or not, for how long, and the file.")
    def record_status(self, data):
        return self._status(self.host.record_status())

    @action("record.start", "Start recording.")
    def record_start(self, data):
        if self.host.record_status() is not None:
            raise ApiError("busy", "Already recording.")
        problem = self.host.start_record()
        if problem:
            raise ApiError("not_allowed", problem)

    @action("record.stop", "Stop recording.")
    def record_stop(self, data):
        status = self.host.record_status()
        if status is None:
            raise ApiError("not_allowed", "Not recording.")
        self.host.stop_record()
        return {"file": status.get("path", "")}

    @action("record.toggle", "Start or stop recording.")
    def record_toggle(self, data):
        if self.host.record_status() is None:
            self.record_start(data)
            return {"on": True}
        self.host.stop_record()
        return {"on": False}

    @action("clips.status", "Whether clips are ready and how long a clip is.")
    def clips_status(self, data):
        return {"on": self.host.clips_ready(), "seconds": self.col.recording.replay_seconds,
                "shapes": self.col.recording.clip_shapes, "last": self.last_clip}

    @action("clips.set", "Keep clips ready (on) or stop (off).", on="true or false")
    def clips_set(self, data):
        on = _get(data, "on", bool)
        if on != self.host.clips_ready():
            problem = self.host.set_clips(on)
            if problem:
                raise ApiError("not_allowed", problem)

    @action("clip.save", "Save the last seconds as a clip (gets clips ready first if they're off).")
    def clip_save(self, data):
        if not self.host.clips_ready():
            problem = self.host.set_clips(True)
            if problem:
                raise ApiError("not_allowed", problem)
            return {"saved": [], "started": True}
        self.last_clip = self.host.save_clip()
        return {"saved": self.last_clip, "started": False}

    # Transitions ---------------------------------------------------------------------------------

    @action("transition.set", "Change the usual transition and/or its length.", kind="?" + ", ".join(TRANSITION_KINDS),
            ms="?length in milliseconds, 50 to 20000")
    def transition_set(self, data):
        kind = _get(data, "kind", str, optional=True)
        ms = _get(data, "ms", float, optional=True)
        if kind is None and ms is None:
            raise _bad("Give kind, ms or both.")
        if kind is not None:
            if kind not in TRANSITION_KINDS:
                raise _bad(f"kind must be one of {', '.join(TRANSITION_KINDS)}.")
            self.col.transition.kind = kind  # type: ignore[assignment]
        if ms is not None:
            if not 50 <= ms <= 20000:
                raise _bad("ms must be 50 to 20000.")
            self.col.transition.duration_ms = int(ms)
        self.host.commit("transition")
        return {"kind": self.col.transition.kind, "ms": self.col.transition.duration_ms}

    @action("transition.get", "The usual transition and its length.")
    def transition_get(self, data):
        return {"kind": self.col.transition.kind, "ms": self.col.transition.duration_ms, "kinds": list(TRANSITION_KINDS)}

    # Events ---------------------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        col = self.col
        items = {}
        for scene in col.scenes:
            for i, item in enumerate(scene.items):
                items[(scene.id, i, item.source)] = item.visible
        return {
            "onAir": dict(col.active_scenes),
            "staged": self.host.staged() if self.host.studio() else {},
            "studio": self.host.studio(),
            "blooms": [(b.id, b.name) for b in self._blooms()],
            "items": items,
            "sounds": {s.id: (bool(s.settings.get("muted", False)), float(s.settings.get("volume_db", 0.0)))
                       for s in col.sources if self._has_sound(s)},
            "filters": {(s.id, f.id): (f.enabled, json.dumps(f.settings, sort_keys=True, default=str))
                        for s in col.sources for f in s.filters},
            "settings": {s.id: json.dumps(s.settings, sort_keys=True, default=str) for s in col.sources},
            "stream": self.host.stream_status() is not None,
            "record": self.host.record_status() is not None,
            "clips": self.host.clips_ready(),
            "transition": (col.transition.kind, col.transition.duration_ms),
            "states": {p.id: p.state for p in col.parts()},
        }

    def changes(self) -> list[tuple[str, dict[str, Any]]]:
        """(event name, data) for what changed since the last call."""
        now = self.snapshot()
        last, self._last = self._last, now
        if last is None:
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        scenes = {s.id: s for s in self.col.scenes}
        sources = {s.id: s for s in self.col.sources}

        def bloom_ref(scene_id: str) -> dict[str, Any]:
            scene = scenes.get(scene_id)
            main = self._main_shape(scene) if scene is not None and scene.kind == "show" else scene
            return {"bloom": main.name if main else scene_id, "bloomId": main.id if main else scene_id}

        if now["blooms"] != last["blooms"]:
            out.append(("blooms.changed", {"blooms": [{"id": i, "name": n} for i, n in now["blooms"]]}))
        for shape, scene_id in now["onAir"].items():
            if last["onAir"].get(shape) != scene_id:
                out.append(("bloom.shown", {**bloom_ref(scene_id), "shape": shape}))
        if now["studio"] != last["studio"]:
            out.append(("studio.changed", {"on": now["studio"]}))
        for shape, scene_id in now["staged"].items():
            if last["staged"].get(shape) != scene_id:
                out.append(("bloom.linedUp", {**bloom_ref(scene_id), "shape": shape}))
        for key, visible in now["items"].items():
            if key in last["items"] and last["items"][key] != visible:
                scene = scenes.get(key[0])
                where = {"sprig": self.col.main_part(scene).name} if scene is not None and scene.kind == "part" \
                    else bloom_ref(key[0])
                out.append(("item.changed", {**where, "shape": scene.canvas if scene else None,
                                             "item": self._item_name(key[2]), "itemId": self._item_key(key[2]),
                                             "visible": visible}))
        for sid, (muted, db) in now["sounds"].items():
            old = last["sounds"].get(sid)
            if old is not None and old != (muted, db):
                out.append(("sound.changed", self._sound_info(sources[sid])))
        for (sid, fid), state in now["filters"].items():
            old = last["filters"].get((sid, fid))
            if old != state:
                found = next((f for f in sources[sid].filters if f.id == fid), None)
                if found is not None:
                    out.append(("filter.changed", {"source": sources[sid].name, "sourceId": sid,
                                                   "added": old is None, **self._filter_info(found)}))
        for (sid, fid) in set(last["filters"]) - set(now["filters"]):
            name = sources[sid].name if sid in sources else sid
            out.append(("filter.removed", {"source": name, "sourceId": sid, "id": fid}))
        for sid, settings in now["settings"].items():
            if sid in last["settings"] and last["settings"][sid] != settings and sources[sid].kind != PART_KIND:
                out.append(("source.changed", {"source": sources[sid].name, "sourceId": sid,
                                               "settings": dict(sources[sid].settings)}))
        for key, name in (("stream", "stream.changed"), ("record", "record.changed"), ("clips", "clips.changed")):
            if now[key] != last[key]:
                out.append((name, {"on": now[key]}))
        if now["transition"] != last["transition"]:
            out.append(("transition.changed", {"kind": now["transition"][0], "ms": now["transition"][1]}))
        for pid, state in now["states"].items():
            if pid in last["states"] and last["states"][pid] != state:
                out.append(("sprig.state", {"sprig": scenes[pid].name, "sprigId": pid, "state": state}))
        return out

    def meters(self, levels: dict[str, tuple[float, float]]) -> dict[str, Any]:
        return {"levels": {sid: {"peakDb": round(p, 1), "rmsDb": round(r, 1)} for sid, (p, r) in levels.items()}}


def event_group(name: str) -> str:
    """bloom.shown -> bloom; stream.changed -> output; meters -> meters."""
    head = name.split(".", 1)[0]
    return {"blooms": "bloom", "stream": "output", "record": "output", "clips": "output",
            "places": "output", "items": "item"}.get(head, head)


@dataclass
class NativeSession:
    """One app on the SprigCast API. receive() yields texts to send or a Close."""

    api: NativeApi
    password: str = ""
    welcomed: bool = False
    events: set[str] = field(default_factory=lambda: set(EVENT_GROUPS))
    salt: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    challenge: str = field(default_factory=lambda: secrets.token_urlsafe(24))

    def hello(self) -> str:
        hello: dict[str, Any] = {"app": "SprigCast", "version": __version__, "api": API_VERSION,
                                 "auth": {"salt": self.salt, "challenge": self.challenge} if self.password else None}
        return json.dumps({"hello": hello})

    def wants(self, group: str) -> bool:
        return self.welcomed and group in self.events

    def event(self, name: str, data: dict[str, Any]) -> str | None:
        if not self.wants(event_group(name)):
            return None
        return json.dumps({"event": name, **data}, default=str)

    def _set_events(self, wanted: Any) -> None:
        if wanted == "all":
            self.events = set(EVENT_GROUPS)
        elif wanted == "none":
            self.events = set()
        elif isinstance(wanted, list):
            self.events = {str(w) for w in wanted}

    def receive(self, text: str) -> Iterator[str | Close]:
        try:
            message = json.loads(text)
        except ValueError:
            yield Close(4002, "Messages must be JSON.")
            return
        if not isinstance(message, dict):
            yield Close(4002, "Messages must be JSON objects.")
            return
        if not self.welcomed:
            if self.password:
                answer = message.get("auth")
                if not isinstance(answer, str) or not secrets.compare_digest(
                        answer, auth_string(self.password, self.salt, self.challenge)):
                    yield Close(4009, "Wrong password.")
                    return
            self._set_events(message.get("events", "all"))
            self.welcomed = True
            yield json.dumps({"welcome": {"api": API_VERSION, "events": sorted(self.events)}})
            if "do" not in message:
                return
        if "events" in message and "do" not in message:
            self._set_events(message["events"])
            yield json.dumps({"welcome": {"api": API_VERSION, "events": sorted(self.events)}})
            return
        yield json.dumps(self.answer(message), default=str)

    def answer(self, message: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"id": message.get("id")}
        name = message.get("do")
        try:
            if not isinstance(name, str):
                raise _bad("do is missing: say which action, like {\"do\": \"bloom.show\", \"bloom\": \"BRB\"}.")
            data = {k: v for k, v in message.items() if k not in ("do", "id")}
            result = self.api.run(name, data)
        except ApiError as exc:
            out.update(ok=False, error={"code": exc.code, "message": exc.message})
            return out
        except Exception as exc:  # a bug must not drop the connection
            out.update(ok=False, error={"code": "failed", "message": f"{type(exc).__name__}: {exc}"})
            return out
        out["ok"] = True
        if result is not None:
            out["result"] = result
        return out
