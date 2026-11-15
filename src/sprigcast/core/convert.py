"""Turn scene collections from OBS Studio, Streamlabs Desktop and Meld Studio into SprigCast gardens.

OBS is the base: Streamlabs and Meld files are first rewritten into OBS's shape, then converted
the same way. What comes over:

- Scenes nested in other scenes, and groups, become sprigs; scenes nothing else uses become blooms
  (and their contents move into a sprig each, as every bloom in SprigCast holds sprigs).
- Empty "divider" scenes (like "--- Main Scenes ---" or "🗄 Main Scenes 🗄") become folders for the
  scenes listed after them.
- A second canvas (OBS 31 canvases / Aitum Vertical, Streamlabs dual output) becomes the vertical
  canvas; "Name (Vertical)" blooms switch together with "Name".
- Pictures, videos and sounds, browser pages, colours, text, cameras, display, window and game
  capture, mic, desktop and app sound, with volume, mute and the common filters.
- Fade, cut, slide, swipe and stinger transitions, and per-scene transition overrides.

Positions are kept by working out each item's top-left corner from OBS's anchor point, which needs
the source's size: known for pages, colours, scenes and cameras; read from the file for pictures and
videos when it's on this PC; guessed (the canvas) for captures. Anything that couldn't come over is
listed in the notes the importer returns. Nothing here knows about Qt.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sprigcast.core.scene import (
    ANY,
    PART_KIND,
    Filter,
    Scene,
    SceneCollection,
    SceneFolder,
    SceneItem,
    SourceConfig,
    Stinger,
    Transition,
    TransitionRule,
)

MAX_BYTES = 64 * 1024 * 1024  # a scene collection bigger than this isn't one
VERTICAL_SUFFIX = re.compile(r"\s*[\(\[]\s*vertical\s*[\)\]]\s*$", re.IGNORECASE)
_ALIGN = {5: "top_left", 4: "top", 6: "top_right", 1: "left", 0: "center", 2: "right", 9: "bottom_left",
          8: "bottom", 10: "bottom_right"}
_BOUNDS = {1: "stretch", 2: "fit", 3: "fill", 4: "fit", 5: "fit", 6: "fit"}

SizeOf = Callable[[str, dict[str, Any]], "tuple[int, int] | None"]


class ConvertError(Exception):
    """The file isn't a scene collection we can read; the message says so in plain words."""


# Reading ----------------------------------------------------------------------------------------------


def read_json(path: Path) -> Any:
    path = Path(path)
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ConvertError(f"{path.name} is too big to be a scene collection.")
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ConvertError(f"{path.name} can't be read.") from exc


def detect(data: Any) -> str:
    """"sprigcast", "obs", "streamlabs", "meld", or "" when it's none of them."""
    if not isinstance(data, dict):
        return ""
    if "canvases" in data and isinstance(data.get("scenes"), list) and "outputs" in data:
        return "sprigcast"
    if isinstance(data.get("sources"), list) and ("scene_order" in data or "current_scene" in data):
        return "obs"
    if data.get("nodeType") == "RootNode" or (isinstance(data.get("sources"), dict)
                                              and data["sources"].get("nodeType") == "SourcesNode"):
        return "streamlabs"
    items = data.get("items") if isinstance(data.get("items"), dict) else \
        (data.get("session") or {}).get("items") if isinstance(data.get("session"), dict) else None
    if isinstance(items, dict) and any(isinstance(v, dict) and v.get("type") == "scene" for v in items.values()):
        return "meld"
    return ""


APP_NAMES = {"obs": "OBS", "streamlabs": "Streamlabs", "meld": "Meld"}


def convert(data: Any, like: SceneCollection, size_of: SizeOf | None = None,
            audio_devices: list[str] | None = None) -> tuple[SceneCollection, list[str]]:
    """Any supported file's data -> (garden, notes about what didn't come over)."""
    kind = detect(data)
    if kind == "obs":
        return from_obs(data, like, size_of, audio_devices)
    if kind == "streamlabs":
        obs, notes = streamlabs_to_obs(data)
        garden, more = from_obs(obs, like, size_of, audio_devices)
        return garden, notes + more
    if kind == "meld":
        obs, notes = meld_to_obs(data)
        garden, more = from_obs(obs, like, size_of, audio_devices)
        return garden, notes + more
    raise ConvertError("That isn't a scene collection from OBS, Streamlabs or Meld.")


# Sizes ------------------------------------------------------------------------------------------------


def file_size_of(kind: str, settings: dict[str, Any]) -> tuple[int, int] | None:
    """The default size_of: reads pictures (Pillow) and videos (PyAV) that are on this PC."""
    if kind == "text":
        try:  # drawn the way SprigCast draws it
            from sprigcast.sources.text import TextSource

            return TextSource("size", dict(settings)).size
        except Exception:
            return None
    path = str(settings.get("path", "") or "")
    if not path or not Path(path).is_file():
        return None
    try:
        if kind == "image":
            from PIL import Image

            with Image.open(path) as image:
                return image.size
        if kind == "media":
            import av

            with av.open(path) as container:
                stream = next((s for s in container.streams if s.type == "video"), None)
                if stream is not None and stream.codec_context.width:
                    return stream.codec_context.width, stream.codec_context.height
    except Exception:
        return None
    return None


# Small helpers ----------------------------------------------------------------------------------------


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _xy(value: Any, default: tuple[float, float] = (0.0, 0.0)) -> tuple[float, float]:
    if isinstance(value, dict):
        return _num(value.get("x"), default[0]), _num(value.get("y"), default[1])
    return default


def obs_color(value: Any, default: str = "#ffffffff") -> str:
    """OBS keeps colours as one number, 0xAABBGGRR. SprigCast writes #rrggbbaa."""
    try:
        n = int(value) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return default
    a, b, g, r = (n >> 24) & 255, (n >> 16) & 255, (n >> 8) & 255, n & 255
    return f"#{r:02x}{g:02x}{b:02x}{a:02x}"


def _volume_db(multiplier: Any) -> float:
    m = _num(multiplier, 1.0)
    return -100.0 if m <= 0.00001 else round(20 * math.log10(m), 1)


def _window_parts(window: str) -> tuple[str, str]:
    """OBS's "title:class:exe" (colons inside written #3A) -> (title, exe)."""
    parts = [p.replace("#3A", ":") for p in str(window or "").split(":")]
    title = parts[0] if parts else ""
    exe = parts[2] if len(parts) > 2 else ""
    return title, exe


def divider_name(name: str) -> str | None:
    """The folder an empty divider scene stands for, or None if the name isn't one.

    "🗄 Main Scenes 🗄" -> "Main Scenes", "--- Alerts ---" -> "Alerts", "[ Media ]" -> "Media"."""
    name = name.strip()
    if len(name) < 3 or name[0].isalnum() or name[-1].isalnum():
        return None
    inner = re.sub(r"^[\W_]+|[\W_]+$", "", name).strip()
    return inner or None


def strip_vertical(name: str) -> str:
    return VERTICAL_SUFFIX.sub("", name).strip() or name


# Sources ----------------------------------------------------------------------------------------------


def _source(obs: dict[str, Any], audio_devices: list[str] | None, notes: list[str]) -> SourceConfig | None:
    """One OBS input as a SprigCast source (without its id), or None when there's no match."""
    kind = str(obs.get("id") or obs.get("versioned_id") or "")
    s = obs.get("settings") or {}
    name = str(obs.get("name") or kind)
    out: tuple[str, dict[str, Any]] | None = None
    if kind == "image_source":
        out = "image", {"path": str(s.get("file", ""))}
    elif kind == "ffmpeg_source":
        if s.get("is_local_file", True) is False:
            notes.append(f"{name}: a network stream (not a file) isn't supported yet.")
            return None
        out = "media", {"path": str(s.get("local_file", "")), "loop": bool(s.get("looping", False)),
                        "restart_on_show": bool(s.get("restart_on_activate", True)),
                        "clear_on_end": bool(s.get("clear_on_media_end", False))}
    elif kind == "browser_source":
        local = bool(s.get("is_local_file", False))
        settings: dict[str, Any] = {"url": "https://" if local else str(s.get("url", "https://")),
                                    "file": str(s.get("local_file", "")) if local else "",
                                    "width": int(_num(s.get("width"), 800)), "height": int(_num(s.get("height"), 600)),
                                    "keep_running": not bool(s.get("shutdown", False)),
                                    "reload_on_air": bool(s.get("restart_when_active", False)),
                                    "audio_to_mixer": bool(s.get("reroute_audio", False))}
        if "css" in s:
            settings["css"] = str(s.get("css", ""))
        if s.get("fps_custom"):
            settings.update(custom_fps=True, fps=int(_num(s.get("fps"), 30)))
        out = "browser", settings
    elif kind in ("color_source", "color_source_v2", "color_source_v3"):
        out = "color", {"color": obs_color(s.get("color", 0xFFD1D1D1)),
                        "width": int(_num(s.get("width"), 1920)), "height": int(_num(s.get("height"), 1080))}
    elif kind.startswith(("text_gdiplus", "text_ft2_source")):
        font = s.get("font") or {}
        face = re.sub(r"[^a-z0-9]", "", str(font.get("face", "")).lower())
        settings = {"text": str(s.get("text", "")), "size": int(_num(font.get("size"), 72)),
                    **({"font": f"{face}.ttf"} if face else {}),
                    "color": obs_color(s.get("color", s.get("color1", 0xFFFFFFFF)))}
        if s.get("read_from_file") or s.get("from_file"):
            settings["file"] = str(s.get("file", s.get("text_file", "")))
        if s.get("outline"):
            settings.update(outline_width=int(_num(s.get("outline_size"), 2)),
                            outline_color=obs_color(s.get("outline_color", 0xFF000000)))
        out = "text", settings
    elif kind == "dshow_input":
        device = str(s.get("video_device_id", ""))
        cam_name, _, path = device.partition(":")
        settings = {"name": cam_name, "path": path}
        res = str(s.get("resolution", ""))
        if s.get("res_type") == 1 and "x" in res:
            w, _, h = res.partition("x")
            settings.update(width=int(_num(w, 1920)), height=int(_num(h, 1080)))
        out = "camera", settings
    elif kind == "monitor_capture":
        out = "display_capture", {"monitor": int(_num(s.get("monitor"), 0)) + 1, "cursor": bool(s.get("capture_cursor", True))}
    elif kind == "window_capture":
        title, exe = _window_parts(s.get("window", ""))
        out = "window_capture", {"title": title, "process": exe, "cursor": bool(s.get("cursor", True))}
    elif kind == "game_capture":
        mode = str(s.get("capture_mode", "any_fullscreen"))
        if mode == "window":
            title, exe = _window_parts(s.get("window", ""))
            out = "game_capture", {"mode": "window", "title": title, "process": exe}
        elif mode == "hotkey":
            out = "game_capture", {"mode": "hotkey"}
        else:
            out = "game_capture", {"mode": "any"}
    elif kind in ("wasapi_input_capture", "wasapi_output_capture", "coreaudio_input_capture",
                  "coreaudio_output_capture", "pulse_input_capture", "pulse_output_capture"):
        sk = "audio_input" if "input" in kind else "audio_output"
        device_id = str(s.get("device_id", "default") or "default")
        device = "default" if device_id == "default" else _device(name, device_id, audio_devices)
        out = sk, {"device": device or "default"}
        if device is None:  # not found here: kept quiet, so it can't double another sound
            out[1]["unmatched"] = True
    elif kind == "wasapi_process_output_capture":
        _title, exe = _window_parts(s.get("window", ""))
        out = "app_audio", {"apps": [exe] if exe else []}
    if out is None:
        notes.append(f"{name}: {kind or 'this kind of source'} has no match in SprigCast yet.")
        return None
    sk, settings = out
    if sk in ("audio_input", "audio_output", "app_audio", "media", "browser"):
        settings["volume_db"] = _volume_db(obs.get("volume", 1.0))
        settings["muted"] = bool(obs.get("muted", False))
    return SourceConfig(id="", kind=sk, name=name, settings=settings, filters=_filters(obs, notes))


def _device(name: str, device_id: str, devices: list[str] | None) -> str | None:
    """OBS names sound devices by a Windows id, SprigCast by name: match the source's name to one."""
    clean = re.sub(r"^\s*\[[^\]]*\]\s*", "", name).strip().casefold()  # "[AOC] Voice Chat" -> "voice chat"
    for device in devices or []:
        if clean and (clean in device.casefold() or device.casefold().split(" (")[0] == clean):
            return device
    return None


_KEY_COLORS = {"green": "#00ff00", "blue": "#0000ff", "magenta": "#ff00ff"}


def _filters(obs: dict[str, Any], notes: list[str]) -> list[Filter]:
    out: list[Filter] = []
    for n, f in enumerate(obs.get("filters") or [], 1):
        kind = str(f.get("id") or f.get("versioned_id") or "")
        s = f.get("settings") or {}
        mapped: tuple[str, dict[str, Any]] | None = None
        if kind == "compressor_filter":
            mapped = "compressor", {"ratio": _num(s.get("ratio"), 10.0), "threshold_db": _num(s.get("threshold"), -18.0),
                                    "attack_ms": int(_num(s.get("attack_time"), 6)),
                                    "release_ms": int(_num(s.get("release_time"), 60)),
                                    "output_db": _num(s.get("output_gain"), 0.0)}
        elif kind == "limiter_filter":
            mapped = "limiter", {"threshold_db": _num(s.get("threshold"), -6.0),
                                 "release_ms": int(_num(s.get("release_time"), 60))}
        elif kind == "expander_filter":
            mapped = "expander", {"ratio": _num(s.get("ratio"), 2.0), "threshold_db": _num(s.get("threshold"), -40.0),
                                  "attack_ms": int(_num(s.get("attack_time"), 10)),
                                  "release_ms": int(_num(s.get("release_time"), 50))}
        elif kind == "noise_gate_filter":
            mapped = "noise_gate", {"close_db": _num(s.get("close_threshold"), -32.0),
                                    "open_db": _num(s.get("open_threshold"), -26.0),
                                    "attack_ms": int(_num(s.get("attack_time"), 25)),
                                    "hold_ms": int(_num(s.get("hold_time"), 200)),
                                    "release_ms": int(_num(s.get("release_time"), 150))}
        elif kind == "gain_filter":
            mapped = "gain", {"db": _num(s.get("db"), 0.0)}
        elif kind == "noise_suppress_filter":
            mapped = "noise_suppression", {"amount_db": abs(_num(s.get("suppress_level"), -30.0)) or 12.0}
        elif kind in ("chroma_key_filter", "chroma_key_filter_v2"):
            key = _KEY_COLORS.get(str(s.get("key_color_type", "green")))
            mapped = "chroma_key", {"key_color": key or obs_color(s.get("key_color", 0xFF00FF00))[:7],
                                    "similarity": int(_num(s.get("similarity"), 400)),
                                    "smoothness": int(_num(s.get("smoothness"), 80)),
                                    "spill": int(_num(s.get("spill"), 100))}
        elif kind in ("color_filter", "color_filter_v2"):
            opacity = _num(s.get("opacity"), 1.0 if kind.endswith("_v2") else 100)
            mapped = "color_correction", {"gamma": _num(s.get("gamma")), "contrast": _num(s.get("contrast")),
                                          "brightness": _num(s.get("brightness")),
                                          "saturation": _num(s.get("saturation")), "hue_shift": _num(s.get("hue_shift")),
                                          "opacity": int(opacity * 100 if kind.endswith("_v2") else opacity)}
        elif kind in ("sharpness_filter", "sharpness_filter_v2"):
            mapped = "sharpen", {"sharpness": _num(s.get("sharpness"), 0.08)}
        elif kind == "scroll_filter":
            mapped = "scroll", {"speed_x": _num(s.get("speed_x")), "speed_y": _num(s.get("speed_y"))}
        if mapped is None:
            notes.append(f"{obs.get('name')}: the {f.get('name') or kind} filter has no match yet.")
            continue
        out.append(Filter(id=f"f-{n}", kind=mapped[0], name=str(f.get("name") or mapped[0]),
                          enabled=bool(f.get("enabled", True)), settings=mapped[1]))
    return out


# Scenes -----------------------------------------------------------------------------------------------


def _canvas_sizes(data: dict[str, Any], vertical_uuid: str | None) -> dict[str, tuple[float, float]]:
    """OBS's canvas sizes: the main one from "resolution" (or the items' scale_ref), the second from items."""
    sizes: dict[str, tuple[float, float]] = {}
    res = data.get("resolution")
    if isinstance(res, dict) and _num(res.get("x")) > 0:
        sizes["horizontal"] = (_num(res.get("x")), _num(res.get("y")))
    for src in data.get("sources", []) + data.get("groups", []):
        if src.get("id") not in ("scene", "group"):
            continue
        canvas = "vertical" if vertical_uuid and src.get("canvas_uuid") == vertical_uuid else "horizontal"
        for item in (src.get("settings") or {}).get("items", []):
            ref = item.get("scale_ref")
            if canvas not in sizes and isinstance(ref, dict) and _num(ref.get("x")) > 0:
                sizes[canvas] = (_num(ref.get("x")), _num(ref.get("y")))
    sizes.setdefault("horizontal", (1920.0, 1080.0))
    sizes.setdefault("vertical", (1080.0, 1920.0))
    return sizes


def from_obs(data: dict[str, Any], like: SceneCollection, size_of: SizeOf | None = None,
             audio_devices: list[str] | None = None) -> tuple[SceneCollection, list[str]]:
    """An OBS scene collection (its .json) as a garden on the canvases of `like`."""
    if detect(data) != "obs":
        raise ConvertError("That isn't an OBS scene collection.")
    size_of = size_of or file_size_of
    notes: list[str] = []
    canvases = [c.model_copy(deep=True) for c in like.canvases]
    canvas_ids = [c.id for c in canvases]
    tall = next((c.id for c in canvases if c.height > c.width), None)
    wide = next((c.id for c in canvases if c.width >= c.height), canvas_ids[0])
    second = [c for c in data.get("canvases") or [] if isinstance(c, dict)]
    vertical_uuid = str((second[0].get("info") or second[0]).get("uuid", "")) if second else None
    obs_sizes = _canvas_sizes(data, vertical_uuid)
    col = SceneCollection(canvases=canvases, audio=like.audio.model_copy(deep=True))
    keep = set(like.global_device_ids())  # your own mic and desktop sound come along
    col.sources = [s.model_copy(deep=True) for s in like.sources if s.id in keep]

    raw = [s for s in data.get("sources", []) if isinstance(s, dict)]
    groups = [g for g in data.get("groups", []) if isinstance(g, dict)]
    by_uuid = {str(s.get("uuid")): s for s in raw + groups if s.get("uuid")}
    by_name = {str(s.get("name")): s for s in raw + groups}

    def obs_ref(item: dict[str, Any]) -> dict[str, Any] | None:
        return by_uuid.get(str(item.get("source_uuid", ""))) or by_name.get(str(item.get("name", "")))

    scenes = [s for s in raw if s.get("id") == "scene"] + groups
    nested: set[int] = set()
    for scene in scenes:
        for item in (scene.get("settings") or {}).get("items", []):
            ref = obs_ref(item)
            if ref is not None and ref.get("id") in ("scene", "group"):
                nested.add(id(ref))

    # Folders from dividers, in the order OBS lists scenes.
    listed = [str(o.get("name")) for o in data.get("scene_order", []) if isinstance(o, dict)]
    order = listed + [str(s.get("name")) for s in scenes if str(s.get("name")) not in listed]
    folder_of: dict[str, str] = {}
    current_divider: str | None = None
    dividers: set[str] = set()
    for name in listed:  # only OBS's own list (a second canvas's scenes aren't in it)
        scene = by_name.get(name)
        if scene is None or scene.get("id") != "scene":
            continue
        items = (scene.get("settings") or {}).get("items", [])
        label = divider_name(name)
        if not items and label and id(scene) not in nested:
            current_divider = label
            dividers.add(name)
            continue
        if current_divider:
            folder_of[name] = current_divider

    # A divider's folder where most scenes are used inside others holds sprigs: its loose ones are too
    # (OBS users often keep spare overlay scenes there that nothing shows yet).
    sprig_folders: set[str] = set()
    for label in set(folder_of.values()):
        members = [by_name[n] for n, f in folder_of.items() if f == label and n in by_name]
        if members and sum(id(m) in nested for m in members) * 2 >= len(members) and \
                any(id(m) in nested for m in members):
            sprig_folders.add(label)
    # A second canvas's "X (Vertical)" follows X: a sprig if X is one, in X's folder.
    main_kind: dict[str, bool] = {}  # name -> is a sprig, for main-canvas scenes
    for sc in scenes:
        name = str(sc.get("name"))
        if not (vertical_uuid and sc.get("canvas_uuid") == vertical_uuid):
            main_kind[name] = id(sc) in nested or sc.get("id") == "group" or folder_of.get(name) in sprig_folders
    twin_of: dict[str, str] = {}
    for sc in scenes:
        name = str(sc.get("name"))
        if vertical_uuid and sc.get("canvas_uuid") == vertical_uuid and strip_vertical(name) in main_kind:
            twin_of[name] = strip_vertical(name)
            if strip_vertical(name) in folder_of:
                folder_of[name] = folder_of[strip_vertical(name)]
    rank = {name: i for i, name in enumerate(order)}
    scenes.sort(key=lambda sc: rank.get(str(sc.get("name")), len(rank)))

    # Sources (not scenes).
    sources: dict[int, SourceConfig] = {}
    for obs in raw:
        if obs.get("id") in ("scene", "group"):
            continue
        config = _source(obs, audio_devices, notes)
        if config is None:
            continue
        config.id = col.new_id(_slug_prefix(config.kind))
        col.sources.append(config)
        sources[id(obs)] = config
    _merge_sounds(col, sources, keep, notes)

    # Scenes and groups: ids first, so items can point at any of them.
    made: dict[int, Scene] = {}
    canvas_of: dict[int, str] = {}
    for obs in scenes:
        name = str(obs.get("name"))
        if name in dividers:
            continue
        vertical = bool(vertical_uuid) and obs.get("canvas_uuid") == vertical_uuid
        if vertical and tall is None:
            notes.append(f"{name}: SprigCast's vertical canvas is off, so it's on the horizontal one.")
        canvas = tall if vertical and tall else wide
        is_sprig = id(obs) in nested or obs.get("id") == "group" or folder_of.get(name) in sprig_folders \
            or main_kind.get(twin_of.get(name, ""), False)
        kind = "part" if is_sprig else "show"
        scene = Scene(id=col.new_id("bloom" if kind == "show" else "sprig"), name=name, canvas=canvas, kind=kind)
        col.scenes.append(scene)
        made[id(obs)] = scene
        canvas_of[id(obs)] = "vertical" if vertical else "horizontal"
    placements: dict[str, SourceConfig] = {}

    def placement(part: Scene) -> SourceConfig:
        if part.id not in placements:
            config = SourceConfig(id=col.new_id("part-src"), kind=PART_KIND, name=part.name, settings={"scene": part.id})
            col.sources.append(config)
            placements[part.id] = config
        return placements[part.id]

    for obs in scenes:
        scene = made.get(id(obs))
        if scene is None:
            continue
        obs_w, obs_h = obs_sizes[canvas_of[id(obs)]]
        target = col.canvas(scene.canvas)
        k = min(target.width / obs_w, target.height / obs_h) if obs_w and obs_h else 1.0
        for item in (obs.get("settings") or {}).get("items", []):
            if item.get("group_item_backup"):
                continue  # a group's insides, copied here for old OBS versions
            ref = obs_ref(item)
            if ref is None:
                continue
            if id(ref) in made:
                inner = made[id(ref)]
                if inner.id == scene.id:
                    continue
                config = placement(inner)
                inner_w, inner_h = obs_sizes[canvas_of[id(ref)]]
                if ref.get("id") == "group":
                    gs = ref.get("settings") or {}
                    inner_w, inner_h = _num(gs.get("cx"), inner_w), _num(gs.get("cy"), inner_h)
                size: tuple[float, float] | None = (inner_w, inner_h)
            elif id(ref) in sources:
                config = sources[id(ref)]
                size = _size(config, size_of, (obs_w, obs_h))
            else:
                continue
            scene.items.append(_item(item, config.id, size, k, notes, config.name))

    # Blooms: "X (Vertical)" switches with "X"; the twin keeps the plain name (its canvas says which).
    shows = [s for s in col.scenes if s.kind == "show"]
    wide_names = {s.name: s for s in shows if s.canvas == wide}
    for show in shows:
        if show.canvas != wide and tall is not None:
            plain = strip_vertical(show.name)
            twin = wide_names.get(plain)
            if twin is not None:
                show.linked_to = twin.id
                show.name = plain

    # Folders.
    folders: dict[tuple[str, str, str], SceneFolder] = {}
    for obs in scenes:
        scene = made.get(id(obs))
        label = folder_of.get(str(obs.get("name")))
        if scene is None or label is None or scene.shape_of is not None:
            continue
        key = (label, scene.kind, scene.canvas if scene.kind == "show" else "")
        if key not in folders:
            folders[key] = col.add_folder(scene.canvas, label, holds=scene.kind)
        scene.folder = folders[key].id

    # On air: OBS's program scene, else the first bloom on each canvas.
    current = by_name.get(str(data.get("current_program_scene") or data.get("current_scene") or ""))
    first = made.get(id(current)) if current is not None else None
    for canvas_id in canvas_ids:
        on = first if first is not None and first.kind == "show" and first.canvas == canvas_id else None
        if on is None and first is not None:
            on = next((s for s in shows if s.canvas == canvas_id and s.linked_to == first.id), None)
        on = on or next((s for s in shows if s.canvas == canvas_id), None)
        if on is None:
            on = Scene(id=col.new_id("bloom"), name="Bloom", canvas=canvas_id)
            col.scenes.append(on)
        col.active_scenes[canvas_id] = on.id

    _transitions(data, col, by_name, made, notes)
    _hotkeys(raw, made, sources, col)
    col.wrap_blooms()
    sort_by_content(col)
    plays_in(col)
    if not any(s.kind == "show" for s in col.scenes):
        raise ConvertError("That scene collection has no scenes.")
    col = SceneCollection.model_validate(col.model_dump())
    return col, _dedupe(notes)


# A sprig no divider sorted goes in the folder for what most of it is (ties: earlier in this list wins).
CONTENT_FOLDERS = (
    ("Capture Scenes", {"display_capture", "window_capture", "game_capture", "camera"}),
    ("Browser Scenes", {"browser", "chat"}),
    ("Media Scenes", {"media", "image", "text", "color"}),
    ("Audio Scenes", {"audio_input", "audio_output", "app_audio"}),
    ("Nested Scenes", {PART_KIND}),
)
MAIN_FOLDER = "Main Scenes"


def content_folder(kinds: list[str]) -> str | None:
    """The folder name for a sprig holding sources of these kinds, or None when it's empty."""
    counts = [(sum(k in members for k in kinds), -i, name) for i, (name, members) in enumerate(CONTENT_FOLDERS)]
    best = max(counts)
    return best[2] if best[0] else None


def sort_by_content(col: SceneCollection) -> None:
    """Blooms no divider sorted go in Main Scenes; sprigs in Browser, Media, Audio, Capture or Nested Scenes
    by what they hold. Folders that already have the name are reused."""
    kinds = {c.id: c.kind for c in col.sources}

    def folder(name: str, scene: Scene) -> SceneFolder:
        holds = scene.kind
        found = next((f for f in col.folders if f.name == name and f.holds == holds
                      and (holds == "part" or f.canvas == scene.canvas)), None)
        return found or col.add_folder(scene.canvas, name, holds=holds)

    for scene in list(col.scenes):
        if scene.folder is not None or scene.shape_of is not None:
            continue
        if scene.kind == "show":
            scene.folder = folder(MAIN_FOLDER, scene).id
            continue
        name = content_folder([kinds.get(i.source, "") for i in scene.items])
        if name is not None:
            scene.folder = folder(name, scene).id


def _merge_sounds(col: SceneCollection, sources: dict[int, SourceConfig], globals_: set[str],
                  notes: list[str]) -> None:
    """Sounds the import would double: OBS's mic (or desktop sound) on the default device, or the only mic
    it has when that device isn't on this PC, becomes your Mic (or Desktop audio) from Settings → Audio,
    with OBS's volume, mute and filters. Other sounds whose device isn't here start muted."""
    mine = {c.kind: c for c in col.sources if c.id in globals_ and c.kind in ("audio_input", "audio_output")}
    imported = [c for c in sources.values() if c.kind in ("audio_input", "audio_output")]
    mics = [c for c in imported if c.kind == "audio_input"]
    for config in imported:
        unmatched = bool(config.settings.pop("unmatched", False))
        target = mine.get(config.kind)
        merge = target is not None and (config.settings["device"] == "default" and not unmatched
                                         or unmatched and config.kind == "audio_input" and len(mics) == 1)
        if merge:
            target.settings["volume_db"] = config.settings.get("volume_db", 0.0)
            target.settings["muted"] = config.settings.get("muted", False)
            if config.filters:
                target.filters = config.filters
            for key, value in list(sources.items()):
                if value is config:
                    sources[key] = target
            col.sources.remove(config)
            mine.pop(config.kind)  # one OBS sound per device of yours
            notes.append(f"{config.name}: uses your {target.name} from Settings → Audio.")
        elif unmatched:
            config.settings["muted"] = True
            notes.append(f"{config.name}: muted until you pick its sound device (that device wasn't found).")


def plays_in(col: SceneCollection) -> None:
    """In OBS a sound plays only while a scene holding it is on air: Plays in picks the same blooms.
    A sound in every bloom (or in none) plays everywhere, as before."""
    kinds = {c.id: c for c in col.sources}
    reach: dict[str, set[str]] = {}

    def inside(scene: Scene, seen: frozenset = frozenset()) -> set[str]:
        out: set[str] = set()
        for layout in [scene, *col.shapes(scene)] if scene.kind == "part" else [scene]:
            for item in layout.items:
                config = kinds.get(item.source)
                if config is None:
                    continue
                out.add(config.id)
                part_id = config.settings.get("scene") if config.kind == PART_KIND else None
                if part_id and part_id not in seen:
                    try:
                        out |= inside(col.scene(part_id), seen | {part_id})
                    except StopIteration:
                        pass
        return out

    blooms = col.bloom_choices()
    for bloom in blooms:
        reach[bloom.id] = inside(bloom).union(*(inside(t) for t in col.linked_with(bloom.id) if t.id != bloom.id))
    globals_ = set(col.global_device_ids())  # your own Mic and Desktop audio keep playing everywhere
    for config in col.sources:
        if config.kind not in ("audio_input", "audio_output", "app_audio") or config.settings.get("blooms") \
                or config.id in globals_:
            continue
        where = [b.id for b in blooms if config.id in reach[b.id]]
        if where and len(where) < len(blooms):
            config.settings["blooms"] = where


_OBS_KEYS = {"SPACE": "Space", "RETURN": "Return", "ENTER": "Enter", "ESCAPE": "Esc", "TAB": "Tab",
             "BACKSPACE": "Backspace", "INSERT": "Ins", "DELETE": "Del", "HOME": "Home", "END": "End",
             "PAGEUP": "PgUp", "PAGEDOWN": "PgDown", "LEFT": "Left", "RIGHT": "Right", "UP": "Up", "DOWN": "Down",
             "PAUSE": "Pause", "PRINT": "Print", "SCROLLLOCK": "ScrollLock", "MINUS": "-", "EQUAL": "=",
             "COMMA": ",", "PERIOD": ".", "SLASH": "/", "SEMICOLON": ";", "APOSTROPHE": "'",
             "BRACKETLEFT": "[", "BRACKETRIGHT": "]", "BACKSLASH": "\\", "QUOTELEFT": "`"}


def obs_key(binding: dict[str, Any]) -> str | None:
    """One OBS hotkey binding ({"key": "OBS_KEY_F1", "control": true}) as Qt's "Ctrl+F1"."""
    key = str(binding.get("key", "")).removeprefix("OBS_KEY_")
    if key.startswith("NUM") and key[3:].isdigit():
        key = key[3:]
    if len(key) == 1 and key.isalnum() or key.startswith("F") and key[1:].isdigit():
        name = key
    else:
        name = _OBS_KEYS.get(key)
    if not name:
        return None
    mods = [label for flag, label in (("control", "Ctrl"), ("alt", "Alt"), ("shift", "Shift"), ("command", "Meta"))
            if binding.get(flag)]
    return "+".join([*mods, name])


def _hotkeys(raw: list[dict[str, Any]], made: dict[int, Scene], sources: dict[int, SourceConfig],
             col: SceneCollection) -> None:
    """OBS's scene keys show the bloom; a sound's mute and unmute keys come along too."""
    taken: set[str] = set()

    def first(bindings: Any) -> str | None:
        for binding in bindings if isinstance(bindings, list) else []:
            combo = obs_key(binding) if isinstance(binding, dict) else None
            if combo and combo not in taken:
                taken.add(combo)
                return combo
        return None

    for obs in raw:
        keys = obs.get("hotkeys") or {}
        scene = made.get(id(obs))
        if scene is not None and scene.kind == "show":
            combo = first(keys.get("OBSBasic.SelectScene"))
            if combo:
                col.hotkeys[f"scene:{scene.id}"] = combo
        config = sources.get(id(obs))
        if config is not None and config.kind in ("audio_input", "audio_output", "app_audio"):
            for obs_name, what in (("libobs.mute", "mute"), ("libobs.unmute", "unmute")):
                combo = first(keys.get(obs_name))
                if combo:
                    col.hotkeys[f"sound:{config.id}:{what}"] = combo


AUDIO_IDS = {"wasapi_input_capture", "wasapi_output_capture", "coreaudio_input_capture", "coreaudio_output_capture",
             "pulse_input_capture", "pulse_output_capture"}


def _slug_prefix(kind: str) -> str:
    return {"audio_input": "mic", "audio_output": "desktop", "display_capture": "display",
            "window_capture": "window", "game_capture": "game"}.get(kind, kind.replace("_", "-"))


def _dedupe(notes: list[str]) -> list[str]:
    seen: list[str] = []
    for note in notes:
        if note not in seen:
            seen.append(note)
    return seen


def _size(config: SourceConfig, size_of: SizeOf, canvas: tuple[float, float]) -> tuple[float, float] | None:
    s = config.settings
    if config.kind in ("browser", "color"):
        return _num(s.get("width"), 800), _num(s.get("height"), 600)
    if config.kind == "camera":
        return _num(s.get("width"), 1920), _num(s.get("height"), 1080)
    if config.kind in ("display_capture", "window_capture", "game_capture"):
        return canvas
    if config.kind in ("image", "media", "text"):
        found = size_of(config.kind, s)
        return (float(found[0]), float(found[1])) if found else None
    return None


def _item(item: dict[str, Any], source_id: str, size: tuple[float, float] | None, k: float, notes: list[str],
          name: str) -> SceneItem:
    """One OBS scene item. OBS's pos is the anchor point; SprigCast keeps the box's top-left corner."""
    px, py = _xy(item.get("pos"))
    sx, sy = _xy(item.get("scale"), (1.0, 1.0))
    sx, sy = abs(sx) or 1.0, abs(sy) or 1.0
    crop = tuple(max(0, int(_num(item.get(f"crop_{side}")))) for side in ("left", "top", "right", "bottom"))
    align = _ALIGN.get(int(_num(item.get("align"), 5)), "top_left")
    bounds = _BOUNDS.get(int(_num(item.get("bounds_type"), 0)), "off")
    bw, bh = _xy(item.get("bounds"))
    if bounds != "off" and (bw <= 0 or bh <= 0):
        bounds = "off"
    out = SceneItem(source=source_id, scale=sx * k, scale_y=sy * k if abs(sx - sy) > 1e-6 else None,
                    rotation=_num(item.get("rot")), align=align, crop=crop, bounds=bounds,
                    bounds_size=(bw * k, bh * k) if bounds != "off" else (0.0, 0.0),
                    bounds_align=_ALIGN.get(int(_num(item.get("bounds_align"), 0)), "center"),
                    visible=bool(item.get("visible", True)), locked=bool(item.get("locked", False)))
    if bounds != "off":
        w, h = bw, bh
    elif size is not None:
        w, h = max(0.0, size[0] - crop[0] - crop[2]) * sx, max(0.0, size[1] - crop[1] - crop[3]) * sy
    else:
        w = h = 0.0
        if align != "top_left":
            notes.append(f"{name}: check where it sits (its size wasn't known).")
    ax, ay = (_ALIGN_SHARE[align])
    out.x, out.y = (px - w * ax) * k, (py - h * ay) * k
    return out


_ALIGN_SHARE = {a: ((i % 3) / 2, (i // 3) / 2) for i, a in enumerate(
    ("top_left", "top", "top_right", "left", "center", "right", "bottom_left", "bottom", "bottom_right"))}


def _transition(obs: dict[str, Any] | None, duration: int, notes: list[str]) -> Transition | None:
    if obs is None:
        return None
    kind = str(obs.get("id", ""))
    s = obs.get("settings") or {}
    if kind == "cut_transition":
        return Transition(kind="cut", duration_ms=0)
    if kind in ("fade_transition", "fade_to_color_transition"):
        return Transition(kind="fade", duration_ms=duration)
    if kind in ("slide_transition", "swipe_transition"):
        direction = str(s.get("direction", "left"))
        return Transition(kind="slide" if kind.startswith("slide") else "swipe", duration_ms=duration,
                          direction=direction if direction in ("left", "right", "up", "down") else "left")
    if kind == "wipe_transition":
        return Transition(kind="wipe", duration_ms=duration)
    if kind == "obs_stinger_transition":
        point = int(_num(s.get("transition_point"), 500))
        matte = "right" if s.get("track_matte_layout", 0) == 0 else "below"
        return Transition(kind="stinger", duration_ms=max(duration, point),
                          stinger=Stinger(path=str(s.get("path", "")), cut_ms=point,
                                          matte=matte if s.get("track_matte_enabled") else "none"))
    notes.append(f"{obs.get('name') or kind}: that transition has no match yet (using fade).")
    return Transition(kind="fade", duration_ms=duration)


def _transitions(data: dict[str, Any], col: SceneCollection, by_name: dict[str, dict], made: dict[int, Scene],
                 notes: list[str]) -> None:
    named = {str(t.get("name")): t for t in data.get("transitions", []) if isinstance(t, dict)}
    builtin = {"Fade": {"id": "fade_transition", "name": "Fade"}, "Cut": {"id": "cut_transition", "name": "Cut"}}
    duration = int(_num(data.get("transition_duration"), 300))
    current = str(data.get("current_transition", "Fade"))
    col.transition = _transition(named.get(current) or builtin.get(current), duration, notes) or Transition()
    for obs in [*data.get("sources", []), *data.get("groups", [])]:
        scene = made.get(id(obs)) if isinstance(obs, dict) else None
        override = (obs.get("private_settings") or {}) if scene is not None else {}
        name = override.get("transition")
        if scene is None or scene.kind != "show" or not name:
            continue
        found = _transition(named.get(name) or builtin.get(name),
                            int(_num(override.get("transition_duration"), duration)), notes)
        if found is not None:
            col.transition_rules.append(TransitionRule(from_scene=ANY, to_scene=scene.id, transition=found))


# Streamlabs -------------------------------------------------------------------------------------------


def streamlabs_to_obs(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """A Streamlabs Desktop scene collection rewritten in OBS's shape (see from_obs).

    Streamlabs keeps sources with OBS's own kinds and settings, and scene items as top-left x and y
    with a scale; a dual-output item for the vertical canvas is marked display "vertical"."""
    notes: list[str] = []
    res = data.get("baseResolutions") or {}
    base = data.get("baseResolution") or res.get("horizontal") or {}
    vbase = res.get("vertical") or {"baseWidth": 1080, "baseHeight": 1920}
    sources = []
    for s in (data.get("sources") or {}).get("items", []):
        if not isinstance(s, dict):
            continue
        filters = [{"id": f.get("type"), "name": f.get("name"), "enabled": f.get("enabled", True),
                    "settings": f.get("settings") or {}} for f in (s.get("filters") or {}).get("items", [])]
        sources.append({"id": s.get("type"), "name": s.get("name"), "uuid": s.get("id"),
                        "settings": s.get("settings") or {}, "volume": s.get("volume", 1.0),
                        "muted": s.get("muted", False), "filters": filters})
        if s.get("propertiesManager") == "widget":
            notes.append(f"{s.get('name')}: a Streamlabs widget; it comes over as its browser page.")
    vertical_uuid = "streamlabs-vertical"
    scenes, order = [], []
    for sc in (data.get("scenes") or {}).get("items", []):
        if not isinstance(sc, dict):
            continue
        by_display: dict[str, list[dict]] = {"horizontal": [], "vertical": []}
        for it in (sc.get("sceneItems") or {}).get("items", []):
            if not isinstance(it, dict) or it.get("sceneNodeType", "item") != "item":
                continue
            crop = it.get("crop") or {}
            by_display["vertical" if it.get("display") == "vertical" else "horizontal"].append({
                "source_uuid": it.get("sourceId"), "pos": {"x": it.get("x", 0), "y": it.get("y", 0)},
                "scale": {"x": it.get("scaleX", 1), "y": it.get("scaleY", 1)}, "rot": it.get("rotation", 0),
                "visible": it.get("visible", True), "locked": it.get("locked", False), "align": 5,
                **{f"crop_{k}": crop.get(k, 0) for k in ("left", "top", "right", "bottom")}})
        scenes.append({"id": "scene", "name": sc.get("name"), "uuid": sc.get("id"),
                       "settings": {"items": by_display["horizontal"]}})
        order.append({"name": sc.get("name")})
        if by_display["vertical"]:
            scenes.append({"id": "scene", "name": f"{sc.get('name')} (Vertical)", "uuid": f"{sc.get('id')}-v",
                           "canvas_uuid": vertical_uuid, "settings": {"items": by_display["vertical"]}})
    current = next((sc.get("name") for sc in (data.get("scenes") or {}).get("items", [])
                    if isinstance(sc, dict) and sc.get("active")), None)
    obs = {"name": "Streamlabs", "sources": sources + scenes, "groups": [], "scene_order": order,
           "current_scene": current, "current_program_scene": current,
           "resolution": {"x": base.get("baseWidth", 1920), "y": base.get("baseHeight", 1080)},
           "canvases": [{"info": {"name": "Vertical", "uuid": vertical_uuid}}] if any(
               s.get("canvas_uuid") for s in scenes) else [],
           "transitions": [], "current_transition": "Fade", "transition_duration": 300}
    for s in scenes:
        if s.get("canvas_uuid"):
            for it in s["settings"]["items"]:
                it["scale_ref"] = {"x": vbase.get("baseWidth", 1080), "y": vbase.get("baseHeight", 1920)}
    return obs, notes


# Meld -------------------------------------------------------------------------------------------------


def meld_to_obs(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """A Meld Studio session (File → Export Session) in OBS's shape. Meld doesn't publish its file
    format; this reads the fields its own API uses: scenes, and layers with x, y, width and height."""
    notes: list[str] = []
    items = data.get("items") if isinstance(data.get("items"), dict) else (data.get("session") or {}).get("items", {})
    scenes = sorted(((k, v) for k, v in items.items() if isinstance(v, dict) and v.get("type") == "scene"),
                    key=lambda kv: _num(kv[1].get("index")))
    layers = [(k, v) for k, v in items.items() if isinstance(v, dict) and v.get("type") == "layer"]
    sources, obs_scenes = [], []
    for key, layer in layers:
        name = str(layer.get("name") or "Layer")
        if layer.get("url"):
            sources.append({"id": "browser_source", "name": name, "uuid": key,
                            "settings": {"url": layer["url"], "width": layer.get("width", 1920),
                                         "height": layer.get("height", 1080)}})
        elif layer.get("source"):
            path = str(layer["source"])
            is_video = Path(path).suffix.lower() in (".mp4", ".mov", ".webm", ".mkv", ".mp3", ".wav", ".ogg")
            sources.append({"id": "ffmpeg_source" if is_video else "image_source", "name": name, "uuid": key,
                            "settings": {"local_file": path, "looping": True} if is_video else {"file": path}})
        else:
            notes.append(f"{name}: Meld layers other than pictures, videos and pages don't come over yet.")
    for key, scene in scenes:
        mine = sorted((lv for lk, lv in layers if lv.get("parent") == key), key=lambda lv: _num(lv.get("index")))
        keys = {id(lv): lk for lk, lv in layers}
        obs_items = []
        for layer in reversed(mine):  # Meld lists the top layer first; OBS the bottom one
            obs_items.append({"source_uuid": keys[id(layer)], "pos": {"x": layer.get("x", 0), "y": layer.get("y", 0)},
                              "rot": layer.get("rotation", 0), "visible": layer.get("visible", True), "align": 5,
                              "bounds_type": 1, "bounds": {"x": layer.get("width", 0), "y": layer.get("height", 0)}})
        obs_scenes.append({"id": "scene", "name": scene.get("name") or "Scene", "uuid": key,
                           "settings": {"items": obs_items}})
    current = next((v.get("name") for _k, v in scenes if v.get("current")), None)
    return {"name": "Meld", "sources": sources + obs_scenes, "groups": [],
            "scene_order": [{"name": s["name"]} for s in obs_scenes], "current_scene": current,
            "current_program_scene": current, "transitions": [], "current_transition": "Fade",
            "transition_duration": 300}, notes


def scene_collection_files(appdata: Path) -> dict[str, list[Path]]:
    """Scene collections the other apps keep on this PC (Windows %APPDATA%), by app, A-Z."""
    found: dict[str, list[Path]] = {}
    obs = appdata / "obs-studio" / "basic" / "scenes"
    if obs.is_dir():
        found["obs"] = sorted(obs.glob("*.json"), key=lambda p: p.stem.casefold())
    slobs = appdata / "slobs-client" / "SceneCollections"
    if slobs.is_dir():
        found["streamlabs"] = sorted((p for p in slobs.glob("*.json") if p.name != "manifest.json"),
                                     key=lambda p: p.stem.casefold())
    return {k: v for k, v in found.items() if v}


def streamlabs_names(folder: Path) -> dict[str, str]:
    """Streamlabs names its files by id; manifest.json has the names you gave them."""
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8-sig"))
        return {str(c["id"]): str(c["name"]) for c in manifest.get("collections", [])
                if isinstance(c, dict) and not c.get("deleted")}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def guess_name(path: Path, data: Any) -> str:
    """A name for the new garden: the collection's own name, else the file's."""
    if isinstance(data, dict) and isinstance(data.get("name"), str) and data["name"].strip():
        return data["name"].strip()
    if path.parent.name == "SceneCollections":
        names = streamlabs_names(path.parent)
        if path.stem in names:
            return names[path.stem]
    return path.stem.replace("_", " ")
