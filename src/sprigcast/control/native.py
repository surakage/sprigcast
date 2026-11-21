"""The SprigCast API: SprigCast's own control language, in its own words.

Connect a WebSocket to ws://127.0.0.1:<port>/sprigcast (the port in Settings → Connections).

    server → {"hello": {"app": "SprigCast", "version": "0.1.0", "api": 1, "auth": {"salt": "…", "challenge": "…"}}}
    app    → {"auth": "<answer>", "events": ["bloom", "sound"]}       # auth only when a password is set
    server → {"welcome": {"api": 1, "events": ["bloom", "sound"]}}
    app    → {"pair": "123456", "name": "Stream Deck"}                # instead of auth: the code from Pair a device
    server → {"paired": {"key": "sc_…", "name": "Stream Deck"}}      # then welcome; use the key as the password
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
import logging
import math
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from sprigcast import __version__
from sprigcast.control.protocol import Close, Sleep, auth_string
from sprigcast.control.who import sound_changed
from sprigcast.core.scene import MOVE_EASINGS, MOVE_UNMATCHED, PART_KIND, TRANSITION_KINDS, Filter, Scene, SceneItem, SourceConfig

log = logging.getLogger(__name__)

API_VERSION = 0  # 0 = preview: names and fields may still change. 1 once it settles.
# What this SprigCast can do, for apps to check (app.info "features") instead of guessing from answers' shapes.
# Names are only ever added, never removed or renamed.
FEATURES = (
    "bloom.new",           # bloom.new, bloom.remove, source.new, item.add
    "places.status",       # stream.status places, place.start, place.stop
    "screenshot.source",   # screenshot.take with source or bloom
    "plugins.enabled",     # plugins.list: id, enabled, store plugins not added
    "plugin.missing",      # plugin_missing error
    "effects.autoend",     # hide.*, roll.spin, freeze.* end by themselves after seconds (1-600); a new call restarts the time
    "source.rotate",
    "start.warnings",      # stream.start / record.start answer doctor_warnings, force: true goes ahead
    "canvas.size",         # app.info canvases: id, width, height, fps, enabled
    "item.scale_xy",       # item.place scale_x / scale_y
    "events.effect",       # effect.ended
    "events.plugins",      # plugins.changed
    "events.lists",        # sources.changed, items.changed
    "notfound.what",       # not_found errors carry what and name
    "page.api",            # Web page sources: window.sprigcast (app, version, visible, bloom...) and its events
    "effects.freeze",      # freeze.start / freeze.stop / freeze.toggle (Freeze plugin), effect.ended "freeze.start"
    "record.pause",        # record.pause / resume / toggle_pause / chapter, record.status paused, record.paused event
    "place.bandwidth_test",  # place.set bandwidth_test (Twitch test mode); places.list says it where there is one
    "place.bitrate",  # place.set bitrateKbps (500-50000, as the window allows), for the next Go live
    "screenshot.bloom_off_air",  # screenshot.take {bloom, shape}: any bloom on its canvas without putting it on air
    "item.place_full",     # item.place crop, bounds, bounds_width/height, bounds_align, align
    "item.edit",           # item.lock, item.remove, item.duplicate, source.remove, source.refresh
    "edit.undo",           # edit.undo / edit.redo; {"steps", "one_step": true}: all or none, one undo step
    "steps.dry_run",       # {"steps", "dry_run": true}: nothing kept; answers the layouts and overlaps it would make
    "events.item_moved",   # item.moved when something is moved, sized, cropped or turned
    "filters.share",       # a placed sprig's filters follow on its other canvases; filters.share, filters.list same_on_both_canvases
    "layout.get",          # layout.get: every item's place, crop, bounds, source size and canvas box; flat, overlaps
    "source.state",        # source.get loaded, state ("capturing 1920x1080"), size, cameraSize, asleep
    "places.size",         # places.list size [w, h], fps, and enhanced where the service has it
    "screenshot.skipped",  # screenshot.take {bloom}: every shape it's in, skipped names a shape left out; canvas pictures are on black
    "places.ready",        # places.list name, ready, problem ("Needs a stream key"): why a place can't go live yet
    "app_audio.pids",      # app_audio settings pids: [process id, ...] catches those processes' trees instead of apps
    "sprig.remove",        # sprig.remove: a sprig in every shape; sounds always stay
)


def features() -> list[str]:
    """FEATURES, plus browser.h264 when Web pages play H.264/AAC themselves (<video>, MediaSource, iframes: the
    page engine with the codecs, whose overlay leaves app/page_video.MARKER). Without it only MP4 files play
    (converted to WebM), not a streaming player such as Twitch's embed."""
    from sprigcast.app.page_video import engine_plays_h264

    return [*FEATURES, "browser.h264"] if engine_plays_h264() else list(FEATURES)


# What {"steps": [...], "one_step": true} and "dry_run": true may hold: changes to the garden only, so all of them
# can be taken back together.
BATCH_ACTIONS = frozenset({
    "item.add", "item.place", "item.order", "item.show", "item.hide", "item.toggle", "item.lock", "item.remove",
    "item.duplicate", "source.new", "source.set", "source.rename", "source.rotate", "source.remove", "sprig.remove",
    "filter.add", "filter.set", "filter.remove", "filter.move", "items.list", "layout.get",
    "sound.mute", "sound.volume", "sound.balance"})  # a sound's own settings: saved in the garden like the rest
PREVIEW = API_VERSION == 0
EVENT_GROUPS = ("bloom", "item", "sprig", "sound", "filter", "source", "output", "transition", "media", "custom",
                "plugin", "garden", "path")
HIGH_VOLUME = ("meters",)  # only when asked for by name: about 15 a second
SOUND_KINDS = ("audio_input", "audio_output", "app_audio", "media")
MAX_STEPS = 100  # in one {"steps": [...]} list
MAX_WAIT_MS = 10 * 60 * 1000  # all its waits together


class ApiError(Exception):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra  # more for the app, e.g. doctor_warnings' list of warnings


class Later:
    """An answer that needs slow work first (a screenshot): job() runs on a worker thread, then then(future) on the
    window's thread turns its outcome into the answer (future.result() raises what the job raised).

    Only a connected app's single message is answered this way (NativeSession.receive): the window's thread never
    waits for the picture. Waiting there for it, events still handled (workers.wait_answering), left the window not
    answering for 514 ms on the PC (2026-11-20, build 44792233314)."""

    def __init__(self, job: Callable[[], Any], then: Callable[[Any], Any]):
        self.job, self.then = job, then

    def map(self, after: Callable[[Any], Any]) -> Later:
        first = self.then
        return Later(self.job, lambda future: after(first(future)))


def _saving(later: Later) -> Later:
    """A screenshot's Later whose file couldn't be written answers "failed", as the direct call does."""
    first = later.then

    def then(future):
        try:
            return first(future)
        except OSError as exc:
            raise ApiError("failed", f"Couldn't save the screenshot: {exc}") from exc

    return Later(later.job, then)


def _bad(message: str) -> ApiError:
    return ApiError("bad_request", message)


def _missing(message: str, what: str = "", name: Any = None) -> ApiError:
    """not_found, saying what kind of thing (bloom, sprig, source, item, filter, shape, state, place, hotkey,
    kind) and the name asked for, so an app can say exactly what's missing."""
    extra = {"what": what, "name": name} if what else {}
    return ApiError("not_found", message, **extra)


@dataclass
class Action:
    name: str
    fields: dict[str, str]  # field -> short description ("?" first = optional)
    about: str
    run: Callable[[dict[str, Any]], Any]
    module: str = ""  # the plugin that added it


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


def _checked_settings(col, kind: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Settings an app sends, checked by the source's kind when it can (a plugin's check_settings(col, settings)
    returns them tidied, e.g. a name matched to an id, or raises ValueError saying what's wrong)."""
    from sprigcast.core.source import source_class

    check = getattr(source_class(kind), "check_settings", None)
    if check is None:
        return settings
    try:
        return dict(check(col, dict(settings)))
    except ValueError as exc:
        raise _bad(str(exc)) from None


_PLUGIN_ACTIONS: dict[str, Action] = {}
ALWAYS_SENT = "app.closing"  # sent just before SprigCast closes (or its server stops): look again soon
_PLUGIN_EVENTS: set[str] = set()  # the first word of events plugins send ("snap" for snap.taken): group "plugin"


def send_event(api, name: str, data: dict[str, Any]) -> None:
    """For plugins: tell connected apps something happened, e.g. send_event(api, "snap.taken", {...}).

    Apps get it in the "plugin" event group as {"event": name, ...data}. Call it on the UI thread
    (an action, hotkey or right-click entry is).
    """
    if "." not in name:
        raise ValueError("plugin events are named '<plugin>.<what>'")
    _PLUGIN_EVENTS.add(name.split(".", 1)[0])
    api.plugin_event(name, data)


def register_action(name: str, about: str, fields: dict[str, str], run: Callable[[Any, dict[str, Any]], Any]) -> None:
    """For plugins: add an action to the SprigCast API. Name it "<plugin>.<thing>".

    run(api, data) gets the NativeApi (api.col is the scene collection, api.host.commit("items")
    saves and redraws after a change) and the fields the app sent; raise ApiError to fail.
    """
    if "." not in name:
        raise ValueError("name plugin actions <plugin>.<thing>")
    import sys

    module = sys._getframe(1).f_globals.get("__name__", "")
    _PLUGIN_ACTIONS[name] = Action(name, dict(fields), about, run, module)


_STORE_ACTIONS: dict[str, str] | None = None


def _store_action_plugins() -> dict[str, str]:
    """Action name -> the store plugin that brings it ("roll.spin" -> "Barrel Roll"), read from the store's files
    without running them, so an app asking for one that isn't added hears which plugin to add."""
    global _STORE_ACTIONS
    if _STORE_ACTIONS is None:
        import re

        from sprigcast.plugin_store import catalog

        found: dict[str, str] = {}
        for plugin in catalog():
            try:
                text = plugin.file.read_text(encoding="utf-8")
            except OSError:
                continue
            for name in re.findall(r'register_action\(\s*"([a-zA-Z0-9_]+\.[a-zA-Z0-9_]+)"', text):
                found[name] = plugin.name
        _STORE_ACTIONS = found
    return _STORE_ACTIONS


def unregister_actions(module: str) -> list[str]:
    """Forget the actions a plugin module added (when it's removed)."""
    gone = [n for n, a in _PLUGIN_ACTIONS.items() if module and a.module == module]
    for name in gone:
        del _PLUGIN_ACTIONS[name]
    return gone


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
        self._last: dict[str, Any] | None = None
        self.custom_event: Callable[[dict[str, Any]], None] = lambda data: None
        self.plugin_event: Callable[[str, dict[str, Any]], None] = lambda name, data: None  # set by the server
        self.last_clip: list[str] = []
        self.later_ok = False  # set while a connected app's single message is answered: slow work may come Later

    def _later(self, name: str):
        """The host's Later version of a slow call, when one may be used now; else None."""
        return getattr(self.host, name, None) if self.later_ok else None

    @property
    def col(self):
        return self.host.collection

    def all_actions(self) -> dict[str, Action]:
        """Built-in actions and plugin ones (plugins can arrive and leave while SprigCast runs)."""
        out = dict(self.actions)
        for name, extra in _PLUGIN_ACTIONS.items():
            if name not in out:
                out[name] = Action(name, extra.fields, extra.about, lambda data, run=extra.run: run(self, data))
        return out

    def run(self, name: str, data: dict[str, Any]) -> Any:
        found = self.actions.get(name)
        if found is None and name in _PLUGIN_ACTIONS:
            return _PLUGIN_ACTIONS[name].run(self, data)
        if found is None:
            plugin = _store_action_plugins().get(name)
            if plugin is not None:  # a store plugin's action, but that plugin isn't added
                raise ApiError("plugin_missing", f"Add the {plugin} plugin in SprigCast (Tools → Plugin store).")
            raise ApiError("unknown_action", f"No action {name!r}. app.actions lists them all.")
        return found.run(data)

    # Lookups ---------------------------------------------------------------------------

    @action("bloom.new", "Make an empty bloom (with its own sprig, where what you add goes).", name="name",
            shape="?both (default: every canvas), horizontal or vertical")
    def bloom_new(self, data):
        name = _get(data, "name", str).strip()
        if not name:
            raise _bad("name is empty.")
        if any(s.kind == "show" and s.name == name for s in self.col.scenes):
            raise ApiError("busy", f"There's already a bloom called {name!r}.")
        shape = self._shape(data)
        canvases = [c.id for c in self.col.canvases if c.enabled or c.id == self.col.canvases[0].id]
        canvases = canvases if shape == "both" else [shape]
        bloom = self.col.new_bloom(name, canvases)
        self.host.commit("scenes")
        return self._bloom_info(bloom)

    @action("bloom.remove", "Remove a bloom (in every shape). Its sources stay while other blooms use them.",
            bloom="name or id")
    def bloom_remove(self, data):
        bloom = self._bloom(data)
        if all(len(self.col.scenes_for(s.canvas)) <= 1 for s in self.col.linked_with(bloom.id)):
            raise ApiError("not_allowed", "Each canvas needs at least one bloom.")
        self.col.remove_bloom(bloom)
        self.host.commit("scenes")
        return {"removed": bloom.name}

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
            raise _missing(f"No bloom called {wanted!r}.", "bloom", wanted)
        return self._main_shape(scene)

    def _main_shape(self, scene: Scene, families: dict[str, list[Scene]] | None = None) -> Scene:
        family = families[scene.id] if families is not None and scene.id in families else self.col.linked_with(scene.id)
        order = {cid: i for i, cid in enumerate(self._shapes())}
        return min(family, key=lambda s: order.get(s.canvas, 99))

    def _families(self) -> dict[str, list[Scene]]:
        """Every scene's linked_with, worked out in one pass. Asked scene by scene (each a walk of every scene,
        twice), the API's look at what changed held the window 409 ms on the PC with a big garden while an app
        was connected (2026-11-18 pop-up sweep)."""
        scenes = self.col.scenes
        index = {id(s): i for i, s in enumerate(scenes)}
        by_id: dict[str, Scene] = {}
        for s in scenes:
            by_id.setdefault(s.id, s)  # scene() finds the first of an id
        partners: dict[str, list[Scene]] = {s.id: [] for s in scenes}
        for o in scenes:
            target = by_id.get(o.linked_to) if o.linked_to else None
            if target is None or target is o or o.canvas == target.canvas:
                continue
            if o.kind == "show":
                partners[target.id].append(o)  # in linked_with(target): o.linked_to == scene.id
            if target.kind == "show":
                partners[o.id].append(target)  # in linked_with(o): scene.linked_to == target.id
        out = {}
        for sid, scene in by_id.items():
            seen, others = set(), []
            for o in sorted(partners[sid], key=lambda x: index[id(x)]):
                if id(o) not in seen and o is not scene:
                    seen.add(id(o))
                    others.append(o)
            out[sid] = [scene] + others
        return out

    def _in_shape(self, bloom: Scene, shape: str) -> list[Scene]:
        family = self.col.linked_with(bloom.id)
        if shape == "both":
            return family
        found = [s for s in family if s.canvas == shape]
        if not found:
            raise _missing(f"{bloom.name!r} has no {shape} shape.", "shape", shape)
        return found

    def _sprig(self, data: dict[str, Any], key: str = "sprig") -> Scene:
        wanted = _get(data, key, str)
        parts = self.col.parts()
        part = next((p for p in parts if p.id == wanted), None) or next((p for p in parts if p.name == wanted), None)
        if part is None:
            raise _missing(f"No sprig called {wanted!r}.", "sprig", wanted)
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
            raise _missing(f"No source called {wanted!r}.", "source", wanted)
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

    def _items(self, data: dict[str, Any]) -> tuple[Scene, list[SceneItem]]:
        """The holder and the items named by "item". Blooms hold sprigs only, so an item that isn't in the bloom
        itself is looked for in the bloom's own sprig: {"bloom": "Chatting", "item": "Cam"} finds the Cam."""
        holder, layouts = self._holder(data)
        wanted = _get(data, "item", str)
        items = self._matching_items(layouts, wanted)
        if not items and data.get("sprig") is None:
            inner = []
            for layout in layouts:
                sprig = self.col.bloom_sprig(layout)
                shape = None if sprig is None else self.col.shape_for(sprig, layout.canvas)
                if shape is not None and all(shape.id != s.id for s in inner):
                    inner.append(shape)
            items = self._matching_items(inner, wanted)
        if not items:
            raise _missing(f"{holder.name!r} has no item {wanted!r}.", "item", wanted)
        return holder, items

    def _filter(self, source: SourceConfig, data: dict[str, Any]) -> Filter:
        wanted = _get(data, "filter", str)
        found = next((f for f in source.filters if f.id == wanted), None) \
            or next((f for f in source.filters if f.name == wanted), None) \
            or next((f for f in source.filters if f.kind == wanted), None)
        if found is None:  # a filter's name today or before the one-word names ("Greenscreen" or "Chroma Key")
            from sprigcast.core.filters import kind_for_label

            kind = kind_for_label(wanted)
            found = next((f for f in source.filters if kind and f.kind == kind), None)
        if found is None:
            raise _missing(f"{source.name!r} has no filter {wanted!r}.", "filter", wanted)
        return found

    # Describing -------------------------------------------------------------------------

    def _bloom_info(self, bloom: Scene) -> dict[str, Any]:
        family = self.col.linked_with(bloom.id)
        on_air = [s.canvas for s in family if self.col.active_scenes.get(s.canvas) == s.id]
        folder = next((f for f in self.col.folders if f.id == bloom.folder), None)
        return {"id": bloom.id, "name": bloom.name, "shapes": {s.canvas: s.id for s in family},
                "onAir": on_air, "folder": folder.name if folder else None, "live": bool(folder and folder.live)}

    def _blooms(self, families: dict[str, list[Scene]] | None = None) -> list[Scene]:
        families = self._families() if families is None else families
        seen: set[str] = set()
        out = []
        for canvas_id in self._shapes():
            for scene in self.col.scenes_for(canvas_id):
                if scene.id in seen:
                    continue
                main = self._main_shape(scene, families)
                seen.update(s.id for s in families[scene.id])
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

    @action("app.info", "Version, API version, the shapes that are on, each canvas's size, event groups and features.")
    def app_info(self, data):
        return {"app": "SprigCast", "version": __version__, "api": API_VERSION, "preview": PREVIEW,
                "shapes": [c.id for c in self.col.canvases if c.enabled],
                "canvases": [{"id": c.id, "width": c.width, "height": c.height, "fps": c.fps, "enabled": c.enabled}
                             for c in self.col.canvases],
                "events": list(EVENT_GROUPS + HIGH_VOLUME), "features": features()}

    @action("app.actions", "Every action with what it does and its fields (fields starting with ? are optional).")
    def app_actions(self, data):
        return {"actions": [{"do": a.name, "about": a.about, "fields": a.fields}
                            for a in sorted(self.all_actions().values(), key=lambda a: a.name)]}

    @action("plugins.list", "The plugins that are added (enabled) and the store's others (not enabled), each with "
            "the actions it brings.")
    def plugins_list(self, data):
        import sys

        from sprigcast.plugin_store import catalog

        found: dict[str, dict[str, Any]] = {}
        for extra in sorted(_PLUGIN_ACTIONS.values(), key=lambda a: a.name):
            if extra.module.startswith("sprigcast_plugin_") and extra.module not in sys.modules:
                continue  # removed
            head = getattr(sys.modules.get(extra.module), "PLUGIN", None) or {}
            pid = head.get("id") or extra.module.removeprefix("sprigcast_plugin_") or extra.name.split(".", 1)[0]
            entry = found.setdefault(pid, {"id": pid, "plugin": pid, "name": head.get("name", pid),
                                           "version": head.get("version"), "enabled": True, "actions": []})
            entry["actions"].append(extra.name)
        store = _store_action_plugins()
        for plugin in catalog():
            if plugin.id not in found:
                found[plugin.id] = {"id": plugin.id, "plugin": plugin.id, "name": plugin.name, "version": plugin.version,
                                    "enabled": False, "actions": sorted(a for a, n in store.items() if n == plugin.name)}
        return {"plugins": sorted(found.values(), key=lambda p: (not p["enabled"], str(p["name"]).lower()))}

    @action("app.stats", "Frames per second, render time and memory.")
    def app_stats(self, data):
        return self.host.stats()

    @action("app.broadcast", "Send your own event to every connected app.", data="any object")
    def app_broadcast(self, data):
        self.custom_event(_get(data, "data", dict))

    @action("hotkey.run", "Do what a hotkey does (hotkey.list shows them). note says why when it couldn't.",
            hotkey="hotkey action id")
    def hotkey_run(self, data):
        wanted = _get(data, "hotkey", str)
        if wanted not in {a for a, _ in self.host.hotkeys()}:
            raise _missing(f"No hotkey action {wanted!r}.", "hotkey", wanted)
        note = self.host.trigger_hotkey(wanted)
        return {"hotkey": wanted, "note": note if isinstance(note, str) else None}

    @action("hotkey.list", "Hotkey actions and what they do.")
    def hotkey_list(self, data):
        return {"hotkeys": [{"id": a, "about": label} for a, label in self.host.hotkeys()]}

    # Gardens and paths -------------------------------------------------------------------
    # A garden is a whole set of blooms (OBS's scene collection); a path is where and how the show
    # goes out: places, encoder, recording (OBS's profile). See core/gardens.py.

    def _shelf(self) -> dict[str, Any]:
        lists = getattr(self.host, "shelf_lists", None)
        if lists is None:
            return {"garden": "Main", "gardens": ["Main"], "path": "Main", "paths": ["Main"]}
        return lists()

    def _shelf_do(self, kind: str, op: str, name: str, new_name: str = "") -> None:
        run = getattr(self.host, "shelf_do", None)
        if run is None:
            raise ApiError("not_allowed", "Not here.")
        problem = run(kind, op, name, new_name)
        if problem:
            code = "not_found" if problem.startswith("No ") else "busy" if problem.startswith("Stop ") else \
                "not_allowed"
            raise ApiError(code, problem)

    @action("gardens.list", "Every garden (a whole set of blooms) A-Z, and the one in use.")
    def gardens_list(self, data):
        shelf = self._shelf()
        return {"garden": shelf["garden"], "gardens": shelf["gardens"]}

    @action("garden.switch", "Switch to another garden.", garden="name")
    def garden_switch(self, data):
        self._shelf_do("garden", "switch", _get(data, "garden", str))
        return {"garden": self._shelf()["garden"]}

    @action("garden.new", "Make a garden and switch to it: empty, or a copy of one.", name="name for it",
            copy="?garden to copy")
    def garden_new(self, data):
        name, copy = _get(data, "name", str), _get(data, "copy", str, optional=True)
        if copy:
            self._shelf_do("garden", "duplicate", copy, name)
        else:
            self._shelf_do("garden", "new", name)
        self._shelf_do("garden", "switch", name)
        return {"garden": self._shelf()["garden"]}

    @action("paths.list", "Every path (where and how you go out: places, encoder, recording) A-Z, and the one in use.")
    def paths_list(self, data):
        shelf = self._shelf()
        return {"path": shelf["path"], "paths": shelf["paths"]}

    @action("path.switch", "Switch to another path (not while live or recording).", path="name")
    def path_switch(self, data):
        self._shelf_do("path", "switch", _get(data, "path", str))
        return {"path": self._shelf()["path"]}

    @action("path.new", "Make a path and switch to it: empty, or a copy of one.", name="name for it",
            copy="?path to copy")
    def path_new(self, data):
        name, copy = _get(data, "name", str), _get(data, "copy", str, optional=True)
        if copy:
            self._shelf_do("path", "duplicate", copy, name)
        else:
            self._shelf_do("path", "new", name)
        self._shelf_do("path", "switch", name)
        return {"path": self._shelf()["path"]}

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

    # Items and sprigs -------------------------------------------------------------------------

    @action("items.list", "What's inside a bloom or sprig, per shape.", bloom="?name or id", sprig="?name or id")
    def items_list(self, data):
        _, layouts = self._holder({**data, "shape": "both"})
        return {"shapes": {layout.canvas: [
            {"id": self._item_key(i.source), "name": self._item_name(i.source), "visible": i.visible,
             "locked": i.locked, "sprig": self._item_key(i.source) != i.source}
            for i in reversed(layout.items)] for layout in layouts}}  # top of the list first, as in Build

    @action("layout.get", "Where everything in a bloom or sprig sits, per shape, top of the stack first: each item's "
            "place, crop, bounds, its source's own size and its box on the canvas. Sprigs carry what's inside them.",
            bloom="?name or id", sprig="?name or id", shape="?both (default), horizontal or vertical",
            flat="?true: only the things themselves (no sprigs), as they end up on the canvas",
            overlaps="?true: also the pairs of shown things whose boxes overlap")
    def layout_get(self, data):
        from sprigcast.control import layout as lay

        flat = _get(data, "flat", bool, optional=True, default=False)
        want_overlaps = _get(data, "overlaps", bool, optional=True, default=False)
        holder, layouts = self._holder(data)
        sizes = dict(getattr(self.host, "source_sizes", dict)() or {})
        shapes = {}
        for layout in layouts:
            canvas = next((c for c in self.col.canvases if c.id == layout.canvas), None)
            rows = lay.describe(self.col, layout, sizes, self._item_name, self._item_key)
            shapes[layout.canvas] = {
                "canvas": {"width": canvas.width, "height": canvas.height} if canvas is not None else None,
                "items": lay.flatten(rows) if flat else rows,
                **({"overlaps": lay.overlaps(rows)} if want_overlaps else {})}
        return {"bloom" if holder.kind == "show" else "sprig": holder.name, "id": holder.id, "shapes": shapes}

    def _add_to(self, data) -> tuple[Scene, list[Scene]]:
        """Where item.add puts things: a sprig's layouts, or a bloom's own sprig (blooms hold sprigs only)."""
        holder, layouts = self._holder(data)
        if holder.kind == "part":
            return holder, layouts
        inner: list[Scene] = []
        for layout in layouts:
            sprig = self.col.give_sprig(layout)  # an empty bloom made in Build gets its sprig now
            shape = self.col.shape_for(sprig, layout.canvas)
            if shape.canvas != layout.canvas:
                shape = self.col.add_shape(sprig, layout.canvas, self.col.new_id(f"{sprig.id}-{layout.canvas}"))
            if all(shape.id != s.id for s in inner):
                inner.append(shape)
        return holder, inner

    @action("item.add", "Put a source in a bloom (in its sprig) or a sprig, on top (a plugin starts readable, in a free corner). Answers the item's id.",
            source="name or id", bloom="?bloom to add to", sprig="?sprig to add to",
            shape="?both (default), horizontal or vertical", visible="?true (default) or false")
    def item_add(self, data):
        source = self._source(data, "source")
        visible = _get(data, "visible", bool, optional=True, default=True)
        holder, layouts = self._add_to(data)
        for layout in layouts:
            item = SceneItem(source=source.id, visible=visible)
            self.col.starting_place(item, source.kind, layout)  # a plugin starts readable, as from the + menu
            layout.items.append(item)  # last is on top
        self.host.commit("items")
        return {"item": source.id, "name": source.name, "shapes": [layout.canvas for layout in layouts]}

    def _set_items(self, data, visible: bool | None):
        _, items = self._items(data)
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

    @action("item.status", "Whether something inside a bloom or sprig is shown.", item="name or id",
            bloom="?bloom it's in", sprig="?sprig it's in", shape="?both (default), horizontal or vertical")
    def item_status(self, data):
        _, items = self._items(data)
        return {"visible": items[0].visible}

    @action("item.place", "Move, size or turn something inside a bloom or sprig (only the fields you give change).",
            item="name or id", bloom="?bloom it's in", sprig="?sprig it's in",
            shape="?both (default), horizontal or vertical", x="?left edge, canvas pixels", y="?top edge",
            scale="?size, 1 = as big as the source", scale_x="?width only, 1 = as wide as the source",
            scale_y="?height only, 1 = as tall as the source", rotation="?degrees clockwise",
            dx="?pixels to move right (negative: left)", dy="?pixels to move down (negative: up)",
            crop="?{left, top, right, bottom} in source pixels (the sides you give change)",
            bounds="?off, fit, fill or stretch: squeeze it into a box", bounds_width="?the box's width, canvas pixels",
            bounds_height="?the box's height", bounds_align="?where the picture sits in its box (top_left ... center ... bottom_right)",
            align="?the point it turns around (top_left ... center ... bottom_right); the picture stays where it is")
    def item_place(self, data):
        from sprigcast.core.scene import ALIGNS, BOUNDS

        holder, items = self._items(data)
        keys = ("x", "y", "scale", "scale_x", "scale_y", "rotation", "dx", "dy")
        values = {key: _get(data, key, float, optional=True) for key in keys}
        crop = _get(data, "crop", dict, optional=True)
        bounds = _get(data, "bounds", str, optional=True)
        box = {key: _get(data, key, float, optional=True) for key in ("bounds_width", "bounds_height")}
        aligns = {key: _get(data, key, str, optional=True) for key in ("bounds_align", "align")}
        more = crop is not None or bounds is not None or any(v is not None for v in (*box.values(), *aligns.values()))
        if all(v is None for v in values.values()) and not more:
            raise _bad("Give x, y, scale, scale_x, scale_y, rotation, dx, dy, crop, bounds or align.")
        if crop is not None:
            if not crop or any(k not in ("left", "top", "right", "bottom") for k in crop):
                raise _bad("crop takes left, top, right and bottom.")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100_000 for v in crop.values()):
                raise _bad("crop sides are pixels, 0 or more.")
        if bounds is not None and bounds not in BOUNDS:
            raise _bad(f"bounds must be one of {', '.join(BOUNDS)}.")
        for key, value in box.items():
            if value is not None and not 1 <= value <= 100_000:
                raise _bad(f"{key} must be 1 to 100000.")
        for key, value in aligns.items():
            if value is not None and value not in ALIGNS:
                raise _bad(f"{key} must be one of {', '.join(ALIGNS)}.")
        if more:
            sizes = dict(getattr(self.host, "source_sizes", dict)() or {})
            for item in items:
                wide = (box["bounds_width"] or item.bounds_size[0], box["bounds_height"] or item.bounds_size[1])
                mode = bounds if bounds is not None else item.bounds
                if mode != "off" and (bounds is not None or any(v is not None for v in box.values())) \
                        and not (wide[0] > 0 and wide[1] > 0):
                    raise _bad("Give bounds_width and bounds_height with bounds.")
            for item in items:
                if crop is not None:
                    sides = dict(zip(("left", "top", "right", "bottom"), item.crop))
                    sides.update({k: int(v) for k, v in crop.items()})
                    item.crop = (sides["left"], sides["top"], sides["right"], sides["bottom"])
                if box["bounds_width"] is not None or box["bounds_height"] is not None:
                    item.bounds_size = (box["bounds_width"] or item.bounds_size[0],
                                        box["bounds_height"] or item.bounds_size[1])
                if bounds is not None:
                    item.bounds = bounds
                if aligns["bounds_align"] is not None:
                    item.bounds_align = aligns["bounds_align"]
                if aligns["align"] is not None:
                    size = self._size_of(item, sizes)
                    if size:
                        item.set_align(size, aligns["align"])  # the picture stays put; only the pin moves
                    else:
                        item.align = aligns["align"]
        for key in ("scale", "scale_x", "scale_y"):
            if values[key] is not None and not 0.01 <= values[key] <= 100:
                raise _bad(f"{key} must be 0.01 to 100.")
        for item in items:
            if values["scale"] is not None:
                ratio = None if item.scale_y is None or not item.scale else item.scale_y / item.scale
                item.scale = values["scale"]
                item.scale_y = None if ratio is None else values["scale"] * ratio
            if values["scale_x"] is not None or values["scale_y"] is not None:  # stretch: each way on its own
                tall = values["scale_y"] if values["scale_y"] is not None else item.sy
                item.scale = values["scale_x"] if values["scale_x"] is not None else item.scale
                item.scale_y = None if math.isclose(tall, item.scale) else tall
            for key, attr in (("x", "x"), ("y", "y"), ("rotation", "rotation")):
                if values[key] is not None:
                    setattr(item, attr, values[key])
            item.x += values["dx"] or 0.0
            item.y += values["dy"] or 0.0
        self.host.commit("items")
        stretch = values["scale_x"] is not None or values["scale_y"] is not None
        return {"items": [{"x": i.x, "y": i.y, "scale": i.scale, "rotation": i.rotation,
                           **({"scale_x": i.scale, "scale_y": i.sy} if stretch else {}),
                           **({"crop": dict(zip(("left", "top", "right", "bottom"), i.crop)),
                               "bounds": {"mode": i.bounds, "width": i.bounds_size[0], "height": i.bounds_size[1],
                                          "align": i.bounds_align}, "align": i.align} if more else {})}
                          for i in items]}

    def _size_of(self, item: SceneItem, sizes: dict) -> tuple[int, int] | None:
        """An item's source's own size (a sprig: its canvas), or None while it isn't known."""
        from sprigcast.control import layout as lay

        scene = next((sc for sc in self.col.scenes if any(i is item for i in sc.items)), None)
        return lay.source_size(self.col, item, scene.canvas if scene is not None else "", sizes)

    def _placed(self, data: dict[str, Any]) -> tuple[Scene, list[tuple[Scene, SceneItem]]]:
        """Like _items, with the layout each item sits in (for taking one out or copying it)."""
        holder, items = self._items(data)
        return holder, [(scene, item) for item in items for scene in self.col.scenes
                        if any(i is item for i in scene.items)]

    @action("item.lock", "Lock something in place in Build, or unlock it (no locked: toggle).", item="name or id",
            bloom="?bloom it's in", sprig="?sprig it's in", shape="?both (default), horizontal or vertical",
            locked="?true or false")
    def item_lock(self, data):
        _, items = self._items(data)
        locked = _get(data, "locked", bool, optional=True)
        locked = (not items[0].locked) if locked is None else locked
        for item in items:
            item.locked = locked
        self.host.commit("items")
        return {"locked": locked}

    @action("item.remove", "Take something out of a bloom or sprig. Its source stays (source.remove removes that).",
            item="name or id", bloom="?bloom it's in", sprig="?sprig it's in",
            shape="?both (default), horizontal or vertical")
    def item_remove(self, data):
        holder, placed = self._placed(data)
        name = self._item_name(placed[0][1].source)
        for scene, item in placed:
            scene.items[:] = [i for i in scene.items if i is not item]
        self.host.commit("items")
        return {"removed": name, "shapes": sorted({scene.canvas for scene, _ in placed})}

    @action("item.duplicate", "Copy something in a bloom or sprig: a new source with the same settings and filters, "
                              "placed the same way, on top. Sprigs can't be copied this way.",
            item="name or id", bloom="?bloom it's in", sprig="?sprig it's in",
            shape="?both (default), horizontal or vertical", name="?the copy's name (default: its name and a number)")
    def item_duplicate(self, data):
        holder, placed = self._placed(data)
        source = next((s for s in self.col.sources if s.id == placed[0][1].source), None)
        if source is None or source.kind == PART_KIND:
            raise _bad("Sprigs can't be copied this way.")
        taken = {s.name for s in self.col.sources if s.kind != PART_KIND}
        name = (_get(data, "name", str, optional=True) or "").strip()
        if name and name in taken:
            raise ApiError("busy", f"There's already a source called {name!r}.")
        n = 2
        while not name:
            name, n = (f"{source.name} {n}" if f"{source.name} {n}" not in taken else ""), n + 1
        copy = source.model_copy(deep=True, update={"id": self.col.new_id(source.kind), "name": name})
        for flt in copy.filters:
            flt.id = secrets.token_hex(4)
        self.col.sources.append(copy)
        for scene, item in placed:
            scene.items.append(item.model_copy(deep=True, update={"source": copy.id, "locked": False}))
        self.host.commit("items")
        return {"item": copy.id, "name": copy.name, "shapes": sorted({scene.canvas for scene, _ in placed})}

    @action("item.order", "Bring something forward or send it back.", item="name or id", bloom="?bloom it's in",
            sprig="?sprig it's in", shape="?both (default), horizontal or vertical",
            to="top, bottom, up (one step) or down (one step)")
    def item_order(self, data):
        holder, layouts = self._holder(data)
        wanted = _get(data, "item", str)
        to = _get(data, "to", str)
        if to not in ("top", "bottom", "up", "down"):
            raise _bad("to must be top, bottom, up or down.")
        moved = False
        for layout in layouts:
            for item in [i for i in layout.items if i in self._matching_items([layout], wanted)]:
                at = next(n for n, i in enumerate(layout.items) if i is item)
                layout.items.pop(at)
                new = {"top": len(layout.items), "bottom": 0, "up": at + 1, "down": max(0, at - 1)}[to]
                layout.items.insert(min(new, len(layout.items)), item)  # index 0 draws first (bottom)
                moved = True
        if not moved:
            raise _missing(f"{holder.name!r} has no item {wanted!r}.", "item", wanted)
        self.host.commit("items")

    @action("sprigs.list", "Every sprig, its Live states and the state showing.")
    def sprigs_list(self, data):
        return {"sprigs": [{"id": p.id, "name": p.name, "state": p.state, "states": [s.name for s in p.states],
                            "shapes": [s.canvas for s in self.col.shapes(p)]} for p in self.col.parts()]}

    @action("sprig.remove", "Remove a sprig in every shape, and take it out of every bloom. Sources only it used "
            "go too, except sounds (mics, desktop and app sounds), which always stay.", sprig="name or id")
    def sprig_remove(self, data):
        part = self._sprig(data)
        removed = self.col.remove_sprig(part)
        self.host.commit("scenes")
        return {"removed": part.name, "sources_removed": removed}

    @action("sprig.state", "Switch a sprig to one of its Live states (null: as built).", sprig="name or id",
            state="state name or null")
    def sprig_state(self, data):
        part = self._sprig(data)
        name = data.get("state") or None
        if name is not None and all(s.name != name for s in part.states):
            raise _missing(f"{part.name!r} has no state {name!r}.", "state", name)
        self.col.set_state(part.id, name)
        self.host.commit("states")

    # Sounds ------------------------------------------------------------------------------------

    @action("sounds.list", "Every sound in the mixer with its volume and mute.")
    def sounds_list(self, data):
        return {"sounds": [self._sound_info(s) for s in self.col.sources if self._has_sound(s)]}

    def _set_sound(self, source: SourceConfig, changes: dict[str, Any]):
        before = source.settings
        source.settings = {**source.settings, **changes}
        held = getattr(self.host, "sound_changes", None)  # in a batch: logged when it's applied, not before
        if held is not None:
            held.append((source, before))
        else:
            sound_changed(source, before)
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

    _OWNER = {"source": "?name or id", "sprig": "?a sprig instead: its filters where it's placed",
              "in": "?with sprig: only where it's placed in this bloom or sprig (both canvases)"}

    def _filter_owners(self, data) -> list[SourceConfig]:
        """What a filter request is about: a source, or a placed sprig. A sprig has a placement in each bloom and on
        each canvas, each with filters of its own: {"sprig"} is all of them, with "in" the ones in that bloom or
        sprig (every canvas of it). "source" may also be one placement's id."""
        if data.get("sprig") is None:
            wanted = data.get("source")
            placed = next((s for s in self.col.sources if s.kind == PART_KIND and s.id == wanted), None)
            return [placed] if placed is not None else [self._source(data, "source")]
        sprig = self._sprig(data)
        placed = [s for s in self.col.sources if self.col.part_of(s) is sprig]
        holder = data.get("in")
        if holder is not None:
            scene = next((s for s in self.col.scenes if s.id == holder), None) or \
                next((s for s in self.col.scenes if s.name == holder and s.shape_of is None), None)
            if scene is None:
                raise _missing(f"No bloom or sprig called {holder!r}.", "in", holder)
            there = {i.source for s in self.col.scenes
                     if s.id == scene.id or s.shape_of == scene.id or s.linked_to == scene.id for i in s.items}
            placed = [s for s in placed if s.id in there]
        if not placed:
            raise ApiError("not_allowed", f"{sprig.name!r} isn't placed {'there' if holder is not None else 'in any bloom yet'}.")
        return placed

    def _each_owner(self, data, one):
        """Run one(source) for each thing the request is about and save once. A placement the filter isn't on is
        passed over; when none has it, that's the error. Answers what the first one answered."""
        answers, missed = [], None
        for source in self._filter_owners(data):
            try:
                answers.append(one(source))
            except ApiError as error:
                if error.code not in ("not_found", "not_allowed"):
                    raise
                missed = missed or error
                continue
            self.col.share_filters(source)  # a placed sprig's other canvases follow, unless it keeps its own
        if not answers:
            raise missed
        self.host.commit("filters")
        return answers[0]

    @action("filters.list", "A source's or a placed sprig's filters, top to bottom.", **_OWNER)
    def filters_list(self, data):
        source = self._filter_owners(data)[0]
        out: dict[str, Any] = {"filters": [self._filter_info(f) for f in source.filters]}
        if self.col.filter_twins(source):  # a placed sprig with a placement per canvas
            out["same_on_both_canvases"] = self.col.shares_filters(source)
        return out

    @action("filters.share", "A placed sprig: the same filters on both canvases (the usual), or its own on each.",
            on="true or false", **_OWNER)
    def filters_share(self, data):
        on = _get(data, "on", bool)
        owners = [s for s in self._filter_owners(data) if self.col.filter_twins(s)]
        if not owners:
            raise ApiError("not_allowed", "That has one set of filters already: it's the same on every canvas.")
        done: set[str] = set()
        for source in owners:
            if source.id not in done:  # the first of a pair gives the other its filters
                self.col.set_shares_filters(source, on)
                done |= {source.id, *(t.id for t in self.col.filter_twins(source))}
        self.host.commit("filters")
        return {"source": owners[0].name, "same_on_both_canvases": on}

    @action("filter.set", "Turn a filter on or off and/or change some of its settings (others stay).",
            filter="filter name, id or kind", on="?true or false", settings="?{key: value}", **_OWNER)
    def filter_set(self, data):
        on = _get(data, "on", bool, optional=True)
        settings = _get(data, "settings", dict, optional=True)
        if on is None and settings is None:
            raise _bad("Give on, settings or both.")
        return self._each_owner(data, lambda source: self._set_filter(source, data, on, settings))

    def _set_filter(self, source, data, on, settings):
        from sprigcast.core.filters import filter_kind, switch

        found = self._filter(source, data)
        if settings:
            kind = filter_kind(found.kind)
            if kind is not None:
                unknown = set(settings) - {f.key for f in kind.fields}
                if unknown:
                    raise _bad(f"{found.kind} has no setting {', '.join(sorted(unknown))}.")
            found.settings = {**found.settings, **settings}
        if on is not None:
            switch(found, on)
        return self._filter_info(found)

    @action("filter.toggle", "Turn a filter on if off, off if on.", filter="name, id or kind", **_OWNER)
    def filter_toggle(self, data):
        on: list[bool] = []  # the first one decides, so a sprig's placements end up the same

        from sprigcast.core.filters import active

        def one(source):
            found = self._filter(source, data)
            on.append(not active(found))  # a Freeze whose time ran out freezes again
            return self._set_filter(source, data, on[0], None)

        return self._each_owner(data, one)

    @action("filter.add", "Add a filter to a source or a placed sprig.", kind="kind from filters.kinds",
            name="?its name", settings="?{key: value}", on="?true (default) or false", **_OWNER)
    def filter_add(self, data):
        from sprigcast.core.filters import filter_kind

        kind = filter_kind(_get(data, "kind", str))
        if kind is None:
            raise _missing("No such filter kind (filters.kinds lists them).", "kind", data.get("kind"))
        name = _get(data, "name", str, optional=True, default=kind.label)
        settings = {**kind.defaults, **_get(data, "settings", dict, optional=True, default={})}
        enabled = _get(data, "on", bool, optional=True, default=True)

        def one(source):
            if kind.media == "video" and any(f.kind == kind.kind for f in source.filters):
                raise ApiError("not_allowed", f"{source.name!r} already has a {kind.label} filter.")
            taken = {f.id for s in self.col.sources for f in s.filters}
            n = 1
            while f"filter-{n}" in taken:
                n += 1
            new = Filter(id=f"filter-{n}", kind=kind.kind, name=name, settings=dict(settings), enabled=enabled)
            source.filters.append(new)
            return self._filter_info(new)

        return self._each_owner(data, one)

    @action("filter.remove", "Remove a filter from a source or a placed sprig.", filter="name, id or kind", **_OWNER)
    def filter_remove(self, data):
        def one(source):
            found = self._filter(source, data)
            source.filters = [f for f in source.filters if f is not found]

        self._each_owner(data, one)

    @action("filter.move", "Move a filter to a new place in the list (0 = top).",
            filter="name, id or kind", index="new position", **_OWNER)
    def filter_move(self, data):
        index = int(_get(data, "index", float))

        def one(source):
            found = self._filter(source, data)
            rest = [f for f in source.filters if f is not found]
            rest.insert(max(0, min(len(rest), index)), found)
            source.filters = rest

        self._each_owner(data, one)

    # Sources -------------------------------------------------------------------------------------

    @action("sources.list", "Every source (cameras, text, browser pages, images, sounds...).")
    def sources_list(self, data):
        return {"sources": [{"id": s.id, "name": s.name, "kind": s.kind, "filters": len(s.filters)}
                            for s in self.col.sources if s.kind != PART_KIND]}

    @action("source.get", "A source's settings.", source="name or id")
    def source_get(self, data):
        source = self._source(data, "source")
        out = {"id": source.id, "name": source.name, "kind": source.kind, "settings": dict(source.settings)}
        now = getattr(self.host, "source_now", lambda _id: None)(source.id)
        out["loaded"] = now is not None  # its bloom has been on screen
        if now:
            out.update({k: now[k] for k in ("state", "size", "cameraSize", "asleep") if k in now})
        return out

    @action("source.set", "Change some of a source's settings: text, a browser page's url, an image file, a color...",
            source="name or id", settings="{key: value}")
    def source_set(self, data):
        source = self._source(data, "source")
        settings = _checked_settings(self.col, source.kind, _get(data, "settings", dict))
        before = source.settings
        source.settings = {**source.settings, **settings}
        sound_changed(source, before)
        self.host.commit("sources")
        return {"id": source.id, "settings": dict(source.settings)}

    @action("source.new", "Make a source (a browser page, text, image...). Put it in a bloom with item.add.",
            name="name", kind="browser, text, image, color, media...", settings="?{key: value}, e.g. url, width, height")
    def source_new(self, data):
        import sprigcast.sources  # noqa: F401  (registers the built-in kinds)
        from sprigcast.core.source import known_kinds, source_class

        name = _get(data, "name", str).strip()
        kind = _get(data, "kind", str)
        settings = _get(data, "settings", dict, optional=True, default={})
        if not name:
            raise _bad("name is empty.")
        if kind == PART_KIND or kind not in known_kinds():
            raise _bad(f"kind must be one of {', '.join(sorted(k for k in known_kinds() if k != PART_KIND))}.")
        if any(s.name == name for s in self.col.sources if s.kind != PART_KIND):
            raise ApiError("busy", f"There's already a source called {name!r}.")
        defaults = dict(getattr(source_class(kind), "defaults", None) or {})
        settings = _checked_settings(self.col, kind, settings)
        source = SourceConfig(id=self.col.new_id(kind), kind=kind, name=name, settings={**defaults, **settings})
        self.col.sources.append(source)
        self.host.commit("sources")
        return {"id": source.id, "name": source.name, "kind": kind, "settings": dict(source.settings)}

    @action("source.rotate", "Turn a source to an angle wherever it's shown (0: upright again).",
            source="name or id", degrees="clockwise, -3600 to 3600")
    def source_rotate(self, data):
        source = self._source(data, "source")
        degrees = _get(data, "degrees", float)
        if not -3600 <= degrees <= 3600:
            raise _bad("degrees must be between -3600 and 3600.")
        items = [i for scene in self.col.scenes for i in scene.items if i.source == source.id]
        if not items:
            raise ApiError("not_allowed", f"{source.name!r} isn't in any bloom or sprig yet.")
        for item in items:
            item.rotation = degrees % 360
        self.host.commit("items")
        return {"source": source.name, "degrees": degrees % 360, "items": len(items)}

    @action("source.rename", "Give a source a new name.", source="name or id", name="new name")
    def source_rename(self, data):
        source = self._source(data, "source")
        name = _get(data, "name", str).strip()
        if not name:
            raise _bad("name is empty.")
        if name != source.name:  # in the log: which name an app gave a sound, and when (StreamBridge 2026-11-20)
            log.info("control: an app renamed %s %r -> %r", source.id, source.name, name)
        source.name = name
        self.host.commit("items")

    @action("source.remove", "Remove a source, taking it out of every bloom and sprig it's in.", source="name or id")
    def source_remove(self, data):
        source = self._source(data, "source")
        places = 0
        for scene in self.col.scenes:
            before = len(scene.items)
            scene.items[:] = [i for i in scene.items if i.source != source.id]
            places += before - len(scene.items)
        self.col.sources = [s for s in self.col.sources
                            if s.id != source.id and s.settings.get("capture_of") != source.id]
        self.host.commit("scenes")
        return {"removed": source.name, "places": places}

    @action("source.refresh", "Load a web page source again, now.", source="name or id")
    def source_refresh(self, data):
        source = self._source(data, "source")
        if source.kind != "browser":
            raise _bad(f"{source.name} isn't a web page.")
        running = getattr(self.host, "source_instance", lambda _id: None)(source.id)
        if running is None or not getattr(running, "_open", False):
            raise ApiError("not_allowed", f"{source.name} isn't open right now (it's off screen), so there's "
                                          "nothing to load again. It loads fresh when it's shown.")
        running.reload()
        return {"refreshed": source.name}

    # Undo, and several changes as one --------------------------------------------------------------

    @action("edit.undo", "Undo the last change to the garden (as Ctrl+Z in SprigCast).")
    def edit_undo(self, data):
        undo = getattr(self.host, "undo", None)
        if undo is None:
            raise ApiError("not_allowed", "Undo isn't possible here.")
        return {"undone": bool(undo())}

    @action("edit.redo", "Put back the change that was just undone (as Ctrl+Y in SprigCast).")
    def edit_redo(self, data):
        redo = getattr(self.host, "redo", None)
        if redo is None:
            raise ApiError("not_allowed", "Redo isn't possible here.")
        return {"redone": bool(redo())}

    def run_batch(self, steps: list, dry_run: bool = False) -> dict[str, Any]:
        """Several layout changes as one: all of them or none, saved and sent once, as one step for undo.

        They run on a copy of the garden, so the real one is never half changed: only when every step worked is
        the copy put in its place (and then saved and drawn once). dry_run: the copy is thrown away; the answer
        says where things would end up (the layouts the steps name, with overlaps). Only BATCH_ACTIONS may be in
        it: anything that does more than change the garden (go live, take a screenshot) can't be taken back."""
        from sprigcast.core.scene import SceneCollection

        real_host, committed = self.host, []
        copy = SceneCollection.model_validate_json(self.col.to_text())

        class Quiet:  # the host, showing the copy, with saving and redrawing held back
            collection = copy
            sound_changes: list = []

            def __getattr__(self, name):
                if name == "commit_settings":
                    raise AttributeError(name)
                return getattr(real_host, name)

            def commit(self, what: str) -> None:
                committed.append(what)

        results: list[dict[str, Any]] = []
        layouts: list[dict[str, Any]] = []
        self.host = Quiet()
        try:
            for step in steps:
                name = step.get("do") if isinstance(step, dict) else None
                if not isinstance(name, str) or name not in BATCH_ACTIONS:
                    results.append({"ok": False, "error": {
                        "code": "bad_request",
                        "message": f"{name!r} can't be in one step. These can: {', '.join(sorted(BATCH_ACTIONS))}."}})
                    break
                result = answer(self, step)
                result.pop("id", None)
                results.append(result)
                if not result["ok"]:
                    break
            ok = len(results) == len(steps) and all(r["ok"] for r in results)
            if dry_run and ok:
                asked = []
                for step in steps:
                    key = next((k for k in ("bloom", "sprig") if step.get(k) is not None), None)
                    ref = (key, step[key]) if key else None
                    if ref is not None and ref not in asked:
                        asked.append(ref)
                for key, ref in asked:
                    try:
                        layouts.append(self.layout_get({key: ref, "overlaps": True}))
                    except ApiError:
                        continue  # removed by the steps themselves
        finally:
            self.host = real_host
        applied = ok and not dry_run and bool(committed)
        if applied:
            for source, before in Quiet.sound_changes:
                sound_changed(source, before)
            for name in type(self.col).model_fields:  # in place: everything holding the garden sees the change
                setattr(self.col, name, getattr(copy, name))
            fresh = getattr(real_host, "new_undo_step", None)
            if fresh is not None:
                fresh()  # never merged into the change before it: undo takes back exactly this batch
            replaced = getattr(real_host, "garden_replaced", None)
            if replaced is not None:
                replaced()
            real_host.commit("scenes" if "scenes" in committed else committed[-1])
        out: dict[str, Any] = {"steps": results, "applied": applied}
        if dry_run:
            out.update(dry_run=True, layouts=layouts)
        return out

    # Stream, record, clips -------------------------------------------------------------------------

    def _status(self, status: dict[str, Any] | None) -> dict[str, Any]:
        if status is None:
            return {"on": False}
        return {"on": True, "seconds": round(float(status.get("seconds", 0.0)), 1),
                **{k: v for k, v in status.items() if k != "seconds"}}

    @action("stream.status", "Live or not, for how long, dropped frames, and each place on its own.")
    def stream_status(self, data):
        out = self._status(self.host.stream_status())
        out["places"] = self._places_now()
        return out

    def _places_now(self) -> list[dict[str, Any]]:
        """Each place: id, name, on (sending now), reconnecting, seconds, skipped and frames."""
        from sprigcast.output.stream import SERVICES

        running = getattr(self.host, "stream_places", dict)() or {}
        out = []
        for o in self.col.outputs:
            if o.type != "stream":
                continue
            service = SERVICES.get(o.service or "custom", SERVICES["custom"])
            now = running.get(o.id)
            out.append({"id": o.id, "name": f"{service.label} ({o.canvas})", "service": o.service, "shape": o.canvas,
                        "on": now is not None, "reconnecting": bool(now and now.get("reconnecting")),
                        "seconds": round(float(now.get("seconds", 0.0)), 1) if now else 0.0,
                        "skipped": int(now.get("skipped", 0)) if now else 0,
                        "frames": int(now.get("frames", 0)) if now else 0})
        return out

    def _place(self, data) -> Any:
        wanted = _get(data, "place", str)
        output = next((o for o in self.col.outputs if o.type == "stream" and wanted in (o.id, o.service)), None)
        if output is None:
            raise _missing(f"No place {wanted!r}.", "place", wanted)
        return output

    @action("place.stop", "While live: stop sending to one place (the others keep going; the last one ends the "
            "stream).", place="id from places.list")
    def place_stop(self, data):
        output = self._place(data)
        if self.host.stream_status() is None:
            raise ApiError("not_allowed", "Not live.")
        problem = self.host.stop_place(output.id)
        if problem:
            raise ApiError("not_allowed", problem)
        return {"places": self._places_now()}

    @action("place.start", "While live: start sending to one more place (switched on for next time too).",
            place="id from places.list")
    def place_start(self, data):
        output = self._place(data)
        if self.host.stream_status() is None:
            raise ApiError("not_allowed", "Not live: use stream.start.")
        problem = self.host.start_place(output.id)
        if problem:
            raise ApiError("not_allowed", problem)
        if not output.enabled:
            output.enabled = True
            self.host.commit("places")
        return {"places": self._places_now()}

    def _start(self, start, data, what: str) -> dict[str, Any]:
        """stream.start / record.start: never waits on a pop-up in SprigCast. If the Stream doctor (or low space)
        would ask first, nothing starts and the answer is doctor_warnings with the list; force: true goes ahead."""
        force = _get(data, "force", bool, optional=True, default=False)
        out = start(force=True) if force else start()
        if isinstance(out, dict):
            warnings = list(out.get("warnings", []))
            if not out.get("started"):
                raise ApiError("doctor_warnings", f"Not {what}: " + " ".join(warnings) + " Send force: true to go ahead.",
                               warnings=warnings)
            return {"on": True, "warnings": warnings}
        if out:
            raise ApiError("not_allowed", out)
        return {"on": True}

    @action("stream.start", "Go live to every place that's switched on; the answer lists them (places). If the "
            "Stream doctor would warn first, nothing starts: doctor_warnings lists them. places: the ids you expect "
            "to go live; if the places switched on differ, nothing starts (places_differ lists them).",
            force="?true: go live past the doctor's warnings",
            places="?list of place ids from places.list: go live only if exactly these are switched on")
    def stream_start(self, data):
        if self.host.stream_status() is not None:
            raise ApiError("busy", "Already live.")
        going = [{"id": o.id, "service": o.service, "shape": o.canvas}
                 for o in self.col.outputs if o.type == "stream" and o.enabled]
        expected = _get(data, "places", list, optional=True)
        if expected is not None:
            if not all(isinstance(p, str) for p in expected):
                raise _bad("places must be a list of place ids.")
            if set(expected) != {p["id"] for p in going}:
                # A place switched on earlier and forgotten would go live with the rest (PC test 2026-11-18).
                raise ApiError("places_differ", "Not live: the places switched on aren't the ones asked for: "
                               + (", ".join(p["service"] or p["id"] for p in going) or "none") + ".",
                               places=going)
        out = self._start(self.host.start_stream, data, "live")
        out["places"] = going
        return out

    @action("stream.stop", "End the stream.")
    def stream_stop(self, data):
        if self.host.stream_status() is None:
            raise ApiError("not_allowed", "Not live.")
        self.host.stop_stream()
        return {"on": False}  # it ends a moment later (stream.changed follows); nothing more goes out

    @action("stream.toggle", "Go live, or end the stream (going live answers like stream.start).",
            force="?true: go live past the doctor's warnings")
    def stream_toggle(self, data):
        if self.host.stream_status() is None:
            return self.stream_start(data)
        self.host.stop_stream()
        return {"on": False}

    @action("places.list", "Where the stream goes (Twitch, YouTube...), each one's shape and if it's on.")
    def places_list(self, data):
        from sprigcast.output.stream import SERVICES

        out = []
        for o in self.col.outputs:
            if o.type != "stream":
                continue
            service = SERVICES.get(o.service or "custom", SERVICES["custom"])
            why = getattr(self.host, "place_problem", lambda _id: None)(o.id)
            place = {"id": o.id, "service": o.service, "shape": o.canvas, "on": o.enabled, "bitrateKbps": o.bitrate_kbps,
                     "name": service.label, "ready": why is None, "problem": why}
            canvas = next((c for c in self.col.canvases if c.id == o.canvas), None)
            size = o.size or ((canvas.width, canvas.height) if canvas is not None else None)
            place["size"] = list(size) if size else None  # what is sent: the place's own, else its shape's
            place["fps"] = o.fps or (canvas.fps if canvas is not None else None)
            if o.service == "twitch":
                place["enhanced"] = o.enhanced  # only where the service has it (Twitch)
            if SERVICES.get(o.service or "custom", SERVICES["custom"]).bandwidth_test:
                place["bandwidth_test"] = o.bandwidth_test  # only where the service has a test mode (Twitch)
            out.append(place)
        return {"places": out}

    @action("place.set", "Switch a place on or off for the next Go live, and/or its test mode (Twitch: the stream "
            "reaches Twitch but viewers never see it).", place="id from places.list", on="?true or false",
            bandwidth_test="?true or false (only places whose service has a test mode)",
            bitrateKbps="?500 to 50000: its bitrate for the next Go live")
    def place_set(self, data):
        from sprigcast.output.stream import SERVICES

        wanted = _get(data, "place", str)
        output = next((o for o in self.col.outputs if o.type == "stream" and wanted in (o.id, o.service)), None)
        if output is None:
            raise _missing(f"No place {wanted!r}.", "place", wanted)
        on = _get(data, "on", bool, optional=True)
        test = _get(data, "bandwidth_test", bool, optional=True)
        kbps = _get(data, "bitrateKbps", int, optional=True)
        if on is None and test is None and kbps is None:
            raise _bad("Give on, bandwidth_test and/or bitrateKbps.")
        if kbps is not None and not 500 <= kbps <= 50000:
            raise _bad("bitrateKbps must be 500 to 50000.")
        service = SERVICES.get(output.service or "custom", SERVICES["custom"])
        if test is not None and not service.bandwidth_test:
            raise _bad(f"{service.label} has no test mode.")
        if self.host.stream_status() is not None:
            raise ApiError("busy", "End the stream to change where it goes.")
        if on is not None:
            output.enabled = on
        if test is not None:
            output.bandwidth_test = test
        if kbps is not None:
            output.bitrate_kbps = kbps
        self.host.commit("places")

    @action("record.status", "Recording or not, for how long, the file, and whether it's paused.")
    def record_status(self, data):
        return self._status(self.host.record_status())

    @action("record.start", "Start recording. If the drive is nearly full, nothing starts: doctor_warnings says so.",
            force="?true: record anyway")
    def record_start(self, data):
        if self.host.record_status() is not None:
            raise ApiError("busy", "Already recording.")
        return self._start(self.host.start_record, data, "recording")

    @action("record.stop", "Stop recording.")
    def record_stop(self, data):
        status = self.host.record_status()
        if status is None:
            raise ApiError("not_allowed", "Not recording.")
        self.host.stop_record()
        return {"file": status.get("path", "")}

    @action("record.toggle", "Start or stop recording (starting answers like record.start).",
            force="?true: record anyway")
    def record_toggle(self, data):
        if self.host.record_status() is None:
            return self.record_start(data)
        self.host.stop_record()
        return {"on": False}

    def _pause(self, pause):
        problem = getattr(self.host, "pause_record", lambda _p: "Pausing isn't available here.")(pause)
        if problem:
            raise ApiError("not_allowed", problem)
        return {"paused": bool((self.host.record_status() or {}).get("paused"))}

    @action("record.pause", "Pause the recording. The file stays open; record.resume carries on in it.")
    def record_pause(self, data):
        return self._pause(True)

    @action("record.resume", "Carry on recording in the same file after record.pause.")
    def record_resume(self, data):
        return self._pause(False)

    @action("record.toggle_pause", "Pause, or carry on after a pause.")
    def record_toggle_pause(self, data):
        return self._pause(None)

    @action("record.chapter", "Add a chapter mark to the recording at this moment (editors and players list them).",
            name="?the chapter's name (default: Chapter 1, 2...)")
    def record_chapter(self, data):
        name = data.get("name", "") if isinstance(data, dict) else ""
        problem = getattr(self.host, "mark_chapter", lambda _n="": "Chapters aren't available here.")(str(name or ""))
        if problem:
            raise ApiError("not_allowed", problem)
        return {}

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

    @action("camera.status", "Whether SprigCast Camera (the virtual camera) is on.")
    def camera_status(self, data):
        return {"on": bool(getattr(self.host, "camera_on", lambda: False)())}

    @action("camera.set", "Turn SprigCast Camera on or off (no on: toggle).", on="?true or false")
    def camera_set(self, data):
        current = bool(getattr(self.host, "camera_on", lambda: False)())
        on = _get(data, "on", bool, optional=True)
        on = (not current) if on is None else on
        if on != current:
            problem = self.host.set_camera(on)
            if problem:
                raise ApiError("not_allowed", problem)
        return {"on": on}

    @action("clip.save", "Save the last seconds as a clip (gets clips ready first if they're off). Answers with "
                         "the file names at once; the clip.saved event says when they are written.")
    def clip_save(self, data):
        if not self.host.clips_ready():
            problem = self.host.set_clips(True)
            if problem:
                raise ApiError("not_allowed", problem)
            return {"saved": [], "started": True}
        self.last_clip = self.host.save_clip()
        return {"saved": self.last_clip, "started": False}

    # Media (video and sound files) ---------------------------------------------------------------

    def _media(self, data: dict[str, Any]) -> SourceConfig:
        source = self._source(data, "media")
        if source.kind != "media":
            raise ApiError("not_allowed", f"{source.name!r} isn't a video or sound file.")
        return source

    def _media_info(self, source: SourceConfig, info: dict[str, Any] | None = None) -> dict[str, Any]:
        info = info if info is not None else self.host.media_info(source.id)
        out = {"media": source.name, "id": source.id, "file": str(source.settings.get("path", "")),
               "loop": bool(source.settings.get("loop"))}
        if info:
            out.update({k: info[k] for k in ("state", "position", "duration", "video") if k in info})
        else:
            out["state"] = "unloaded"  # its bloom hasn't been on screen yet
        return out

    def _media_do(self, data: dict[str, Any], what: str, seconds: float | None = None) -> dict[str, Any]:
        source = self._media(data)
        info = self.host.media_control(source.id, what, seconds)
        if info is None:
            raise ApiError("not_allowed", f"{source.name!r} isn't loaded yet: put its bloom on screen first.")
        return self._media_info(source, info)

    @action("media.list", "Every video and sound file, with what it's doing (playing, paused, stopped, ended).")
    def media_list(self, data):
        return {"media": [self._media_info(s) for s in self.col.sources if s.kind == "media"]}

    @action("media.status", "What a video or sound file is doing, where it is and how long it is.", media="name or id")
    def media_status(self, data):
        return self._media_info(self._media(data))

    @action("media.play", "Play (from the start if it ended or was stopped).", media="name or id")
    def media_play(self, data):
        return self._media_do(data, "play")

    @action("media.pause", "Pause where it is.", media="name or id")
    def media_pause(self, data):
        return self._media_do(data, "pause")

    @action("media.toggle", "Pause if playing, else play.", media="name or id")
    def media_toggle(self, data):
        return self._media_do(data, "toggle")

    @action("media.restart", "Play from the start.", media="name or id")
    def media_restart(self, data):
        return self._media_do(data, "restart")

    @action("media.stop", "Stop and show nothing (play starts from the beginning).", media="name or id")
    def media_stop(self, data):
        return self._media_do(data, "stop")

    @action("media.seek", "Jump to a time.", media="name or id", seconds="?time from the start",
            change="?seconds to move (negative: back)")
    def media_seek(self, data):
        source = self._media(data)
        seconds = _get(data, "seconds", float, optional=True)
        change = _get(data, "change", float, optional=True)
        if (seconds is None) == (change is None):
            raise _bad("Give seconds or change.")
        if change is not None:
            current = self.host.media_info(source.id)
            if current is None:
                raise ApiError("not_allowed", f"{source.name!r} isn't loaded yet: put its bloom on screen first.")
            seconds = current["position"] + change
        return self._media_do(data, "seek", max(0.0, seconds))

    @action("media.loop", "Loop it or not (no on: toggle).", media="name or id", on="?true or false")
    def media_loop(self, data):
        source = self._media(data)
        on = _get(data, "on", bool, optional=True)
        source.settings = {**source.settings, "loop": (not source.settings.get("loop")) if on is None else on}
        self.host.commit("sources")
        return self._media_info(source)

    # Screenshots ---------------------------------------------------------------------------------

    @action("screenshot.take", "A picture of what's on air in each shape, or of one source or bloom.",
            shape="?both (default), horizontal or vertical",
            source="?just this source (a camera...), name or id", bloom="?just this bloom, name or id",
            width="?shrink to this width in pixels (keeps the shape)",
            save="?true (default): save PNGs in the recordings folder, under Screenshots",
            data="?true: send the picture back as base64 PNG (for bots and pages; large)")
    def screenshot_take(self, data):
        shape = self._shape(data)
        width = _get(data, "width", int, optional=True)
        if width is not None and not 16 <= width <= 7680:
            raise _bad("width must be between 16 and 7680.")
        save = _get(data, "save", bool, optional=True, default=True)
        as_data = _get(data, "data", bool, optional=True, default=False)
        if not save and not as_data:
            raise _bad("Nothing to do: give save or data.")
        if data.get("source") is not None or data.get("bloom") is not None:
            return self._one_shot(data, shape, width, save, as_data)
        shapes = self._shapes() if shape == "both" else [shape]
        return self._canvas_shots(shapes, width, save, as_data, lambda shots: {"shots": shots})

    def _canvas_shots(self, shapes, width, save, as_data, done):
        """Pictures of what's on air in these shapes: done(shots), now or Later."""
        def checked(shots):
            if not shots:
                raise ApiError("not_allowed", "Nothing is drawing right now, so there's nothing to picture.")
            return done(shots)

        later = self._later("screenshot_later")
        if later is not None:
            return _saving(later(shapes, width, save, as_data)).map(checked)
        try:
            shots = self.host.screenshot(shapes, width, save, as_data)
        except OSError as exc:
            raise ApiError("failed", f"Couldn't save the screenshot: {exc}") from exc
        return checked(shots)

    def _one_shot(self, data, shape: str, width: int | None, save: bool, as_data: bool):
        """screenshot.take of one source or bloom. A bloom on air is pictured as its canvas; one off air, as
        its sprig drawn on its own."""
        if data.get("source") is not None:
            source = self._source(data, "source")
            target, name = source.id, source.name
        else:
            bloom = self._bloom(data)
            family = self.col.linked_with(bloom.id)
            if data.get("shape") is None:
                # No shape given and the bloom named is one canvas's own (its id, or a name only it has): that
                # canvas. {"bloom": "bloom-8"}, the vertical Gameplay, answered with the horizontal one first
                # (PC, 2026-11-21). The main bloom, or a name its shapes share, is still every shape.
                wanted = data["bloom"]
                named = next((s for s in family if s.id == wanted), None) or \
                    next((s for s in family if s.name == wanted), None)
                if named is not None and named is not bloom:
                    shape = named.canvas
            # A shape this bloom has no layout for is said, not quietly left out: a bloom made for one canvas (its
            # vertical look being another bloom) answered "both" with one picture and no word why (PC, 2026-11-21).
            asked = self._shapes() if shape == "both" else [shape]
            skipped = [{"shape": c, "why": f"{bloom.name!r} has no {c} layout."}
                       for c in asked if all(s.canvas != c for s in family)]
            if shape != "both":
                family = [s for s in family if s.canvas == shape] or family
            def noted(out):
                return {**out, "skipped": skipped} if skipped else out

            on_air = [s.canvas for s in family if self.col.active_scenes.get(s.canvas) == s.id]
            if on_air and shape == "both":
                skipped += [{"shape": s.canvas, "why": f"{bloom.name!r} isn't on air there."}
                            for s in family if s.canvas not in on_air]
            if on_air:  # each shape it's on air in ("both" gave only the first)
                return self._canvas_shots(on_air if shape == "both" else on_air[:1], width, save, as_data,
                                          lambda shots: noted({"shots": [{**shot, "bloom": bloom.name} for shot in shots]}))
            off_air = getattr(self.host, "bloom_screenshot", None)
            if off_air is not None:  # drawn on its own canvas, nothing on air changes; its sources wake for it
                if self.host.stream_status() is not None or self.host.record_status() is not None:
                    raise ApiError("not_allowed", f"{bloom.name!r} isn't on air. A bloom off air can't be pictured "
                                                  "while live or recording: its sources would have to wake.")
                layouts = family if shape == "both" else family[:1]
                later = self._later("bloom_screenshots_later")
                if later is not None:  # every shape at once, on a worker
                    def collected(got, layouts=layouts, name=bloom.name):
                        shots = [{"shape": layout.canvas, **shot, "bloom": name, "on_air": False}
                                 for layout, shot in zip(layouts, got) if shot is not None]
                        if not shots:
                            raise ApiError("not_allowed", "Nothing is drawing right now, so there's nothing to picture.")
                        return noted({"shots": shots})

                    return _saving(later([layout.id for layout in layouts], bloom.name, width, save,
                                         as_data)).map(collected)
                shots = []
                for layout in layouts:
                    try:
                        shot = off_air(layout.id, bloom.name, width, save, as_data)
                    except OSError as exc:
                        raise ApiError("failed", f"Couldn't save the screenshot: {exc}") from exc
                    if shot is not None:
                        shots.append({"shape": layout.canvas, **shot, "bloom": bloom.name, "on_air": False})
                if not shots:
                    raise ApiError("not_allowed", "Nothing is drawing right now, so there's nothing to picture.")
                return noted({"shots": shots})
            placed = [i.source for i in family[0].items]
            if len(placed) != 1:
                raise ApiError("not_allowed", f"{bloom.name!r} isn't on air, so there's nothing to picture.")
            target, name = placed[0], bloom.name
        shoot = getattr(self.host, "source_screenshot", None)
        if shoot is None:
            raise ApiError("not_allowed", "Pictures of one source aren't possible here.")
        key = "source" if data.get("source") is not None else "bloom"

        def checked(shot):
            if shot is None:
                raise ApiError("not_allowed", f"{name!r} isn't drawing right now, so there's nothing to picture.")
            return {"shots": [{**shot, key: name}]}

        later = self._later("source_screenshot_later")
        if later is not None:
            return _saving(later(target, name, width, save, as_data)).map(checked)
        try:
            shot = shoot(target, name, width, save, as_data)
        except OSError as exc:
            raise ApiError("failed", f"Couldn't save the screenshot: {exc}") from exc
        return checked(shot)

    # Transitions ---------------------------------------------------------------------------------

    @action("transition.set", "Change the usual transition, its length and (Move) how it moves.",
            kind="?" + ", ".join(TRANSITION_KINDS), ms="?length in milliseconds, 50 to 20000",
            easing="?Move only: " + ", ".join(MOVE_EASINGS),
            unmatched="?Move only, what only one bloom shows: " + ", ".join(MOVE_UNMATCHED))
    def transition_set(self, data):
        kind = _get(data, "kind", str, optional=True)
        ms = _get(data, "ms", float, optional=True)
        easing = _get(data, "easing", str, optional=True)
        unmatched = _get(data, "unmatched", str, optional=True)
        if kind is None and ms is None and easing is None and unmatched is None:
            raise _bad("Give kind, ms, easing or unmatched.")
        if kind is not None and kind not in TRANSITION_KINDS:
            raise _bad(f"kind must be one of {', '.join(TRANSITION_KINDS)}.")
        if ms is not None and not 50 <= ms <= 20000:
            raise _bad("ms must be 50 to 20000.")
        if easing is not None and easing not in MOVE_EASINGS:
            raise _bad(f"easing must be one of {', '.join(MOVE_EASINGS)}.")
        if unmatched is not None and unmatched not in MOVE_UNMATCHED:
            raise _bad(f"unmatched must be one of {', '.join(MOVE_UNMATCHED)}.")
        t = self.col.transition
        if kind is not None:
            t.kind = kind  # type: ignore[assignment]
        if ms is not None:
            t.duration_ms = int(ms)
        if easing is not None:
            t.easing = easing  # type: ignore[assignment]
        if unmatched is not None:
            t.unmatched = unmatched  # type: ignore[assignment]
        self.host.commit("transition")
        return self._transition_info()

    def _transition_info(self) -> dict[str, Any]:
        t = self.col.transition
        return {"kind": t.kind, "ms": t.duration_ms, "easing": t.easing, "unmatched": t.unmatched}

    @action("transition.get", "The usual transition, its length and (Move) how it moves.")
    def transition_get(self, data):
        return {**self._transition_info(), "kinds": list(TRANSITION_KINDS), "easings": list(MOVE_EASINGS),
                "unmatched_choices": list(MOVE_UNMATCHED)}

    # Events ---------------------------------------------------------------------------------------

    def snapshot(self, full: bool = True) -> dict[str, Any]:
        """full=False: the settings and filters are taken from the last look instead of written out again (the
        half-second watch, when nothing was edited or asked: writing every source's settings out as JSON held
        Python's lock 13 ms at a time and slowed the frames, PC heavy run 2026-11-17)."""
        col = self.col
        last = getattr(self, "_last", None)
        if not full and last is not None:
            here = {src.id for src in col.sources}  # a source gone since: never named from the old look
            filters = {key: v for key, v in last["filters"].items() if key[0] in here}
            settings = {sid: v for sid, v in last["settings"].items() if sid in here}
        else:
            filters = {(s.id, f.id): (f.enabled, json.dumps(f.settings, sort_keys=True, default=str))
                       for s in col.sources for f in s.filters}
            settings = {s.id: json.dumps(s.settings, sort_keys=True, default=str) for s in col.sources}
        items = {}
        for scene in col.scenes:
            for i, item in enumerate(scene.items):
                items[(scene.id, i, item.source)] = item.visible
        if not full and last is not None and "places" in last:
            places = last["places"]  # a move ends with a full look (an edit), so the watch needn't write these out
        else:
            places = {(scene.id, i.source): (i.x, i.y, i.scale, i.sy, i.rotation, i.crop, i.bounds, i.bounds_size,
                                             i.bounds_align, i.align)
                      for scene in col.scenes for i in scene.items}
        return {
            "onAir": dict(col.active_scenes),
            "blooms": [(b.id, b.name) for b in self._blooms()],
            "items": items,
            "places": places,
            "sounds": {s.id: (bool(s.settings.get("muted", False)), float(s.settings.get("volume_db", 0.0)))
                       for s in col.sources if self._has_sound(s)},
            "filters": filters,
            "settings": settings,
            "sourceList": {s.id: (s.name, s.kind) for s in col.sources if s.kind != PART_KIND},
            "layouts": {scene.id: tuple(i.source for i in scene.items) for scene in col.scenes},
            "plugins": self._plugin_ids(),
            "stream": self.host.stream_status() is not None,
            "streamKey": getattr(self.host, "stream_key", lambda: 0)(),
            "record": self.host.record_status() is not None,
            "recordPaused": bool((self.host.record_status() or {}).get("paused")),
            "clips": self.host.clips_ready(),
            "camera": bool(getattr(self.host, "camera_on", lambda: False)()),
            "transition": (col.transition.kind, col.transition.duration_ms),
            "states": {p.id: p.state for p in col.parts()},
            "media": {s.id: self._media_state(s.id) for s in col.sources if s.kind == "media"},
            "shelf": self._shelf(),
        }

    def _media_state(self, source_id: str) -> str | None:
        recent = getattr(self.host, "media_state", None)
        if recent is not None:
            return recent(source_id)
        return (self.host.media_info(source_id) or {}).get("state")

    @staticmethod
    def _plugin_ids() -> tuple[str, ...]:
        """The plugins that are added now (their actions are registered), for plugins.changed. Each module looked
        at once, not once per action (67 ms on the PC with every built-in's actions)."""
        import sys

        ids = set()
        for module in {extra.module for extra in _PLUGIN_ACTIONS.values()}:
            if module.startswith("sprigcast_plugin_") and module not in sys.modules:
                continue
            head = getattr(sys.modules.get(module), "PLUGIN", None) or {}
            ids.add(str(head.get("id") or module.removeprefix("sprigcast_plugin_")))
        return tuple(sorted(ids))

    def _blooms_showing(self, part: Scene) -> list[str]:
        """The blooms whose own sprig this is (blooms hold sprigs only), so an item.changed inside it can say."""
        out = []
        for bloom in self._blooms():
            sprig = self.col.bloom_sprig(bloom)
            if sprig is not None and sprig.id == part.id:
                out.append(bloom.name)
        return out

    def changes(self, full: bool = True) -> list[tuple[str, dict[str, Any]]]:
        """(event name, data) for what changed since the last call. full=False: see snapshot."""
        now = self.snapshot(full)
        last, self._last = self._last, now
        if last is None:
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        scenes = {s.id: s for s in self.col.scenes}
        sources = {s.id: s for s in self.col.sources}
        families = self._families()

        def bloom_ref(scene_id: str) -> dict[str, Any]:
            scene = scenes.get(scene_id)
            main = self._main_shape(scene, families) if scene is not None and scene.kind == "show" else scene
            return {"bloom": main.name if main else scene_id, "bloomId": main.id if main else scene_id}

        shelf, old = now["shelf"], last["shelf"]
        for kind in ("garden", "path"):
            if shelf[kind + "s"] != old[kind + "s"]:
                out.append((f"{kind}s.changed", {kind + "s": shelf[kind + "s"]}))
            if shelf[kind] != old[kind]:
                out.append((f"{kind}.changed", {kind: shelf[kind]}))
        if now["blooms"] != last["blooms"]:
            out.append(("blooms.changed", {"blooms": [{"id": i, "name": n} for i, n in now["blooms"]]}))
        for shape, scene_id in now["onAir"].items():
            if last["onAir"].get(shape) != scene_id:
                out.append(("bloom.shown", {**bloom_ref(scene_id), "shape": shape}))
        owners: dict[str, list[str]] = {}
        for key, visible in now["items"].items():
            if key in last["items"] and last["items"][key] != visible:
                scene = scenes.get(key[0])
                if scene is not None and scene.kind == "part":
                    part = self.col.main_part(scene)
                    where = {"sprig": part.name, "blooms": owners.setdefault(part.id, self._blooms_showing(part))}
                else:
                    where = bloom_ref(key[0])
                out.append(("item.changed", {**where, "shape": scene.canvas if scene else None,
                                             "item": self._item_name(key[2]), "itemId": self._item_key(key[2]),
                                             "visible": visible}))
        if now["places"] is not last.get("places"):
            for key, place in now["places"].items():
                before = last.get("places", {}).get(key)
                scene = scenes.get(key[0])
                if before is None or before == place or scene is None:
                    continue
                if scene.kind == "part":
                    part = self.col.main_part(scene)
                    where = {"sprig": part.name, "blooms": owners.setdefault(part.id, self._blooms_showing(part))}
                else:
                    where = bloom_ref(key[0])
                x, y, sx, sy, rotation, crop, bounds, bounds_size, bounds_align, align = place
                out.append(("item.moved", {
                    **where, "shape": scene.canvas, "item": self._item_name(key[1]), "itemId": self._item_key(key[1]),
                    "x": x, "y": y, "scale_x": sx, "scale_y": sy, "rotation": rotation, "align": align,
                    "crop": dict(zip(("left", "top", "right", "bottom"), crop)),
                    "bounds": {"mode": bounds, "width": bounds_size[0], "height": bounds_size[1],
                               "align": bounds_align}}))
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
        out += self._effects_ended(last["filters"], now["filters"], sources)
        for sid, settings in now["settings"].items():
            if sid in last["settings"] and last["settings"][sid] != settings and sources[sid].kind != PART_KIND:
                out.append(("source.changed", {"source": sources[sid].name, "sourceId": sid,
                                               "settings": dict(sources[sid].settings)}))
        if "sourceList" in last and now["sourceList"] != last["sourceList"]:
            out.append(("sources.changed", {
                "sources": [{"id": sid, "name": n, "kind": k} for sid, (n, k) in now["sourceList"].items()],
                "added": [n for sid, (n, _) in now["sourceList"].items() if sid not in last["sourceList"]],
                "removed": [n for sid, (n, _) in last["sourceList"].items() if sid not in now["sourceList"]],
                "renamed": [{"from": last["sourceList"][sid][0], "to": n} for sid, (n, _) in now["sourceList"].items()
                            if sid in last["sourceList"] and last["sourceList"][sid][0] != n]}))
        for scene_id, held in now.get("layouts", {}).items():
            if "layouts" not in last:
                break
            before = last["layouts"].get(scene_id, ())  # a new bloom or sprig counts from empty
            scene = scenes.get(scene_id)
            if before == held or scene is None:
                continue
            if scene.kind == "part":
                part = self.col.main_part(scene)
                where = {"sprig": part.name, "blooms": owners.setdefault(part.id, self._blooms_showing(part))}
            else:
                where = bloom_ref(scene_id)
            out.append(("items.changed", {**where, "shape": scene.canvas,
                                          "items": [{"item": self._item_name(sid), "itemId": self._item_key(sid)}
                                                    for sid in reversed(held)]}))  # top first, as items.list
        if "plugins" in last and now["plugins"] != last["plugins"]:
            out.append(("plugins.changed", {"plugins": list(now["plugins"]),
                                            "added": sorted(set(now["plugins"]) - set(last["plugins"])),
                                            "removed": sorted(set(last["plugins"]) - set(now["plugins"]))}))
        if now["stream"] and last.get("stream") and now.get("streamKey") != last.get("streamKey"):
            out.append(("stream.changed", {"on": False}))  # ended and started again between two looks: both said
            out.append(("stream.changed", {"on": True}))
        for key, name in (("stream", "stream.changed"), ("record", "record.changed"), ("clips", "clips.changed"),
                          ("camera", "camera.changed")):
            if now.get(key) != last.get(key):
                out.append((name, {"on": now[key]}))
        if "recordPaused" in last and now["recordPaused"] != last["recordPaused"] and now["record"]:
            out.append(("record.paused", {"paused": now["recordPaused"]}))
        if now["transition"] != last["transition"]:
            out.append(("transition.changed", {"kind": now["transition"][0], "ms": now["transition"][1]}))
        for pid, state in now["states"].items():
            if pid in last["states"] and last["states"][pid] != state:
                out.append(("sprig.state", {"sprig": scenes[pid].name, "sprigId": pid, "state": state}))
        for sid, state in now["media"].items():
            if sid in last["media"] and last["media"][sid] != state and state is not None:
                out.append(("media.changed", self._media_info(sources[sid])))
        return out

    EFFECTS = {"pixelate": "hide.pixelate", "blackout": "hide.blackout", "barrel_roll": "roll.spin",
               "freeze": "freeze.start"}

    def _effects_ended(self, last: dict, now: dict, sources: dict) -> list[tuple[str, dict[str, Any]]]:
        """effect.ended: a Hide it, Barrel Roll or Freeze effect finished (its time was up, or hide.show), or was replaced
        (pixelate swapped for blackout, a new spin started over a running one)."""
        out = []
        turned_on = {sid for (sid, fid), (on, _) in now.items() if on and not (last.get((sid, fid)) or (False,))[0]}
        for (sid, fid), (was_on, was_settings) in last.items():
            if not was_on or sid not in sources:
                continue
            flt = next((f for f in sources[sid].filters if f.id == fid), None)
            kind = flt.kind if flt is not None else None
            if kind not in self.EFFECTS:
                continue
            on, settings = now.get((sid, fid), (False, was_settings))
            restarted = on and kind == "barrel_roll" and \
                json.loads(settings).get("run") != json.loads(was_settings).get("run")
            if on and not restarted:
                continue
            out.append(("effect.ended", {"source": sources[sid].name, "sourceId": sid, "effect": self.EFFECTS[kind],
                                         "reason": "replaced" if restarted or sid in turned_on else "ended"}))
        return out

    def meters(self, levels: dict[str, tuple[float, float]]) -> dict[str, Any]:
        return {"levels": {sid: {"peakDb": round(p, 1), "rmsDb": round(r, 1)} for sid, (p, r) in levels.items()}}


def event_group(name: str) -> str:
    """bloom.shown -> bloom; stream.changed -> output; meters -> meters."""
    head = name.split(".", 1)[0]
    if head in _PLUGIN_EVENTS:
        return "plugin"
    return {"blooms": "bloom", "stream": "output", "record": "output", "clips": "output",
            "places": "output", "camera": "output", "items": "item", "gardens": "garden", "paths": "path",
            "sources": "source", "plugins": "plugin", "effect": "filter"}.get(head, head)


# Actions that only look: answering one changes nothing, so it needn't start a full look for events
# (control_server soon). An app reconnecting 23 times a minute to ask record.status started a full look
# (every source's settings written out as JSON) each time (PC native run, 2026-11-17).
LOOKING_SUFFIXES = (".list", ".get", ".status", ".info", ".actions", ".stats", ".current", ".kinds")


def may_change(message: Any) -> bool:
    """Whether running this message could change anything (a welcome, an events choice or only looking can't)."""
    if not isinstance(message, dict):
        return False
    if "steps" in message:
        steps = message["steps"] if isinstance(message["steps"], list) else []
        return any(may_change(step) for step in steps if isinstance(step, dict) and "do" in step)
    name = message.get("do")
    if name is None:
        return False
    return not (isinstance(name, str) and name.endswith(LOOKING_SUFFIXES))


@dataclass
class NativeSession:
    """One app on the SprigCast API. receive() yields texts to send or a Close."""

    api: NativeApi
    password: str = ""
    keys: list[str] = field(default_factory=list)  # paired devices' keys: each works like the password
    pairing: Any = None  # control.pairing.Pairing: a code from Settings → Connections → Pair a device
    key: str = ""  # the paired key it signed in with (removing that device closes this connection)
    name: str = ""  # what it calls itself in its greeting ("name"): only ever for the log
    welcomed: bool = False
    events: set[str] = field(default_factory=lambda: set(EVENT_GROUPS))
    salt: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    challenge: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    may_change: bool = True  # whether the last message received could have changed something (may_change)

    def hello(self) -> str:
        hello: dict[str, Any] = {"app": "SprigCast", "version": __version__, "api": API_VERSION, "preview": PREVIEW,
                                 "auth": {"salt": self.salt, "challenge": self.challenge} if self.password else None}
        return json.dumps({"hello": hello})

    def wants(self, group: str) -> bool:
        return self.welcomed and group in self.events

    def event(self, name: str, data: dict[str, Any]) -> str | None:
        if name == ALWAYS_SENT and self.welcomed:
            pass  # every app hears that SprigCast is closing, whatever groups it asked for
        elif not self.wants(event_group(name)):
            return None
        return json.dumps({"event": name, **data}, default=str)

    def _set_events(self, wanted: Any) -> None:
        if wanted == "all":
            self.events = set(EVENT_GROUPS)
        elif wanted == "none":
            self.events = set()
        elif isinstance(wanted, list):
            self.events = {str(w) for w in wanted}

    def receive(self, text: str) -> Iterator[str | Close | Sleep]:
        try:
            message = json.loads(text)
        except ValueError:
            yield Close(4002, "Messages must be JSON.")
            return
        if not isinstance(message, dict):
            yield Close(4002, "Messages must be JSON objects.")
            return
        self.may_change = may_change(message)
        if not self.welcomed:
            if "pair" in message:
                device = self.pairing.redeem(message["pair"], message.get("name", "")) if self.pairing else None
                if device is None:
                    locked = self.pairing.locked_seconds() if self.pairing else 0
                    yield Close(4011, f"Too many wrong pairing codes: try again in {locked} s." if locked
                                else "Wrong or expired pairing code.")
                    return
                self.key = device.key
                log.info("control: paired %s (%s) with a code", device.name, device.id)
                yield json.dumps({"paired": {"key": device.key, "name": device.name}})
            elif self.password:
                answer = message.get("auth")
                right = False
                for secret in [self.password, *self.keys]:
                    if isinstance(answer, str) and secret and secrets.compare_digest(
                            answer, auth_string(secret, self.salt, self.challenge)):
                        right = True
                        self.key = "" if secret == self.password else secret
                if not right:
                    yield Close(4009, "Wrong password.")
                    return
            said = message.get("name")
            if isinstance(said, str):  # its own word for itself: kept short and printable, used only in the log
                self.name = "".join(c for c in said if c.isprintable()).strip()[:40]
            self._set_events(message.get("events", "all"))
            self.welcomed = True
            yield json.dumps({"welcome": {"api": API_VERSION, "events": sorted(self.events)}})
            if "do" not in message:
                return
        if "events" in message and "do" not in message:
            self._set_events(message["events"])
            yield json.dumps({"welcome": {"api": API_VERSION, "events": sorted(self.events)}})
            return
        if "steps" in message:
            yield from self._steps(message)
            return
        self.api.later_ok = True  # this app's own message: a screenshot is made off the window's thread
        try:
            out = self.answer(message)
        finally:
            self.api.later_ok = False
        if isinstance(out, Later):
            yield out.map(lambda done: json.dumps(done, default=str))
            return
        yield json.dumps(out, default=str)

    def _steps(self, message: dict[str, Any]) -> Iterator[str | Close | Sleep]:
        """{"steps": [{"do": ...}, {"wait": 5000}, ...]}: run them in order, waiting where asked.

        Answers once, at the end, with every step's answer. stopOnError (default true) stops at the
        first failure. Other messages keep working while a list waits."""
        steps = message.get("steps")
        problem = None
        dry_run, one_step = message.get("dry_run") is True, message.get("one_step") is True
        if (dry_run or one_step) and isinstance(steps, list) and steps and len(steps) <= MAX_STEPS:
            if any(not isinstance(step, dict) or "wait" in step for step in steps):
                problem = "Steps taken as one can't wait: only actions."
            else:
                result = self.api.run_batch(steps, dry_run)
                ok = all(r["ok"] for r in result["steps"]) and len(result["steps"]) == len(steps)
                yield json.dumps({"id": message.get("id"), "ok": ok, "result": result}, default=str)
                return
            yield json.dumps({"id": message.get("id"), "ok": False, "error": {"code": "bad_request", "message": problem}})
            return
        if not isinstance(steps, list) or not steps:
            problem = "steps must be a list of actions and waits."
        elif len(steps) > MAX_STEPS:
            problem = f"At most {MAX_STEPS} steps."
        elif sum(float(s.get("wait", 0) or 0) for s in steps if isinstance(s, dict)
                 and isinstance(s.get("wait", 0), (int, float))) > MAX_WAIT_MS:
            problem = f"The waits add up to more than {MAX_WAIT_MS // 60000} minutes."
        if problem:
            yield json.dumps({"id": message.get("id"), "ok": False, "error": {"code": "bad_request", "message": problem}})
            return
        stop = message.get("stopOnError", True) is not False
        results: list[dict[str, Any]] = []
        for step in steps:
            if isinstance(step, dict) and "wait" in step and "do" not in step:
                wait = step["wait"]
                if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0:
                    results.append({"ok": False, "error": {"code": "bad_request", "message": "wait is milliseconds."}})
                    if stop:
                        break
                    continue
                yield Sleep(wait / 1000)
                results.append({"ok": True, "waited": wait})
                continue
            result = answer(self.api, step if isinstance(step, dict) else {})
            result.pop("id", None)
            results.append(result)
            if stop and not result["ok"]:
                break
        yield json.dumps({"id": message.get("id"), "ok": all(r["ok"] for r in results) and len(results) == len(steps),
                          "result": {"steps": results}}, default=str)

    def answer(self, message: dict[str, Any]) -> dict[str, Any]:
        return answer(self.api, message)


def answer(api: NativeApi, message: dict[str, Any]) -> dict[str, Any]:
    """Run one {"do": ..., fields} message: {"id", "ok", "result"} or {"id", "ok": false, "error"}."""
    out: dict[str, Any] = {"id": message.get("id")}
    name = message.get("do")
    try:
        if not isinstance(name, str):
            raise _bad("do is missing: say which action, like {\"do\": \"bloom.show\", \"bloom\": \"BRB\"}.")
        data = {k: v for k, v in message.items() if k not in ("do", "id")}
        result = api.run(name, data)
        if isinstance(result, Later):
            return _answer_later(out, result)
    except ApiError as exc:
        out.update(ok=False, error={"code": exc.code, "message": exc.message, **getattr(exc, "extra", {})})
        return out
    except Exception as exc:  # a bug must not drop the connection
        out.update(ok=False, error={"code": "failed", "message": f"{type(exc).__name__}: {exc}"})
        return out
    out["ok"] = True
    if result is not None:
        out["result"] = result
    return out


def _answer_later(out: dict[str, Any], result: Later) -> Later:
    """answer() for an action that answers Later: the same {"id", "ok", ...} once its work is done."""
    def then(future):
        done = dict(out)
        try:
            value = result.then(future)
        except ApiError as exc:
            done.update(ok=False, error={"code": exc.code, "message": exc.message, **getattr(exc, "extra", {})})
            return done
        except Exception as exc:  # noqa: BLE001  a bug must not drop the connection
            done.update(ok=False, error={"code": "failed", "message": f"{type(exc).__name__}: {exc}"})
            return done
        done["ok"] = True
        if value is not None:
            done["result"] = value
        return done

    return Later(result.job, then)


def commit_settings(host, sources, what: str = "sources") -> None:
    """Save and send these sources' new settings, nothing else changed (a plugin's own state, from a hotkey or an
    app): the light way when the host has it, else the whole commit."""
    light = getattr(host, "commit_settings", None)
    if light is not None:
        light(list(sources))
    else:
        host.commit(what)
