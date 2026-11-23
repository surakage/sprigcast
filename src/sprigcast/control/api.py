"""What each obs-websocket request does in SprigCast, and which events to send when things change.

Words: OBS "scenes" are SprigCast blooms (shows) and sprigs (parts); OBS "inputs" are sources
(cameras, mics, browser pages...). Apps name things by name, so:

- GetSceneList lists the blooms on the first canvas (horizontal) by their own names, then blooms
  on other canvases that aren't linked to one of those, then sprigs. A name used twice gets the
  canvas or "sprig" added in brackets, e.g. "Chatting (vertical)". sceneUuid is SprigCast's id.
- Switching to a bloom also switches the blooms linked to it on other canvases, as in Live.
- Showing or hiding something in a bloom does the same in its linked twin on the other canvas
  (and in a sprig's other shape), so both shapes stay alike. Moving it does not.
- Sprigs can't go on air by themselves, but their insides can be shown and hidden like a scene's.

SprigCast's own extras (clips, per-canvas program, sprig states) are
vendor requests: CallVendorRequest with vendorName "sprigcast" (see VENDOR_REQUESTS).

Nothing here knows about Qt: ControlApi talks to the app through a Host (see app/control_server.py).
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from typing import Any, Protocol

from sprigcast import __version__
from sprigcast.control.protocol import OBS_WEBSOCKET_VERSION, RPC_VERSION, RequestError, Status, Sub
from sprigcast.control.places import OLD_PREFIX, find_place, place_names, place_state
from sprigcast.control.who import sound_changed
from sprigcast.core.scene import PART_KIND, TRANSITION_KINDS, Scene, SceneCollection, SceneItem, SourceConfig

log = logging.getLogger("sprigcast.control")

VENDOR = "sprigcast"
OBS_VERSION = "31.0.0"  # what we tell apps that check OBS's version; sprigcastVersion says what we are
SOUND_KINDS = ("audio_input", "audio_output", "app_audio")

# SprigCast source kind -> the OBS input kind apps expect for the same thing.
INPUT_KINDS = {
    "audio_input": "wasapi_input_capture",
    "audio_output": "wasapi_output_capture",
    "app_audio": "wasapi_process_output_capture",
    "camera": "dshow_input",
    "display_capture": "monitor_capture",
    "window_capture": "window_capture",
    "image": "image_source",
    "color": "color_source_v3",
    "text": "text_gdiplus_v3",
    "browser": "browser_source",
}
STREAM_FLAGS = {"OBS_OUTPUT_AUDIO": True, "OBS_OUTPUT_VIDEO": True, "OBS_OUTPUT_ENCODED": True,
                "OBS_OUTPUT_SERVICE": True}
TRANSITION_NAMES = {kind: kind.capitalize() for kind in TRANSITION_KINDS}  # "fade" -> "Fade"
OBS_TRANSITION_KINDS = {"cut": "cut_transition", "fade": "fade_transition", "slide": "slide_transition",
                        "swipe": "swipe_transition", "wipe": "wipe_transition", "stinger": "obs_stinger_transition",
                        "move": "move_transition"}


# OBS setting names -> SprigCast's, per source kind (anything else passes through as is).
OBS_SETTING_NAMES = {
    "image": {"file": "path"},
    "media": {"local_file": "path", "looping": "loop", "restart_on_activate": "restart_on_show",
              "close_when_inactive": "clear_on_end", "speed_percent": "speed", "hw_decode": "hw_decode"},
    "browser": {"local_file": "file", "is_local_file": None, "reroute_audio": None, "css": "css",
                "restart_when_active": "reload_on_air"},
    "text": {"text": "text", "file": "file", "read_from_file": "read_file"},
}
PICTURE_REUSE_S = 1.0
KEEP_ON_REPLACE = ("volume_db", "muted", "balance", "monitor", "sync_ms")  # mixer settings stay
MEDIA_STATES = {"playing": "OBS_MEDIA_STATE_PLAYING", "paused": "OBS_MEDIA_STATE_PAUSED",
                "stopped": "OBS_MEDIA_STATE_STOPPED", "ended": "OBS_MEDIA_STATE_ENDED"}
MEDIA_ACTIONS = {"OBS_WEBSOCKET_MEDIA_INPUT_ACTION_NONE": "", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY": "play",
                 "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE": "pause", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP": "stop",
                 "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART": "restart",
                 "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_NEXT": "restart", "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PREVIOUS": "restart"}


def settings_from_obs(kind: str, settings: dict[str, Any]) -> dict[str, Any]:
    """An OBS app's inputSettings in SprigCast's words (e.g. an image's "file" is our "path")."""
    names = OBS_SETTING_NAMES.get(kind, {})
    out: dict[str, Any] = {}
    for key, value in settings.items():
        ours = names.get(key, key)
        if ours is None:
            continue
        if kind == "media" and ours == "speed" and isinstance(value, (int, float)):
            value = value / 100
        if kind == "browser" and key == "shutdown":  # OBS: shut down when hidden; ours: keep running when hidden
            ours, value = "keep_running", not bool(value)
        out[ours] = value
    return out


def settings_for_obs(kind: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Our settings as an OBS app reads them (adds OBS's own names where they differ in meaning)."""
    out = dict(settings)
    if kind == "browser":
        out["shutdown"] = not bool(settings.get("keep_running", False))
    if kind == "text":  # OBS's switch for a text read from a file
        out["read_from_file"] = bool(str(settings.get("file", "") or "").strip()) and bool(settings.get("read_file", True))
    return out


def _ms(seconds: Any) -> int | None:
    return None if seconds is None else int(float(seconds) * 1000)


class Host(Protocol):
    """The app as the API sees it. Everything runs on the UI thread."""

    collection: SceneCollection

    def commit(self, what: str) -> None:
        """The collection was changed here: save it, send it to the renderer, refresh the screens.
        what: "program", "items", "sound", "states" or "transition" (which screens need a refresh)."""


    # Optional: commit_settings(sources) when only those sources' settings changed (see commit_settings below).

    def stream_status(self) -> dict[str, Any] | None: ...  # None when offline
    def record_status(self) -> dict[str, Any] | None: ...
    def clips_ready(self) -> bool: ...
    def start_stream(self) -> str | None: ...  # a reason it can't, or None (it starts a moment later)
    def stop_stream(self) -> None: ...
    def start_record(self) -> str | None: ...
    def stop_record(self) -> None: ...
    def set_clips(self, on: bool) -> str | None: ...
    def save_clip(self) -> list[str]: ...  # the files saved

    def source_sizes(self) -> dict[str, tuple[int, int]]: ...

    def screenshot(self, shapes: list[str], width: int | None, save: bool, as_data: bool) -> list[dict[str, Any]]: ...

    def media_info(self, source_id: str) -> dict[str, Any] | None: ...  # None until loaded

    def source_instance(self, source_id: str): ...  # the running source, for plugins

    def grab_picture(self, source_id: str | None, callback) -> None: ...  # see Engine.grab_source

    def media_control(self, source_id: str, what: str, seconds: float | None = None) -> dict[str, Any] | None: ...
    def stats(self) -> dict[str, Any]: ...
    def hotkeys(self) -> list[tuple[str, str]]: ...  # (action id, label)
    def trigger_hotkey(self, action: str) -> str | None: ...  # a note when it couldn't do anything


def _dedupe(name: str, used: set[str], tag: str) -> str:
    if name not in used:
        return name
    candidate, n = f"{name} ({tag})", 2
    while candidate in used:
        candidate, n = f"{name} ({tag} {n})", n + 1
    return candidate


def scene_names(col: SceneCollection) -> list[tuple[str, Scene]]:
    """Every scene an app can see, as (name it goes by, scene), in list order."""
    out: list[tuple[str, Scene]] = []
    used: set[str] = set()
    listed: set[str] = set()
    canvases = [c.id for c in col.canvases]
    for canvas_id in canvases:
        for scene in col.scenes_for(canvas_id):
            if scene.id in listed:
                continue
            name = _dedupe(scene.name, used, canvas_id)
            used.add(name)
            out.append((name, scene))
            listed.update(s.id for s in col.linked_with(scene.id))  # the twin answers to the same name
    for part in col.parts():
        name = _dedupe(part.name, used, "sprig")
        used.add(name)
        out.append((name, part))
    return out


def _require(data: dict[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    if key not in data:
        raise RequestError(Status.MISSING_REQUEST_FIELD, f"{key} is missing.")
    value = data[key]
    if not isinstance(value, kind) or (isinstance(value, bool) and kind in ((int, float), int, float)):
        raise RequestError(Status.INVALID_REQUEST_FIELD_TYPE, f"{key} has the wrong type.")
    return value


def _number(data: dict[str, Any], key: str) -> float:
    return float(_require(data, key, (int, float)))


def _db_to_mul(db: float) -> float:
    return 0.0 if db <= -100 else 10 ** (db / 20)


def _timecode(seconds: float) -> str:
    ms = int(seconds * 1000)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


class ControlApi:
    def __init__(self, host: Host):
        self.host = host
        self._item_ids: dict[tuple[str, str, int], int] = {}  # (scene id, source id, nth use) -> sceneItemId
        self._next_item_id: dict[str, int] = {}
        self._last: dict[str, Any] | None = None  # what changes() compared against last time
        self.requests: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = {
            name[4:]: getattr(self, name) for name in dir(self) if name.startswith("req_")
        }
        self.vendor_requests: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = {
            name[7:]: getattr(self, name) for name in dir(self) if name.startswith("vendor_")
        }
        self.custom_event: Callable[[dict[str, Any]], None] = lambda data: None  # set by the server
        self._pictures: dict[tuple[str, int | None], tuple[float, dict[str, Any]]] = {}  # see _picture

    @property
    def col(self) -> SceneCollection:
        return self.host.collection

    def handle(self, request_type: str, data: dict[str, Any]) -> dict[str, Any] | None:
        handler = self.requests.get(request_type)
        if handler is None:
            raise RequestError(Status.UNKNOWN_REQUEST_TYPE, f"SprigCast doesn't do {request_type} yet.")
        return handler(data)

    # Lookups -------------------------------------------------------------------------

    def _names(self) -> dict[str, str]:
        """scene id -> the name apps know it by (twins answer to their linked bloom's name)."""
        names = {}
        for name, scene in scene_names(self.col):
            names[scene.id] = name
            if scene.kind == "show":
                for twin in self.col.linked_with(scene.id):
                    names.setdefault(twin.id, name)
        return names

    def _scene_name(self, scene: Scene | None) -> str | None:
        return None if scene is None else self._names().get(scene.id, scene.name)

    def _scene(self, data: dict[str, Any], name_key: str = "sceneName", uuid_key: str = "sceneUuid") -> Scene:
        """The scene a request names, by uuid (SprigCast id) or by name."""
        uuid, name = data.get(uuid_key), data.get(name_key)
        if uuid is None and name is None:
            raise RequestError(Status.MISSING_REQUEST_FIELD, f"{name_key} or {uuid_key} is missing.")
        if uuid is not None:
            scene = next((s for s in self.col.scenes if s.id == uuid), None)
        else:
            if not isinstance(name, str):
                raise RequestError(Status.INVALID_REQUEST_FIELD_TYPE, f"{name_key} must be text.")
            scene = next((s for n, s in scene_names(self.col) if n == name), None)
            if scene is None:
                scene = next((s for s in self.col.scenes if s.name == name and s.shape_of is None), None)
        if scene is None:
            raise RequestError(Status.RESOURCE_NOT_FOUND, f"No bloom or sprig called {uuid or name!r}.")
        return self.col.main_part(scene)

    def _layout(self, data: dict[str, Any]) -> Scene:
        """The layout whose items a request means. Blooms hold one sprig named after them (an OBS scene imported
        becomes a bloom and its sprig), so a bloom holding just one sprig shows that sprig's items, in the bloom's
        shape: what was in the OBS scene answers to the scene's name, as apps made for OBS expect."""
        scene = self._scene(data)
        if scene.kind == "show" and scene.items:
            sources = {c.id: c for c in self.col.sources}
            parts = {getattr(self.col.part_of(sources[i.source]), "id", None) if i.source in sources else None
                     for i in scene.items}
            if len(parts) == 1 and None not in parts:  # just its one sprig (anything else: the bloom's own items)
                return self.col.shape_for(self.col.scene(parts.pop()), scene.canvas)
        return scene

    def _inputs(self) -> list[SourceConfig]:
        return [s for s in self.col.sources if s.kind != PART_KIND]

    def _input(self, data: dict[str, Any]) -> SourceConfig:
        uuid, name = data.get("inputUuid"), data.get("inputName")
        if uuid is None and name is None:
            raise RequestError(Status.MISSING_REQUEST_FIELD, "inputName or inputUuid is missing.")
        key, value = ("id", uuid) if uuid is not None else ("name", name)
        source = next((s for s in self._inputs() if getattr(s, key) == value), None)
        if source is None and uuid is not None and isinstance(uuid, str):
            alias = self.col.source_by_id(uuid)  # its id as it was spelled before, or is now
            source = next((s for s in self._inputs() if alias is not None and s.id == alias.id), None)
        if source is None and uuid is None and isinstance(name, str):
            shared = self.col.named_for_canvas(name)  # a copy it took the place of (Garden check → Share)
            source = shared[0] if shared is not None else None
        if source is None:
            raise RequestError(Status.RESOURCE_NOT_FOUND, f"No source called {value!r}.")
        return source

    def _sound(self, data: dict[str, Any]) -> SourceConfig:
        source = self._input(data)
        if not self._has_sound(source):
            raise RequestError(Status.INVALID_RESOURCE_STATE, f"{source.name!r} has no sound.")
        return source

    @staticmethod
    def _has_sound(source: SourceConfig) -> bool:
        if source.kind in SOUND_KINDS:
            return True
        if source.kind == "browser":
            from sprigcast.sources.browser import sound_to_mixer

            return sound_to_mixer(source.settings)
        return False

    def _input_kind(self, source: SourceConfig) -> str:
        return INPUT_KINDS.get(source.kind, source.kind)

    # Scene items: OBS gives each a number that stays the same while the app runs.

    def _item_id(self, scene: Scene, index: int) -> int:
        source = scene.items[index].source
        nth = sum(1 for item in scene.items[:index] if item.source == source)
        key = (scene.id, source, nth)
        if key not in self._item_ids:
            self._next_item_id[scene.id] = self._next_item_id.get(scene.id, 0) + 1
            self._item_ids[key] = self._next_item_id[scene.id]
        return self._item_ids[key]

    def _item(self, scene: Scene, data: dict[str, Any]) -> tuple[int, SceneItem]:
        wanted = _require(data, "sceneItemId", int)
        for index in range(len(scene.items)):
            if self._item_id(scene, index) == wanted:
                return index, scene.items[index]
        raise RequestError(Status.RESOURCE_NOT_FOUND, f"No item {wanted} in {scene.name!r}.")

    def _source_name(self, source_id: str) -> str:
        source = next((s for s in self.col.sources if s.id == source_id), None)
        if source is None:
            return source_id
        part = self.col.part_of(source)
        return self._scene_name(part) if part is not None else source.name

    def _twins(self, scene: Scene) -> list[Scene]:
        """The other shapes of the same bloom or sprig (they show and hide things together)."""
        if scene.kind == "part":
            return [s for s in self.col.shapes(scene) if s.id != scene.id]
        return [s for s in self.col.linked_with(scene.id) if s.id != scene.id]

    def _canvas_name(self, source_id: str, name: str) -> str | None:
        """The canvas a copy's old name means for this source (Garden check → Share), or None."""
        source = next((s for s in self.col.sources if s.id == source_id), None)
        return source.canvas_names.get(name) if source is not None else None

    def _apart_on(self, source_id: str, canvases: set[str]) -> bool:
        """A source that took the place of a copy on one of these canvases: each canvas's placement is shown and
        hidden on its own, as the two copies were."""
        source = next((s for s in self.col.sources if s.id == source_id), None)
        return source is not None and bool(canvases & set(source.canvas_names.values()))

    def _same_thing(self, a: str, b: str) -> bool:
        """True when two sources show the same thing (one sprig placed in two shapes has two placements)."""
        if a == b:
            return True
        sa = next((s for s in self.col.sources if s.id == a), None)
        sb = next((s for s in self.col.sources if s.id == b), None)
        if sa is None or sb is None or sa.kind != PART_KIND or sb.kind != PART_KIND:
            return False
        pa, pb = self.col.part_of(sa), self.col.part_of(sb)
        return pa is not None and pa is pb

    def _item_info(self, scene: Scene, index: int) -> dict[str, Any]:
        item = scene.items[index]
        source = next((s for s in self.col.sources if s.id == item.source), None)
        part = self.col.part_of(source) if source is not None else None
        info = {
            "sceneItemId": self._item_id(scene, index),
            "sceneItemIndex": index,
            "sourceName": self._source_name(item.source),
            "sourceUuid": part.id if part is not None else item.source,
            "sourceType": "OBS_SOURCE_TYPE_SCENE" if part is not None else "OBS_SOURCE_TYPE_INPUT",
            "inputKind": None if part is not None or source is None else self._input_kind(source),
            "isGroup": None if part is None else False,
            "sceneItemEnabled": item.visible,
            "sceneItemLocked": item.locked,
            "sceneItemBlendMode": "OBS_BLEND_NORMAL",
            "sceneItemTransform": self._transform(item),
        }
        return info

    def _transform(self, item: SceneItem) -> dict[str, Any]:
        sw, sh = self.host.source_sizes().get(item.source, (0, 0))
        w, h = item.drawn_size((sw, sh))
        x, y = item.pin((sw, sh)) if sw and sh else (item.x, item.y)
        sx, sy = item.picture_scale((sw, sh)) if sw and sh else (item.scale, item.sy)
        left, top, right, bottom = item.crop
        return {
            "positionX": x, "positionY": y, "rotation": item.rotation,
            "scaleX": sx, "scaleY": sy, "width": w, "height": h,
            "sourceWidth": sw, "sourceHeight": sh, "alignment": _obs_align(item.align),
            "boundsType": BOUNDS_TO_OBS[item.bounds if item.bounded else "off"],
            "boundsAlignment": _obs_align(item.bounds_align),
            "boundsWidth": float(item.bounds_size[0]), "boundsHeight": float(item.bounds_size[1]),
            "cropLeft": left, "cropTop": top, "cropRight": right, "cropBottom": bottom,
            "cropToBounds": item.bounded and item.bounds == "fill",
        }

    # General -------------------------------------------------------------------------

    def req_GetVersion(self, data):
        return {
            "obsVersion": OBS_VERSION, "obsWebSocketVersion": OBS_WEBSOCKET_VERSION, "rpcVersion": RPC_VERSION,
            "availableRequests": sorted(self.requests), "supportedImageFormats": [],
            "platform": "windows", "platformDescription": "SprigCast", "sprigcastVersion": __version__,
        }

    def req_GetStats(self, data):
        stats = {
            "cpuUsage": 0.0, "memoryUsage": 0.0, "availableDiskSpace": 0.0, "activeFps": 0.0,
            "averageFrameRenderTime": 0.0, "renderSkippedFrames": 0, "renderTotalFrames": 0,
            "outputSkippedFrames": 0, "outputTotalFrames": 0,
            "webSocketSessionIncomingMessages": 0, "webSocketSessionOutgoingMessages": 0,
        }
        stats.update(self.host.stats())
        return stats

    def req_BroadcastCustomEvent(self, data):
        self.custom_event(_require(data, "eventData", dict))

    def req_CallVendorRequest(self, data):
        vendor = _require(data, "vendorName", str)
        request = _require(data, "requestType", str)
        if vendor != VENDOR:
            raise RequestError(Status.RESOURCE_NOT_FOUND, f"No vendor called {vendor!r}; SprigCast's is {VENDOR!r}.")
        handler = self.vendor_requests.get(request)
        if handler is None:
            raise RequestError(Status.UNKNOWN_REQUEST_TYPE, f"SprigCast has no vendor request {request!r}.")
        inner = data.get("requestData") or {}
        if not isinstance(inner, dict):
            raise RequestError(Status.INVALID_REQUEST_FIELD_TYPE, "requestData must be an object.")
        return {"vendorName": VENDOR, "requestType": request, "responseData": handler(inner) or {}}

    def req_GetHotkeyList(self, data):
        return {"hotkeys": [action for action, _ in self.host.hotkeys()]}

    def req_TriggerHotkeyByName(self, data):
        name = _require(data, "hotkeyName", str)
        if name not in {action for action, _ in self.host.hotkeys()}:
            raise RequestError(Status.RESOURCE_NOT_FOUND, f"No hotkey action {name!r} (see GetHotkeyList).")
        self.host.trigger_hotkey(name)

    def req_Sleep(self, data):
        raise RequestError(Status.UNKNOWN_REQUEST_TYPE, "Sleep only works inside a request batch.")

    # Config: scene collections are gardens, profiles are paths (core/gardens.py) -----------------

    def _shelf(self) -> dict[str, Any]:
        lists = getattr(self.host, "shelf_lists", None)
        if lists is None:  # a host without gardens (tests, tools)
            return {"garden": "SprigCast", "gardens": ["SprigCast"], "path": "SprigCast", "paths": ["SprigCast"]}
        return lists()

    def _shelf_do(self, kind: str, op: str, name: str, new_name: str = "") -> None:
        run = getattr(self.host, "shelf_do", None)
        if run is None:
            raise RequestError(Status.RESOURCE_ACTION_NOT_SUPPORTED, "Not here.")
        problem = run(kind, op, name, new_name)
        if problem:
            code = Status.RESOURCE_NOT_FOUND if problem.startswith("No ") else \
                Status.RESOURCE_ALREADY_EXISTS if "already" in problem else \
                Status.OUTPUT_RUNNING if problem.startswith("Stop ") else Status.INVALID_RESOURCE_STATE
            raise RequestError(code, problem)

    def req_GetSceneCollectionList(self, data):
        shelf = self._shelf()
        return {"currentSceneCollectionName": shelf["garden"], "sceneCollections": shelf["gardens"]}

    def req_SetCurrentSceneCollection(self, data):
        self._shelf_do("garden", "switch", _require(data, "sceneCollectionName", str))

    def req_CreateSceneCollection(self, data):
        name = _require(data, "sceneCollectionName", str)
        self._shelf_do("garden", "new", name)
        self._shelf_do("garden", "switch", name)

    def req_GetProfileList(self, data):
        shelf = self._shelf()
        return {"currentProfileName": shelf["path"], "profiles": shelf["paths"]}

    def req_SetCurrentProfile(self, data):
        self._shelf_do("path", "switch", _require(data, "profileName", str))

    def req_CreateProfile(self, data):
        name = _require(data, "profileName", str)
        self._shelf_do("path", "new", name)
        self._shelf_do("path", "switch", name)

    def req_RemoveProfile(self, data):
        self._shelf_do("path", "remove", _require(data, "profileName", str))

    def req_GetVideoSettings(self, data):
        canvas = self.col.canvases[0]
        return {"fpsNumerator": canvas.fps, "fpsDenominator": 1, "baseWidth": canvas.width,
                "baseHeight": canvas.height, "outputWidth": canvas.width, "outputHeight": canvas.height}

    def req_GetRecordDirectory(self, data):
        return {"recordDirectory": self.host.stats().get("recordDirectory", "")}

    # Scenes --------------------------------------------------------------------------

    def req_GetSceneList(self, data):
        listed = scene_names(self.col)
        scenes = [{"sceneIndex": i, "sceneName": name, "sceneUuid": scene.id}
                  for i, (name, scene) in enumerate(reversed(listed))]  # OBS: index 0 is the bottom of the list
        program = self.col.active_scene(self.col.canvases[0].id)
        return {
            "currentProgramSceneName": self._scene_name(program),
            "currentProgramSceneUuid": program.id if program else None,
            "currentPreviewSceneName": None,  # no Studio mode, so no preview scene (as in OBS with it off)
            "currentPreviewSceneUuid": None,
            "scenes": scenes,
        }

    def req_GetGroupList(self, data):
        return {"groups": []}

    def req_CreateScene(self, data):
        """A new bloom, in every shape that's on (with its own sprig, where new inputs go)."""
        name = _require(data, "sceneName", str).strip()
        if not name:
            raise RequestError(Status.REQUEST_FIELD_EMPTY, "sceneName is empty.")
        if any(s.kind == "show" and s.name == name for s in self.col.scenes):
            raise RequestError(Status.RESOURCE_ALREADY_EXISTS, f"A bloom called {name!r} already exists.")
        first = self.col.canvases[0].id
        bloom = self.col.new_bloom(name, [c.id for c in self.col.canvases if c.enabled or c.id == first])
        self.host.commit("scenes")
        return {"sceneUuid": bloom.id}

    def req_RemoveScene(self, data):
        scene = self._scene(data)
        if scene.kind != "show":
            raise RequestError(Status.INVALID_RESOURCE_TYPE, f"{scene.name!r} is a sprig, not a bloom.")
        if all(len(self.col.scenes_for(s.canvas)) <= 1 for s in self.col.linked_with(scene.id)):
            raise RequestError(Status.NOT_ENOUGH_RESOURCES, "Each canvas needs at least one bloom.")
        self.col.remove_bloom(scene)
        self.host.commit("scenes")

    def req_GetCurrentProgramScene(self, data):
        scene = self.col.active_scene(self.col.canvases[0].id)
        if scene is None:  # only the vertical canvas has a bloom on air
            scene = next((self.col.active_scene(c.id) for c in self.col.canvases if self.col.active_scene(c.id)), None)
        name = self._scene_name(scene)
        uuid = scene.id if scene else None
        return {"sceneName": name, "sceneUuid": uuid, "currentProgramSceneName": name, "currentProgramSceneUuid": uuid}

    def req_SetCurrentProgramScene(self, data):
        scene = self._scene(data)
        if scene.kind != "show":
            raise RequestError(Status.INVALID_RESOURCE_TYPE, f"{scene.name!r} is a sprig: sprigs only show inside blooms.")
        self.col.set_active(scene.id)
        self.host.commit("program")

    # SprigCast has no Studio mode (Live can't edit, so there's nothing to line up). OBS clients
    # still get proper answers: it's always off, and the preview-scene requests say so.
    NO_STUDIO = "SprigCast has no Studio mode: switching a bloom puts it on air."

    def req_GetCurrentPreviewScene(self, data):
        raise RequestError(Status.STUDIO_MODE_NOT_ACTIVE, self.NO_STUDIO)

    def req_SetCurrentPreviewScene(self, data):
        raise RequestError(Status.STUDIO_MODE_NOT_ACTIVE, self.NO_STUDIO)

    def req_GetStudioModeEnabled(self, data):
        return {"studioModeEnabled": False}

    def req_SetStudioModeEnabled(self, data):
        if _require(data, "studioModeEnabled", bool):
            raise RequestError(Status.STUDIO_MODE_NOT_ACTIVE, self.NO_STUDIO)

    def req_TriggerStudioModeTransition(self, data):
        raise RequestError(Status.STUDIO_MODE_NOT_ACTIVE, self.NO_STUDIO)

    # Transitions ---------------------------------------------------------------------

    def _transition_info(self, kind: str) -> dict[str, Any]:
        return {"transitionName": TRANSITION_NAMES[kind], "transitionUuid": kind,
                "transitionKind": OBS_TRANSITION_KINDS.get(kind, kind), "transitionFixed": kind == "cut",
                "transitionConfigurable": True}

    def req_GetSceneTransitionList(self, data):
        current = self.col.transition.kind
        return {"currentSceneTransitionName": TRANSITION_NAMES[current], "currentSceneTransitionUuid": current,
                "currentSceneTransitionKind": OBS_TRANSITION_KINDS.get(current, current),
                "transitions": [self._transition_info(k) for k in TRANSITION_KINDS]}

    def req_GetTransitionKindList(self, data):
        return {"transitionKinds": [OBS_TRANSITION_KINDS.get(k, k) for k in TRANSITION_KINDS]}

    def req_GetCurrentSceneTransition(self, data):
        t = self.col.transition
        info = self._transition_info(t.kind)
        info.update({"transitionDuration": None if t.kind == "cut" else t.duration_ms, "transitionSettings": {}})
        return info

    def req_SetCurrentSceneTransition(self, data):
        name = _require(data, "transitionName", str)
        kind = next((k for k, label in TRANSITION_NAMES.items() if name.lower() in (k, label.lower())), None)
        if kind is None:
            raise RequestError(Status.RESOURCE_NOT_FOUND, f"No transition called {name!r}.")
        self.col.transition.kind = kind  # type: ignore[assignment]
        self.host.commit("transition")

    def req_SetCurrentSceneTransitionDuration(self, data):
        ms = _number(data, "transitionDuration")
        if not 50 <= ms <= 20000:
            raise RequestError(Status.REQUEST_FIELD_OUT_OF_RANGE, "transitionDuration must be 50 to 20000 ms.")
        self.col.transition.duration_ms = int(ms)
        self.host.commit("transition")

    # Inputs (sources) ----------------------------------------------------------------

    def _input_info(self, source: SourceConfig) -> dict[str, Any]:
        kind = self._input_kind(source)
        return {"inputName": source.name, "inputUuid": source.id, "inputKind": kind, "unversionedInputKind": kind}

    def req_GetInputList(self, data):
        wanted = data.get("inputKind")
        inputs = [self._input_info(s) for s in self._inputs()]
        return {"inputs": [i for i in inputs if wanted in (None, i["inputKind"])]}

    def req_GetInputKindList(self, data):
        return {"inputKinds": sorted({self._input_kind(s) for s in self._inputs()} | set(INPUT_KINDS.values()))}

    def req_GetSpecialInputs(self, data):
        def find(kind: str, n: int) -> str | None:
            found = [s.name for s in self._inputs() if s.kind == kind]
            return found[n] if n < len(found) else None

        return {"desktop1": find("audio_output", 0), "desktop2": find("audio_output", 1),
                "mic1": find("audio_input", 0), "mic2": find("audio_input", 1),
                "mic3": find("audio_input", 2), "mic4": find("audio_input", 3)}

    def req_GetInputSettings(self, data):
        source = self._input(data)
        return {"inputSettings": settings_for_obs(source.kind, source.settings), "inputKind": self._input_kind(source)}

    def req_GetInputMute(self, data):
        return {"inputMuted": bool(self._sound(data).settings.get("muted", False))}

    def req_SetInputMute(self, data):
        source = self._sound(data)
        self._set_sound(source, {"muted": bool(_require(data, "inputMuted", bool))})

    def req_ToggleInputMute(self, data):
        source = self._sound(data)
        muted = not bool(source.settings.get("muted", False))
        self._set_sound(source, {"muted": muted})
        return {"inputMuted": muted}

    def req_GetInputVolume(self, data):
        db = float(self._sound(data).settings.get("volume_db", 0.0))
        return {"inputVolumeDb": db, "inputVolumeMul": _db_to_mul(db)}

    def req_SetInputVolume(self, data):
        source = self._sound(data)
        if "inputVolumeDb" in data:
            db = _number(data, "inputVolumeDb")
            if not -100 <= db <= 26:
                raise RequestError(Status.REQUEST_FIELD_OUT_OF_RANGE, "inputVolumeDb must be -100 to 26.")
        elif "inputVolumeMul" in data:
            mul = _number(data, "inputVolumeMul")
            if not 0 <= mul <= 20:
                raise RequestError(Status.REQUEST_FIELD_OUT_OF_RANGE, "inputVolumeMul must be 0 to 20.")
            db = -100.0 if mul <= 1e-5 else max(-100.0, 20 * math.log10(mul))
        else:
            raise RequestError(Status.MISSING_REQUEST_FIELD, "inputVolumeDb or inputVolumeMul is missing.")
        self._set_sound(source, {"volume_db": round(db, 2)})

    def req_GetInputAudioBalance(self, data):
        balance = float(self._sound(data).settings.get("balance", 0.0) or 0.0)  # -1 left .. 1 right
        return {"inputAudioBalance": (balance + 1) / 2}

    def req_SetInputAudioBalance(self, data):
        source = self._sound(data)
        value = _number(data, "inputAudioBalance")
        if not 0 <= value <= 1:
            raise RequestError(Status.REQUEST_FIELD_OUT_OF_RANGE, "inputAudioBalance must be 0 to 1.")
        self._set_sound(source, {"balance": round(value * 2 - 1, 3)})

    def req_GetInputAudioSyncOffset(self, data):
        return {"inputAudioSyncOffset": int(self._sound(data).settings.get("delay_ms", 0) or 0)}

    def req_SetInputAudioSyncOffset(self, data):
        source = self._sound(data)
        ms = _number(data, "inputAudioSyncOffset")
        if not 0 <= ms <= 20000:
            raise RequestError(Status.REQUEST_FIELD_OUT_OF_RANGE, "SprigCast delays sound by 0 to 20000 ms.")
        self._set_sound(source, {"delay_ms": int(ms)})

    def req_GetInputAudioMonitorType(self, data):
        monitor = self._sound(data).settings.get("monitor", False)
        kind = {"only": "OBS_MONITORING_TYPE_MONITOR_ONLY", True: "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT"}.get(
            monitor, "OBS_MONITORING_TYPE_NONE")
        return {"monitorType": kind}

    def req_SetInputAudioMonitorType(self, data):
        source = self._sound(data)
        kind = _require(data, "monitorType", str)
        values = {"OBS_MONITORING_TYPE_NONE": False, "OBS_MONITORING_TYPE_MONITOR_ONLY": "only",
                  "OBS_MONITORING_TYPE_MONITOR_AND_OUTPUT": True}
        if kind not in values:
            raise RequestError(Status.INVALID_REQUEST_FIELD, f"Unknown monitorType {kind!r}.")
        self._set_sound(source, {"monitor": values[kind]})

    def _set_sound(self, source: SourceConfig, changes: dict[str, Any]) -> None:
        before = source.settings
        source.settings = {**source.settings, **changes}
        sound_changed(source, before)
        self.host.commit("sound")

    # Scene items ---------------------------------------------------------------------

    def _add_item(self, data: dict[str, Any], source: SourceConfig, visible: bool) -> int:
        """Put a source on top of a scene; answers its sceneItemId. A bloom's goes in its sprig, in every shape
        (blooms hold sprigs only), so it answers to the bloom's name like everything imported from OBS."""
        scene = self._scene(data)
        layout = self._layout(data)
        if scene.kind == "show" and layout is scene and scene.items:
            targets = list(self.col.linked_with(scene.id))  # an older bloom with its own items: add it there
        elif scene.kind == "show":
            targets = []
            for bloom in self.col.linked_with(scene.id):
                sprig = self.col.give_sprig(bloom)
                shape = self.col.shape_for(sprig, bloom.canvas)
                if shape.canvas != bloom.canvas:
                    shape = self.col.add_shape(sprig, bloom.canvas, self.col.new_id(f"{sprig.id}-{bloom.canvas}"))
                if all(shape.id != t.id for t in targets):
                    targets.append(shape)
        else:
            targets = [scene] + self._twins(scene)
        for target in targets:
            item = SceneItem(source=source.id, visible=visible)
            self.col.starting_place(item, source.kind, target)  # a plugin starts readable, as from the + menu
            target.items.append(item)  # last is on top
        self.host.commit("items")
        layout = self._layout(data)
        index = max(i for i, item in enumerate(layout.items) if item.source == source.id)
        return self._item_id(layout, index)

    def req_CreateInput(self, data):
        name = _require(data, "inputName", str).strip()
        obs_kind = _require(data, "inputKind", str)
        settings = data.get("inputSettings") or {}
        if not isinstance(settings, dict):
            raise RequestError(Status.INVALID_REQUEST_FIELD_TYPE, "inputSettings must be an object.")
        visible = data.get("sceneItemEnabled", True)
        if not isinstance(visible, bool):
            raise RequestError(Status.INVALID_REQUEST_FIELD_TYPE, "sceneItemEnabled must be true or false.")
        if not name:
            raise RequestError(Status.REQUEST_FIELD_EMPTY, "inputName is empty.")
        if any(s.name == name for s in self._inputs()):
            raise RequestError(Status.RESOURCE_ALREADY_EXISTS, f"A source called {name!r} already exists.")
        import sprigcast.sources  # noqa: F401  (registers the built-in kinds)
        from sprigcast.core.source import known_kinds, source_class

        kind = next((k for k, obs in INPUT_KINDS.items() if obs == obs_kind), obs_kind)
        if kind == PART_KIND or kind not in known_kinds():
            raise RequestError(Status.INVALID_INPUT_KIND, f"Unknown inputKind {obs_kind!r}.")
        if data.get("sceneName") is None and data.get("sceneUuid") is None:
            raise RequestError(Status.MISSING_REQUEST_FIELD, "sceneName or sceneUuid is missing.")
        self._scene(data)  # the scene exists before anything is made
        defaults = dict(getattr(source_class(kind), "defaults", None) or {})
        source = SourceConfig(id=self.col.new_source_id(kind), kind=kind, name=name,
                              settings={**defaults, **settings_from_obs(kind, settings)})
        self.col.sources.append(source)
        self.host.commit("sources")
        return {"inputUuid": source.id, "sceneItemId": self._add_item(data, source, visible)}

    def req_CreateSceneItem(self, data):
        uuid, name = data.get("sourceUuid"), data.get("sourceName")
        if uuid is None and name is None:
            raise RequestError(Status.MISSING_REQUEST_FIELD, "sourceName or sourceUuid is missing.")
        source = self._input({"inputUuid": uuid} if uuid is not None else {"inputName": name})
        visible = data.get("sceneItemEnabled", True)
        if not isinstance(visible, bool):
            raise RequestError(Status.INVALID_REQUEST_FIELD_TYPE, "sceneItemEnabled must be true or false.")
        return {"sceneItemId": self._add_item(data, source, visible)}

    def req_RemoveSceneItem(self, data):
        scene = self._layout(data)
        index, item = self._item(scene, data)
        nth = sum(1 for other in scene.items[:index] if other.source == item.source)
        del scene.items[index]
        for twin in self._twins(scene):
            same = [i for i, other in enumerate(twin.items) if self._same_thing(other.source, item.source)]
            if nth < len(same):
                del twin.items[same[nth]]
        self.host.commit("items")

    def req_GetSceneItemList(self, data):
        scene = self._layout(data)
        return {"sceneItems": [self._item_info(scene, i) for i in range(len(scene.items))]}

    def req_GetGroupSceneItemList(self, data):
        return self.req_GetSceneItemList(data)

    def req_GetSceneItemId(self, data):
        scene = self._layout(data)
        name = _require(data, "sourceName", str)
        offset = int(data.get("searchOffset", 0))
        found = [i for i in range(len(scene.items)) if self._source_name(scene.items[i].source) == name
                 or self._canvas_name(scene.items[i].source, name) == scene.canvas]
        if offset < -1:
            raise RequestError(Status.REQUEST_FIELD_OUT_OF_RANGE, "searchOffset must be -1 or more.")
        if not found or offset >= len(found):
            raise RequestError(Status.RESOURCE_NOT_FOUND, f"{scene.name!r} has no {name!r}.")
        return {"sceneItemId": self._item_id(scene, found[offset])}

    def req_GetSceneItemSource(self, data):
        scene = self._layout(data)
        index, item = self._item(scene, data)
        info = self._item_info(scene, index)
        return {"sourceName": info["sourceName"], "sourceUuid": info["sourceUuid"]}

    def req_GetSceneItemEnabled(self, data):
        return {"sceneItemEnabled": self._item(self._layout(data), data)[1].visible}

    def req_SetSceneItemEnabled(self, data):
        scene = self._layout(data)
        _, item = self._item(scene, data)
        on = bool(_require(data, "sceneItemEnabled", bool))
        item.visible = on
        for twin in self._twins(scene):
            if self._apart_on(item.source, {scene.canvas, twin.canvas}):
                continue  # it was a copy per canvas, shown and hidden on its own: still so
            for other in twin.items:
                if self._same_thing(other.source, item.source):
                    other.visible = on
        self.host.commit("items")

    def req_GetSceneItemLocked(self, data):
        return {"sceneItemLocked": self._item(self._layout(data), data)[1].locked}

    def req_SetSceneItemLocked(self, data):
        _, item = self._item(self._layout(data), data)
        item.locked = bool(_require(data, "sceneItemLocked", bool))
        self.host.commit("items")

    def req_GetSceneItemIndex(self, data):
        return {"sceneItemIndex": self._item(self._layout(data), data)[0]}

    def req_GetSceneItemTransform(self, data):
        return {"sceneItemTransform": self._transform(self._item(self._layout(data), data)[1])}

    def req_SetSceneItemTransform(self, data):
        _, item = self._item(self._layout(data), data)
        transform = _require(data, "sceneItemTransform", dict)
        size = self.host.source_sizes().get(item.source, (0, 0))
        known = bool(size[0] and size[1])
        if "alignment" in transform:
            align = _from_obs_align(int(_number(transform, "alignment")))
            if known:
                item.set_align(size, align)
            else:
                item.align = align
        if "boundsAlignment" in transform:
            item.bounds_align = _from_obs_align(int(_number(transform, "boundsAlignment")))
        if "boundsType" in transform:
            item.bounds = BOUNDS_FROM_OBS.get(str(transform["boundsType"]), "off")
        for key, n in (("boundsWidth", 0), ("boundsHeight", 1)):
            if key in transform:
                box = list(item.bounds_size)
                box[n] = max(0.0, float(_number(transform, key)))
                item.bounds_size = tuple(box)  # type: ignore[assignment]
        if "rotation" in transform:
            item.rotation = float(_number(transform, "rotation"))
        if "positionX" in transform or "positionY" in transform:
            x, y = item.pin(size) if known else (item.x, item.y)
            x = float(_number(transform, "positionX")) if "positionX" in transform else x
            y = float(_number(transform, "positionY")) if "positionY" in transform else y
            if known:
                item.set_pin(size, x, y)
            else:
                item.x, item.y = x, y
        if "scaleX" in transform:
            ratio = None if item.scale_y is None else item.scale_y / item.scale if item.scale else None
            item.scale = _number(transform, "scaleX")
            if ratio is not None and "scaleY" not in transform:
                item.scale_y = item.scale * ratio
        if "scaleY" in transform:
            sy = _number(transform, "scaleY")
            item.scale_y = None if math.isclose(sy, item.scale) else sy
        crop = list(item.crop)
        for n, key in enumerate(("cropLeft", "cropTop", "cropRight", "cropBottom")):
            if key in transform:
                crop[n] = max(0, int(_number(transform, key)))
        item.crop = tuple(crop)  # type: ignore[assignment]
        self.host.commit("items")

    # Outputs -------------------------------------------------------------------------

    @staticmethod
    def _output_status(status: dict[str, Any] | None) -> dict[str, Any]:
        status = status or {}
        seconds = float(status.get("seconds", 0.0))
        return {
            "outputActive": bool(status), "outputReconnecting": bool(status.get("reconnecting", False)),
            "outputTimecode": _timecode(seconds), "outputDuration": int(seconds * 1000),
            "outputCongestion": float(status.get("congestion", 0.0)), "outputBytes": int(status.get("bytes", 0)),
            "outputSkippedFrames": int(status.get("skipped", 0)), "outputTotalFrames": int(status.get("frames", 0)),
        }

    def req_GetStreamStatus(self, data):
        return self._output_status(self.host.stream_status())

    def _start(self, start, data: dict[str, Any], what: str) -> None:
        """StartStream / StartRecord never wait on a pop-up in SprigCast: if the Stream doctor (or low space) would
        ask first, nothing starts and the comment lists why. A "force": true field goes ahead (SprigCast's own)."""
        force = data.get("force") is True
        out = start(force=True) if force else start()
        if isinstance(out, dict):
            if not out.get("started"):
                raise RequestError(Status.OUTPUT_DISABLED, f"Not {what}: "
                                   + " ".join(out.get("warnings", [])))
        elif out:
            raise RequestError(Status.OUTPUT_DISABLED, out)

    def req_StartStream(self, data):
        if self.host.stream_status() is not None:
            raise RequestError(Status.OUTPUT_RUNNING, "Already live.")
        self._start(self.host.start_stream, data, "live")

    def req_StopStream(self, data):
        if self.host.stream_status() is None:
            raise RequestError(Status.OUTPUT_NOT_RUNNING, "Not live.")
        self.host.stop_stream()

    def req_ToggleStream(self, data):
        if self.host.stream_status() is None:
            self.req_StartStream(data)
            return {"outputActive": True}
        self.host.stop_stream()
        return {"outputActive": False}

    def req_GetRecordStatus(self, data):
        out = self._output_status(self.host.record_status())
        del out["outputReconnecting"], out["outputCongestion"], out["outputSkippedFrames"], out["outputTotalFrames"]
        out["outputPaused"] = bool((self.host.record_status() or {}).get("paused"))
        return out

    def req_StartRecord(self, data):
        if self.host.record_status() is not None:
            raise RequestError(Status.OUTPUT_RUNNING, "Already recording.")
        self._start(self.host.start_record, data, "recording")

    def req_StopRecord(self, data):
        status = self.host.record_status()
        if status is None:
            raise RequestError(Status.OUTPUT_NOT_RUNNING, "Not recording.")
        self.host.stop_record()
        return {"outputPath": status.get("path", "")}

    def req_ToggleRecord(self, data):
        if self.host.record_status() is None:
            self.req_StartRecord(data)
            return {"outputActive": True}
        self.host.stop_record()
        return {"outputActive": False}

    def _pause(self, pause):
        status = self.host.record_status()
        if status is None:
            raise RequestError(Status.OUTPUT_NOT_RUNNING, "Not recording.")
        if pause is not None and bool(status.get("paused")) == pause:
            raise RequestError(Status.OUTPUT_PAUSED if pause else Status.OUTPUT_NOT_PAUSED,
                               "Already paused." if pause else "Not paused.")
        problem = getattr(self.host, "pause_record", lambda _p: "Pausing isn't available here.")(pause)
        if problem:
            raise RequestError(Status.RESOURCE_ACTION_NOT_SUPPORTED, problem)

    def req_PauseRecord(self, data):
        self._pause(True)

    def req_ResumeRecord(self, data):
        self._pause(False)

    def req_ToggleRecordPause(self, data):
        self._pause(None)
        return {"outputPaused": bool((self.host.record_status() or {}).get("paused"))}

    def req_CreateRecordChapter(self, data):
        if self.host.record_status() is None:
            raise RequestError(Status.OUTPUT_NOT_RUNNING, "Not recording.")
        name = (data or {}).get("chapterName", "") if isinstance(data, dict) else ""
        problem = getattr(self.host, "mark_chapter", lambda _n="": "Chapters aren't available here.")(str(name or ""))
        if problem:
            raise RequestError(Status.RESOURCE_ACTION_NOT_SUPPORTED, problem)

    def _camera_on(self) -> bool:
        return bool(getattr(self.host, "camera_on", lambda: False)())

    def _set_camera(self, on: bool) -> None:
        if on == self._camera_on():
            raise RequestError(Status.OUTPUT_RUNNING if on else Status.OUTPUT_NOT_RUNNING,
                               "SprigCast Camera is already " + ("on." if on else "off."))
        problem = self.host.set_camera(on)
        if problem:
            raise RequestError(Status.INVALID_RESOURCE_STATE, problem)

    def req_GetVirtualCamStatus(self, data):
        return {"outputActive": self._camera_on()}

    def req_StartVirtualCam(self, data):
        self._set_camera(True)

    def req_StopVirtualCam(self, data):
        self._set_camera(False)

    def req_ToggleVirtualCam(self, data):
        on = not self._camera_on()
        self._set_camera(on)
        return {"outputActive": on}

    def req_GetReplayBufferStatus(self, data):
        return {"outputActive": self.host.clips_ready()}

    def req_StartReplayBuffer(self, data):
        if self.host.clips_ready():
            raise RequestError(Status.OUTPUT_RUNNING, "Clips are already ready.")
        problem = self.host.set_clips(True)
        if problem:
            raise RequestError(Status.OUTPUT_DISABLED, problem)

    def req_StopReplayBuffer(self, data):
        if not self.host.clips_ready():
            raise RequestError(Status.OUTPUT_NOT_RUNNING, "Clips aren't on.")
        self.host.set_clips(False)

    def req_ToggleReplayBuffer(self, data):
        on = not self.host.clips_ready()
        problem = self.host.set_clips(on)
        if problem:
            raise RequestError(Status.OUTPUT_DISABLED, problem)
        return {"outputActive": on}

    def req_SaveReplayBuffer(self, data):
        if not self.host.clips_ready():
            raise RequestError(Status.OUTPUT_NOT_RUNNING, "Clips aren't on: start the replay buffer first.")
        self._last_clip = self.host.save_clip()

    def req_GetLastReplayBufferReplay(self, data):
        paths = getattr(self, "_last_clip", None)
        if not paths:
            raise RequestError(Status.RESOURCE_NOT_FOUND, "No clip saved yet.")
        return {"savedReplayPath": paths[0]}

    def req_GetOutputList(self, data):
        outputs = [{"outputName": "simple_stream", "outputKind": "rtmp_output", "outputActive": self.host.stream_status() is not None},
                   {"outputName": "simple_file_output", "outputKind": "ffmpeg_muxer", "outputActive": self.host.record_status() is not None},
                   {"outputName": "Replay Buffer", "outputKind": "replay_buffer", "outputActive": self.host.clips_ready()}]
        outputs[0]["outputFlags"] = dict(STREAM_FLAGS)
        places, names = self._places(), place_names(self.col)
        for output in self.col.outputs:
            if output.type == "stream":
                # Its name, as a person reads it; requests take the name, the id, or the older sprigcast:<id>.
                outputs.append({"outputName": names[output.id], "outputKind": "rtmp_output",
                                "outputId": output.id, "outputAlias": OLD_PREFIX + output.id,
                                "outputActive": output.id in places,
                                "outputWidth": (output.size or (0, 0))[0], "outputHeight": (output.size or (0, 0))[1],
                                "outputFlags": dict(STREAM_FLAGS)})
        return {"outputs": outputs}

    def _places(self) -> dict[str, dict[str, Any]]:
        """Places being sent to now, by output id (hosts without per-place status: every switched-on place)."""
        places = getattr(self.host, "stream_places", None)
        if places is not None:
            return places() or {}
        status = self.host.stream_status()
        return {} if status is None else {o.id: status for o in self.col.outputs if o.type == "stream" and o.enabled}

    def _output(self, data: dict[str, Any]) -> str:
        """simple_stream, simple_file_output, Replay Buffer, or sprigcast:<id> for a place asked for by its name,
        its id or sprigcast:<id>."""
        name = _require(data, "outputName", str)
        if name in ("simple_stream", "simple_file_output", "Replay Buffer"):
            return name
        place = find_place(self.col, name)
        if place is not None:
            return OLD_PREFIX + place.id
        raise RequestError(Status.RESOURCE_NOT_FOUND, f"No output called {name!r}.")

    def req_GetOutputStatus(self, data):
        name = self._output(data)
        if name == "simple_stream":
            return self.req_GetStreamStatus(data)
        if name == "simple_file_output":
            return self._output_status(self.host.record_status())
        if name == "Replay Buffer":
            return self._output_status({"seconds": 0.0} if self.host.clips_ready() else None)
        return self._output_status(self._places().get(name[10:]))

    def req_StartOutput(self, data):
        name = self._output(data)
        if name == "simple_stream":
            return self.req_StartStream(data)
        if name == "simple_file_output":
            return self.req_StartRecord(data)
        if name == "Replay Buffer":
            return self.req_StartReplayBuffer(data)
        if self.host.stream_status() is None:
            raise RequestError(Status.OUTPUT_NOT_RUNNING, "Not live: start the stream first.")
        if name[10:] in self._places():
            raise RequestError(Status.OUTPUT_RUNNING, "Already sending there.")
        problem = self.host.start_place(name[10:])
        if problem:
            raise RequestError(Status.OUTPUT_DISABLED, problem)

    def req_StopOutput(self, data):
        name = self._output(data)
        if name == "simple_stream":
            return self.req_StopStream(data)
        if name == "simple_file_output":
            self.req_StopRecord(data)
            return None
        if name == "Replay Buffer":
            return self.req_StopReplayBuffer(data)
        if name[10:] not in self._places():
            raise RequestError(Status.OUTPUT_NOT_RUNNING, "Not sending there.")
        problem = self.host.stop_place(name[10:])
        if problem:
            raise RequestError(Status.OUTPUT_DISABLED, problem)

    def req_ToggleOutput(self, data):
        name = self._output(data)
        on = (self.host.stream_status() is not None if name == "simple_stream" else
              self.host.record_status() is not None if name == "simple_file_output" else
              self.host.clips_ready() if name == "Replay Buffer" else name[10:] in self._places())
        (self.req_StopOutput if on else self.req_StartOutput)(data)
        return {"outputActive": not on}

    # Input settings, filters, media and source pictures: what TikFinity, Streamer.bot and Stream
    # Deck's OBS actions send most (change a text, turn a filter on, play a sound, grab a picture).

    def req_GetInputDefaultSettings(self, data):
        kind = _require(data, "inputKind", str)
        ours = next((k for k, obs in INPUT_KINDS.items() if obs == kind), kind)
        import sprigcast.sources  # noqa: F401  (registers the built-in kinds)
        from sprigcast.core.source import source_class

        try:
            cls = source_class(ours)
        except Exception:
            cls = None
        if cls is None:
            raise RequestError(Status.INVALID_REQUEST_FIELD, f"Unknown inputKind {kind!r}.")
        return {"defaultInputSettings": dict(getattr(cls, "defaults", {}) or {})}

    def req_SetInputSettings(self, data):
        source = self._input(data)
        settings = _require(data, "inputSettings", dict)
        overlay = data.get("overlay", True)
        if not isinstance(overlay, bool):
            raise RequestError(Status.INVALID_REQUEST_FIELD_TYPE, "overlay has the wrong type.")
        theirs = settings_from_obs(source.kind, settings)
        before = source.settings
        if overlay:
            source.settings = {**source.settings, **theirs}
        else:  # OBS's "replace": back to the kind's defaults (so nothing it needs goes missing), mixer settings kept
            from sprigcast.core.source import source_class

            defaults = dict(getattr(source_class(source.kind), "defaults", None) or {})
            kept = {k: v for k, v in source.settings.items() if k in KEEP_ON_REPLACE}
            source.settings = {**defaults, **kept, **theirs}
        sound_changed(source, before)
        self.host.commit("sources")

    def req_SetInputName(self, data):
        source = self._input(data)
        name = _require(data, "newInputName", str).strip()
        if not name:
            raise RequestError(Status.REQUEST_FIELD_EMPTY, "newInputName is empty.")
        if any(s.name == name and s is not source for s in self._inputs()):
            raise RequestError(Status.RESOURCE_ALREADY_EXISTS, f"A source called {name!r} already exists.")
        if name != source.name:
            log.info("control: an OBS-style app renamed %s %r -> %r", source.id, source.name, name)
        source.name = name
        self.host.commit("sources")

    def req_PressInputPropertiesButton(self, data):
        source = self._input(data)
        button = _require(data, "propertyName", str)
        if source.kind != "browser" or button not in ("refreshnocache", "refresh"):
            raise RequestError(Status.INVALID_REQUEST_FIELD, f"{source.name!r} has no {button!r} button.")
        running = self.host.source_instance(source.id)
        if running is not None and hasattr(running, "reload"):
            running.reload()

    def _filter_source(self, data: dict[str, Any]) -> SourceConfig:
        """The source a filter request names: an input, or a sprig (its first placement)."""
        uuid, name = data.get("sourceUuid"), data.get("sourceName")
        if uuid is None and name is None:
            raise RequestError(Status.MISSING_REQUEST_FIELD, "sourceName or sourceUuid is missing.")
        for source in self._inputs():
            if (uuid is not None and source.id == uuid) or (uuid is None and source.name == name):
                return source
        try:
            scene = self._scene({"sceneName": name, "sceneUuid": uuid})
        except RequestError:
            scene = None
        if scene is not None:
            placed = next((s for s in self.col.sources if s.kind == PART_KIND and self.col.part_of(s) is scene), None)
            if placed is not None:
                return placed
        raise RequestError(Status.RESOURCE_NOT_FOUND, f"No source called {uuid or name!r}.")

    def _filter_target(self, data: dict[str, Any]) -> tuple[SourceConfig, list]:
        """The source a filter request names and the filter list it means. "Name (Vertical)" (a canvas in
        brackets), when no source has that name, is the source's vertical placements: their own filters when
        that canvas has its own ("Same on both canvases" off), else the shared ones."""
        try:
            source = self._filter_source(data)
            return source, source.filters
        except RequestError as missing:
            name = data.get("sourceName") if data.get("sourceUuid") is None else None
            shared = self.col.named_for_canvas(name) if isinstance(name, str) else None
            if shared is not None:  # a copy's old name: the source on that canvas
                source, canvas = shared
                return source, source.canvas_filters.get(canvas, source.filters)
            match = re.fullmatch(r"(.+) \(([^()]+)\)", name.strip()) if isinstance(name, str) else None
            canvas = next((c.id for c in self.col.canvases if match and c.id.lower() == match.group(2).lower()), None)
            if canvas is None:
                raise missing from None
            source = self._filter_source({"sourceName": match.group(1)})
            return source, source.canvas_filters.get(canvas, source.filters)

    def _source_filter(self, data: dict[str, Any]) -> tuple[SourceConfig, Any]:
        return self._source_filter_in(data)[:2]

    def _source_filter_in(self, data: dict[str, Any]) -> tuple[SourceConfig, Any, list]:
        source, filters = self._filter_target(data)
        wanted = _require(data, "filterName", str)
        found = next((f for f in filters if f.name == wanted), None)
        if found is None:
            raise RequestError(Status.RESOURCE_NOT_FOUND, f"{source.name!r} has no filter {wanted!r}.")
        return source, found, filters

    @staticmethod
    def _filter_obs(index: int, f) -> dict[str, Any]:
        return {"filterName": f.name, "filterKind": f.kind, "filterIndex": index,
                "filterEnabled": f.enabled, "filterSettings": dict(f.settings)}

    def req_GetSourceFilterList(self, data):
        _source, filters = self._filter_target(data)
        return {"filters": [self._filter_obs(i, f) for i, f in enumerate(filters)]}

    def req_GetSourceFilter(self, data):
        _source, found, filters = self._source_filter_in(data)
        info = self._filter_obs(filters.index(found), found)
        info.pop("filterName")
        return info

    def req_SetSourceFilterEnabled(self, data):
        source, found = self._source_filter(data)
        from sprigcast.core.filters import switch

        switch(found, _require(data, "filterEnabled", bool))
        self.col.share_filters(source)
        self.host.commit("filters")

    def req_SetSourceFilterSettings(self, data):
        source, found = self._source_filter(data)
        settings = _require(data, "filterSettings", dict)
        overlay = data.get("overlay", True)
        found.settings = {**found.settings, **settings} if overlay is not False else dict(settings)
        self.col.share_filters(source)
        self.host.commit("filters")

    def req_SetSourceFilterName(self, data):
        source, found, filters = self._source_filter_in(data)
        name = _require(data, "newFilterName", str).strip()
        if not name:
            raise RequestError(Status.REQUEST_FIELD_EMPTY, "newFilterName is empty.")
        if any(f.name == name and f is not found for f in filters):
            raise RequestError(Status.RESOURCE_ALREADY_EXISTS, f"{source.name!r} already has a filter {name!r}.")
        found.name = name
        self.col.share_filters(source)
        self.host.commit("filters")

    def _media_input(self, data: dict[str, Any]) -> SourceConfig:
        source = self._input(data)
        if source.kind != "media":
            raise RequestError(Status.INVALID_RESOURCE_TYPE, f"{source.name!r} isn't a video or sound file.")
        return source

    def _media_run(self, source: SourceConfig, what: str, seconds: float | None = None) -> dict[str, Any]:
        info = self.host.media_control(source.id, what, seconds)
        if info is None:
            raise RequestError(Status.INVALID_RESOURCE_STATE, f"{source.name!r} isn't loaded yet: put its bloom on screen first.")
        return info

    def req_GetMediaInputStatus(self, data):
        source = self._media_input(data)
        info = self.host.media_info(source.id)
        if not info:
            return {"mediaState": "OBS_MEDIA_STATE_NONE", "mediaDuration": None, "mediaCursor": None}
        return {"mediaState": MEDIA_STATES.get(info.get("state"), "OBS_MEDIA_STATE_NONE"),
                "mediaDuration": _ms(info.get("duration")), "mediaCursor": _ms(info.get("position"))}

    def req_TriggerMediaInputAction(self, data):
        source = self._media_input(data)
        action = _require(data, "mediaAction", str)
        what = MEDIA_ACTIONS.get(action)
        if what is None:
            raise RequestError(Status.INVALID_REQUEST_FIELD, f"Unknown mediaAction {action!r}.")
        if what:
            self._media_run(source, what)

    def req_SetMediaInputCursor(self, data):
        source = self._media_input(data)
        self._media_run(source, "seek", max(0.0, _number(data, "mediaCursor") / 1000))

    def req_OffsetMediaInputCursor(self, data):
        source = self._media_input(data)
        info = self.host.media_info(source.id)
        if not info:
            raise RequestError(Status.INVALID_RESOURCE_STATE, f"{source.name!r} isn't loaded yet: put its bloom on screen first.")
        self._media_run(source, "seek", max(0.0, float(info.get("position") or 0) + _number(data, "mediaCursorOffset") / 1000))

    def _picture(self, data: dict[str, Any]) -> dict[str, Any]:
        """A PNG of a bloom that's on air (or the canvas it's on air in). Other sources can't be
        pictured on their own yet: OBS apps nearly always ask for the program scene."""
        fmt = str(data.get("imageFormat", "png")).lower()
        if fmt != "png":
            raise RequestError(Status.INVALID_REQUEST_FIELD, "SprigCast saves pictures as png only.")
        name = data.get("sourceName")
        uuid = data.get("sourceUuid")
        scene = self._scene({"sceneName": name, "sceneUuid": uuid})
        canvases = [cid for cid, sid in self.col.active_scenes.items()
                    if sid == scene.id or any(t.id == sid for t in self.col.linked_with(scene.id))]
        if not canvases:
            raise RequestError(Status.INVALID_RESOURCE_STATE, f"{self._scene_name(scene)!r} isn't on air, so there's nothing to picture.")
        width = data.get("imageWidth")
        if width is not None and (not isinstance(width, int) or not 8 <= width <= 4096):
            raise RequestError(Status.REQUEST_FIELD_OUT_OF_RANGE, "imageWidth must be between 8 and 4096.")
        # Some apps ask for a picture every second or two for a key preview: answer those from a picture that's
        # at most PICTURE_REUSE_S old, so grabbing and PNG-encoding never crowds out the encoders.
        import time

        key = (canvases[0], width)
        cached = self._pictures.get(key)
        if cached is not None and time.monotonic() - cached[0] < PICTURE_REUSE_S:
            return cached[1]
        shots = self.host.screenshot([canvases[0]], width, False, True)
        if not shots:
            raise RequestError(Status.INVALID_RESOURCE_STATE, "Nothing is drawing right now, so there's nothing to picture.")
        self._pictures = {key: (time.monotonic(), shots[0])}  # one kept: no pile of old pictures
        return shots[0]

    def req_GetSourceScreenshot(self, data):
        shot = self._picture(data)
        return {"imageData": "data:image/png;base64," + shot["png"]}

    def req_SaveSourceScreenshot(self, data):
        import base64
        from pathlib import Path

        path = Path(_require(data, "imageFilePath", str))
        if not path.is_absolute() or path.suffix.lower() != ".png":
            raise RequestError(Status.INVALID_REQUEST_FIELD, "imageFilePath must be a full path ending in .png.")
        shot = self._picture(data)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(shot["png"]))
        except OSError as exc:
            raise RequestError(Status.GENERIC_ERROR, f"Couldn't save the picture: {exc}") from exc

    # SprigCast's own requests (CallVendorRequest, vendorName "sprigcast") -------------------

    def vendor_GetCanvases(self, data):
        """Each canvas (horizontal, vertical), its size and the bloom on air there."""
        out = []
        for canvas in self.col.canvases:
            scene = self.col.active_scene(canvas.id)
            out.append({"canvas": canvas.id, "width": canvas.width, "height": canvas.height, "fps": canvas.fps,
                        "enabled": canvas.enabled, "sceneName": self._scene_name(scene),
                        "sceneUuid": scene.id if scene else None})
        return {"canvases": out}

    def vendor_SetCanvasProgramScene(self, data):
        """Put a bloom on air on its own canvas only (its linked twins stay as they are)."""
        scene = self._scene(data)
        if scene.kind != "show":
            raise RequestError(Status.INVALID_RESOURCE_TYPE, f"{scene.name!r} is a sprig.")
        canvas = data.get("canvas")
        if canvas is not None:
            twin = next((s for s in self.col.linked_with(scene.id) if s.canvas == canvas), None)
            if twin is None:
                raise RequestError(Status.RESOURCE_NOT_FOUND, f"{scene.name!r} has no {canvas} bloom.")
            scene = twin
        self.col.active_scenes[scene.canvas] = scene.id
        self.host.commit("program")
        return {"canvas": scene.canvas, "sceneUuid": scene.id}

    def vendor_SaveClip(self, data):
        """Save a clip of the last seconds; starts clips first if they're off (that clip starts now)."""
        if not self.host.clips_ready():
            problem = self.host.set_clips(True)
            if problem:
                raise RequestError(Status.OUTPUT_DISABLED, problem)
            return {"saved": [], "started": True}
        self._last_clip = self.host.save_clip()
        return {"saved": self._last_clip, "started": False}

    def vendor_GetSprigStates(self, data):
        """Every sprig with Live states: its states and the one showing (null = as built)."""
        return {"sprigs": [{"sceneName": self._scene_name(p), "sceneUuid": p.id, "state": p.state,
                            "states": [s.name for s in p.states]} for p in self.col.parts() if p.states]}

    def vendor_SetSprigState(self, data):
        """Switch a sprig to one of its Live states; state null (or "") goes back to as built."""
        part = self._scene(data)
        if part.kind != "part":
            raise RequestError(Status.INVALID_RESOURCE_TYPE, f"{part.name!r} is a bloom, not a sprig.")
        name = data.get("state") or None
        if name is not None and all(s.name != name for s in part.states):
            raise RequestError(Status.RESOURCE_NOT_FOUND, f"{part.name!r} has no state {name!r}.")
        self.col.set_state(part.id, name)
        self.host.commit("states")

    def vendor_GetStreamPlaces(self, data):
        """Each place you stream to (Twitch, YouTube...), which canvas it gets, and if it's on."""
        return {"places": [{"id": o.id, "service": o.service, "canvas": o.canvas, "enabled": o.enabled,
                            "bitrateKbps": o.bitrate_kbps} for o in self.col.outputs if o.type == "stream"]}

    def vendor_SetStreamPlaceEnabled(self, data):
        """Switch a place on or off for the next Go live (not while live)."""
        place = _require(data, "id", str)
        output = next((o for o in self.col.outputs if o.id == place and o.type == "stream"), None)
        if output is None:
            raise RequestError(Status.RESOURCE_NOT_FOUND, f"No place {place!r} (see GetStreamPlaces).")
        if self.host.stream_status() is not None:
            raise RequestError(Status.OUTPUT_RUNNING, "End the stream to change where it goes.")
        output.enabled = bool(_require(data, "enabled", bool))
        self.host.commit("places")

    # Events --------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        col = self.col
        names = scene_names(col)
        items: dict[tuple[str, int], bool] = {}
        for _, scene in names:
            for i, item in enumerate(scene.items):
                items[(scene.id, self._item_id(scene, i))] = item.visible
        sounds = {s.id: (s.name, bool(s.settings.get("muted", False)), float(s.settings.get("volume_db", 0.0)))
                  for s in self._inputs() if self._has_sound(s)}
        return {
            "program": dict(col.active_scenes),
            "scenes": [(n, s.id) for n, s in names],
            "items": items,
            "sounds": sounds,
            "stream": self.host.stream_status() is not None,
            "reconnecting": bool((self.host.stream_status() or {}).get("reconnecting")),
            "places": {pid: (place_state(now), int(now.get("reconnects", 0))) for pid, now in self._places().items()}
            if getattr(self.host, "stream_places", None) is not None else None,
            "record": self.host.record_status() is not None,
            "clips": self.host.clips_ready(),
            "camera": self._camera_on(),
            "transition": (col.transition.kind, col.transition.duration_ms),
            "states": {p.id: p.state for p in col.parts() if p.states},
            "filters": {(src.id, f.name): f.enabled for src in col.sources for f in src.filters},
            "shelf": self._shelf(),
        }

    def changes(self) -> list[tuple[str, int, dict[str, Any]]]:
        """Events for what changed since the last call: (eventType, intent, eventData)."""
        now = self.snapshot()
        last, self._last = self._last, now
        if last is None:
            return []
        events: list[tuple[str, int, dict[str, Any]]] = []
        names = dict((sid, n) for n, sid in now["scenes"])
        names.update(self._names())

        old_ids = {sid for _, sid in last["scenes"]}
        new_ids = {sid for _, sid in now["scenes"]}
        for n, sid in last["scenes"]:
            if sid not in new_ids:
                events.append(("SceneRemoved", Sub.SCENES, {"sceneName": n, "sceneUuid": sid, "isGroup": False}))
        for n, sid in now["scenes"]:
            if sid not in old_ids:
                events.append(("SceneCreated", Sub.SCENES, {"sceneName": n, "sceneUuid": sid, "isGroup": False}))
        old_names = dict((sid, n) for n, sid in last["scenes"])
        for n, sid in now["scenes"]:
            if sid in old_names and old_names[sid] != n:
                events.append(("SceneNameChanged", Sub.SCENES, {"sceneUuid": sid, "oldSceneName": old_names[sid], "sceneName": n}))
        if now["scenes"] != last["scenes"]:
            events.append(("SceneListChanged", Sub.SCENES, {"scenes": self.req_GetSceneList({})["scenes"]}))

        first = self.col.canvases[0].id
        for canvas_id, scene_id in now["program"].items():
            if last["program"].get(canvas_id) == scene_id:
                continue
            data = {"sceneName": names.get(scene_id, scene_id), "sceneUuid": scene_id}
            if canvas_id == first or first not in now["program"]:
                events.append(("CurrentProgramSceneChanged", Sub.SCENES, data))
            events.append(("VendorEvent", Sub.VENDORS, {"vendorName": VENDOR, "eventType": "CanvasProgramChanged",
                                                        "eventData": {"canvas": canvas_id, **data}}))

        for key, visible in now["items"].items():
            if key in last["items"] and last["items"][key] != visible:
                events.append(("SceneItemEnableStateChanged", Sub.SCENE_ITEMS,
                               {"sceneName": names.get(key[0], key[0]), "sceneUuid": key[0],
                                "sceneItemId": key[1], "sceneItemEnabled": visible}))

        for sid, (name, muted, db) in now["sounds"].items():
            old = last["sounds"].get(sid)
            if old is None:
                continue
            if old[1] != muted:
                events.append(("InputMuteStateChanged", Sub.INPUTS, {"inputName": name, "inputUuid": sid, "inputMuted": muted}))
            if old[2] != db:
                events.append(("InputVolumeChanged", Sub.INPUTS, {"inputName": name, "inputUuid": sid,
                                                                  "inputVolumeDb": db, "inputVolumeMul": _db_to_mul(db)}))

        for key, event in (("stream", "StreamStateChanged"), ("record", "RecordStateChanged"),
                           ("clips", "ReplayBufferStateChanged"), ("camera", "VirtualcamStateChanged")):
            if now.get(key) != last.get(key):
                state = "OBS_WEBSOCKET_OUTPUT_STARTED" if now[key] else "OBS_WEBSOCKET_OUTPUT_STOPPED"
                events.append((event, Sub.OUTPUTS, {"outputActive": now[key], "outputState": state}))

        if now["stream"] and last.get("stream") and now.get("reconnecting") != last.get("reconnecting", False):
            # As OBS says it: the stream lost its connection and is trying again, then is back.
            state = "OBS_WEBSOCKET_OUTPUT_RECONNECTING" if now["reconnecting"] else "OBS_WEBSOCKET_OUTPUT_RECONNECTED"
            events.append(("StreamStateChanged", Sub.OUTPUTS, {"outputActive": True, "outputState": state}))
        if now.get("places") is not None and last.get("places") is not None:
            place_name, outputs = place_names(self.col), {o.id: o for o in self.col.outputs}
            for pid in [*now["places"], *(p for p in last["places"] if p not in now["places"])]:
                state, tries = now["places"].get(pid, ("stopped", last["places"].get(pid, ("", 0))[1]))
                was, was_tries = last["places"].get(pid, ("stopped", 0))
                if (state, tries) == (was, was_tries):
                    continue
                o = outputs.get(pid)
                events.append(("VendorEvent", Sub.VENDORS, {"vendorName": VENDOR, "eventType": "PlaceStateChanged", "eventData": {
                    "outputName": place_name.get(pid, pid), "outputId": pid, "service": o.service if o else None,
                    "canvas": o.canvas if o else None, "state": state, "was": was, "reconnects": tries,
                    "dropped": tries > was_tries and "live" in (was, state)}}))

        if now["transition"][0] != last["transition"][0]:
            kind = now["transition"][0]
            events.append(("CurrentSceneTransitionChanged", Sub.TRANSITIONS,
                           {"transitionName": TRANSITION_NAMES[kind], "transitionUuid": kind}))
        if now["transition"][1] != last["transition"][1]:
            events.append(("CurrentSceneTransitionDurationChanged", Sub.TRANSITIONS,
                           {"transitionDuration": now["transition"][1]}))

        for pid, state in now["states"].items():
            if pid in last["states"] and last["states"][pid] != state:
                events.append(("VendorEvent", Sub.VENDORS, {"vendorName": VENDOR, "eventType": "SprigStateChanged",
                                                            "eventData": {"sceneName": names.get(pid, pid),
                                                                          "sceneUuid": pid, "state": state}}))

        for (sid, fname), on in now.get("filters", {}).items():
            if last.get("filters", {}).get((sid, fname), on) != on:
                events.append(("SourceFilterEnableStateChanged", Sub.FILTERS,
                               {"sourceName": self._source_name(sid), "filterName": fname, "filterEnabled": on}))

        shelf, old = now["shelf"], last["shelf"]
        if shelf["gardens"] != old["gardens"]:
            events.append(("SceneCollectionListChanged", Sub.CONFIG, {"sceneCollections": shelf["gardens"]}))
        if shelf["garden"] != old["garden"]:
            events.append(("CurrentSceneCollectionChanged", Sub.CONFIG, {"sceneCollectionName": shelf["garden"]}))
        if shelf["paths"] != old["paths"]:
            events.append(("ProfileListChanged", Sub.CONFIG, {"profiles": shelf["paths"]}))
        if shelf["path"] != old["path"]:
            events.append(("CurrentProfileChanged", Sub.CONFIG, {"profileName": shelf["path"]}))
        return events

    def meters(self, levels: dict[str, tuple[float, float]]) -> dict[str, Any]:
        """InputVolumeMeters data from the mixer's (peak, RMS) dBFS per source."""
        inputs = []
        for source in self._inputs():
            level = levels.get(source.id)
            if level is None:
                continue
            peak, rms = (_db_to_mul(v) if v > -100 else 0.0 for v in level)
            inputs.append({"inputName": source.name, "inputUuid": source.id,
                           "inputLevelsMul": [[rms, peak, peak], [rms, peak, peak]]})
        return {"inputs": inputs}


# OBS's alignment bits: left 1, right 2, top 4, bottom 8 (0 is the centre).
_ALIGN_BITS = {"top_left": 5, "top": 4, "top_right": 6, "left": 1, "center": 0, "right": 2,
               "bottom_left": 9, "bottom": 8, "bottom_right": 10}
BOUNDS_TO_OBS = {"off": "OBS_BOUNDS_NONE", "fit": "OBS_BOUNDS_SCALE_INNER", "fill": "OBS_BOUNDS_SCALE_OUTER",
                 "stretch": "OBS_BOUNDS_STRETCH"}
BOUNDS_FROM_OBS = {v: k for k, v in BOUNDS_TO_OBS.items()} | {"OBS_BOUNDS_MAX_ONLY": "fit",
                                                               "OBS_BOUNDS_SCALE_TO_WIDTH": "fit",
                                                               "OBS_BOUNDS_SCALE_TO_HEIGHT": "fit"}


def _obs_align(align: str) -> int:
    return _ALIGN_BITS.get(align, 0)


def _from_obs_align(bits: int) -> str:
    return next((name for name, b in _ALIGN_BITS.items() if b == bits), "center")

