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
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sprigcast.core.scene import (  # noqa: I001
    new_filter_id,
    ANY,
    PART_KIND,
    Filter,
    Scene,
    SceneCollection,
    SceneFolder,
    SceneItem,
    SOUND_KINDS,
    SourceConfig,
    Stinger,
    Transition,
    TransitionRule,
)

from sprigcast.core.looks import FROM_FILE as LUT_FROM_FILE  # noqa: E402

MAX_BYTES = 64 * 1024 * 1024  # a scene collection bigger than this isn't one
PAGES_SLEEP_NOTE = ("Web pages: each sleeps while not on screen (OBS kept them all running). A page that must keep "
                    "running has a switch for it in its settings.")
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
    session = data.get("session") if isinstance(data.get("session"), dict) else data
    if isinstance(session.get("scenes"), list) and ("audioTracks" in session or "verticalScenes" in session
                                                     or any(isinstance(sc, dict) and "childItems" in sc
                                                            for sc in session["scenes"])):
        return "meld"
    return ""


APP_NAMES = {"obs": "OBS", "streamlabs": "Streamlabs", "meld": "Meld"}

# What the last import couldn't carry over, as "source:vlc_source"-style keys with counts: the plugin and type ids
# only, never names or paths, so it can go in the logbook (core/privacy.py) and show which matches to build next.
MISSED: Counter[str] = Counter()


def _missed(what: str, kind: Any) -> None:
    MISSED[f"{what}:{str(kind or 'unknown')[:60]}"] += 1


def missed_summary() -> str:
    """'filter:move_source_filter x3, source:vlc_source x1' (most first), or '' when everything came over."""
    return ", ".join(f"{key} x{count}" for key, count in MISSED.most_common())


def convert(data: Any, like: SceneCollection, size_of: SizeOf | None = None,
            audio_devices: list[str] | None = None) -> tuple[SceneCollection, list[str]]:
    """Any supported file's data -> (garden, notes about what didn't come over). MISSED says the same for the log."""
    MISSED.clear()
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
    elif kind in ("slideshow", "slideshow_v2"):  # the Slide show plugin (Plugin store) plays a folder
        files = [str(f.get("value", "")) for f in s.get("files") or [] if isinstance(f, dict) and f.get("value")]
        folders = {f if not Path(f).suffix else str(Path(f).parent) for f in files}
        out = "slideshow", {"folder": sorted(folders)[0] if folders else "",
                            "seconds": max(1, round(_num(s.get("slide_time"), 8000) / 1000)),
                            "shuffle": bool(s.get("randomize", False)), "loop": bool(s.get("loop", True)),
                            **_slide_box(s.get("use_custom_size"))}
        if len(folders) > 1:
            notes.append(f"{name}: OBS played pictures from {len(folders)} folders; the Slide show plays one, "
                         "so put them in one folder.")
            _missed("source", "slideshow:several_folders")
    elif kind == "ffmpeg_source":
        if s.get("is_local_file", True) is False:
            notes.append(f"{name}: a network stream (not a file) isn't supported yet.")
            _missed("source", "ffmpeg_source:network")
            return None
        out = "media", {"path": str(s.get("local_file", "")), "loop": bool(s.get("looping", False)),
                        "restart_on_show": bool(s.get("restart_on_activate", True)),
                        "clear_on_end": bool(s.get("clear_on_media_end", False))}
    elif kind == "browser_source":
        local = bool(s.get("is_local_file", False))
        settings: dict[str, Any] = {"url": "https://" if local else str(s.get("url", "https://")),
                                    "file": str(s.get("local_file", "")) if local else "",
                                    "width": int(_num(s.get("width"), 800)), "height": int(_num(s.get("height"), 600)),
                                    # Never kept running: OBS runs every page all the time unless told otherwise
                                    # (its default), and carried over that kept all 61 pages of a garden awake
                                    # (PC, 2026-11-19). The page's own switch turns it back on for one that needs it.
                                    "keep_running": False,
                                    "reload_on_air": bool(s.get("restart_when_active", False)),
                                    "audio_to_mixer": bool(s.get("reroute_audio", False))}
        if not s.get("shutdown", False) and PAGES_SLEEP_NOTE not in notes:
            notes.append(PAGES_SLEEP_NOTE)
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
        if int(_num(s.get("priority"), 0)) == 1:  # OBS's "Window title must match"
            out[1]["match"] = "title"
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
    if out is None and kind == "obs_glow_source":
        notes.append(f"{name}: the Glow plugin's source has no match; add the Glow effect in Filters instead.")
        _missed("source", kind)
        return None
    if out is None:
        notes.append(f"{name}: {kind or 'this kind of source'} has no match in SprigCast yet.")
        _missed("source", kind)
        return None
    sk, settings = out
    if sk in ("audio_input", "audio_output", "app_audio", "media", "browser"):
        settings["volume_db"] = _volume_db(obs.get("volume", 1.0))
        settings["muted"] = bool(obs.get("muted", False))
    if sk in SOUND_SHAPED:
        _sound_details(obs, settings)
    if sk == "camera":  # OBS's Video Delay filter on a camera: the camera's own Video delay (at most 1 s)
        delay = next((f for f in obs.get("filters") or [] if str(f.get("id") or "") in _VIDEO_DELAYS
                      and f.get("enabled", True)), None)
        if delay is not None:
            ms = int(_num((delay.get("settings") or {}).get("delay_ms"), 0))
            if ms > 0:
                settings["delay_ms"] = min(1000, ms)
                if ms > 1000:
                    notes.append(f"{name}: its video delay was {ms} ms; SprigCast's camera delay goes up to 1000 ms.")
    monitoring = int(_num(obs.get("monitoring_type"), 0))  # OBS: 1 = monitor only, 2 = monitor and output
    if sk in ("audio_input", "audio_output", "app_audio", "media", "browser", "camera") and monitoring in (1, 2):
        settings["monitor"] = "only" if monitoring == 1 else True
    return SourceConfig(id="", kind=sk, name=name, settings=settings, filters=_filters(obs, notes))


SOUND_SHAPED = ("audio_input", "audio_output", "app_audio", "media", "browser")
OBS_FORCE_MONO = 1 << 1  # OBS's source flag
OBS_ALL_TRACKS = 0b111111


def _slide_box(custom: Any) -> dict[str, int]:
    """OBS's slide show size: "1280x720", or "Automatic" (the biggest picture; here the usual 1920x1080)."""
    match = re.fullmatch(r"\s*(\d+)\s*x\s*(\d+)\s*", str(custom or ""))
    return {"width": int(match[1]), "height": int(match[2])} if match else {"width": 1920, "height": 1080}


def _sound_details(obs: dict[str, Any], settings: dict[str, Any]) -> None:
    """OBS's Advanced Audio Properties the SprigCast way: mono, balance, sync offset (a delay; SprigCast can't
    play a sound early, so a negative offset is left at 0) and tracks (OBS track 1 is what's streamed and
    recorded; tracks 4 and 5 are SprigCast's own picks; one on no track at all is heard nowhere)."""
    if int(_num(obs.get("flags"))) & OBS_FORCE_MONO:
        settings["mono"] = True
    balance = _num(obs.get("balance"), 0.5)  # OBS: 0 left .. 0.5 centre .. 1 right
    if abs(balance - 0.5) > 1e-3:
        settings["balance"] = round(max(-1.0, min(1.0, (balance - 0.5) * 2)), 3)
    sync_ms = int(round(_num(obs.get("sync")) / 1_000_000))  # nanoseconds
    if sync_ms > 0:
        settings["delay_ms"] = min(2000, sync_ms)
    if "mixers" in obs:
        mixers = int(_num(obs.get("mixers"), OBS_ALL_TRACKS))
        if not mixers & 1:  # off track 1: not in the stream or the recording's main mix
            settings["tracks"] = []
        for bit, track in ((3, "track4"), (4, "track5")):
            if mixers & (1 << bit) and mixers != OBS_ALL_TRACKS:
                settings[track] = True


def _device(name: str, device_id: str, devices: list[str] | None) -> str | None:
    """OBS names sound devices by a Windows id, SprigCast by name: match the source's name to one."""
    clean = re.sub(r"^\s*\[[^\]]*\]\s*", "", name).strip().casefold()  # "[AOC] Voice Chat" -> "voice chat"
    for device in devices or []:
        if clean and (clean in device.casefold() or device.casefold().split(" (")[0] == clean):
            return device
    return None


_KEY_COLORS = {"green": "#00ff00", "blue": "#0000ff", "magenta": "#ff00ff"}


# The Stroke Glow Shadow plugin's filters (OBS) as SprigCast's own Outline, Glow and Shadow. Its settings have
# had a few names over its versions; the first one found is used, else SprigCast's default.
_STROKE_GLOW_SHADOW = {"obs_stroke_filter": "stroke", "stroke_filter": "stroke", "obs_glow_filter": "glow",
                       "glow_filter": "glow", "obs_shadow_filter": "shadow", "shadow_filter": "shadow"}


def _first(s: dict[str, Any], *names: str) -> Any:
    return next((s[n] for n in names if n in s), None)


def _stroke_glow_shadow(kind: str, s: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    mine = _STROKE_GLOW_SHADOW[kind]
    color = _first(s, f"{mine}_color", "color", "fill_color")
    opacity = _first(s, f"{mine}_opacity", "opacity", "fill_opacity")
    out: dict[str, Any] = {}
    if color is not None:
        out["color"] = obs_color(color)[:7]
    if mine == "stroke":
        out["width"] = max(0.5, min(40.0, _num(_first(s, "stroke_size", "size", "width"), 4.0)))
        if opacity is not None:
            out["opacity"] = int(max(0.0, min(100.0, _num(opacity) * (100 if _num(opacity) <= 1 else 1))))
    elif mine == "glow":
        out["size"] = max(1.0, min(60.0, _num(_first(s, "glow_size", "size"), 16.0)))
        strength = _first(s, "intensity", "glow_intensity", "strength")
        if strength is not None:
            out["strength"] = int(max(0.0, min(100.0, _num(strength) * (100 if _num(strength) <= 1 else 1))))
    else:
        out["offset_x"] = max(-100.0, min(100.0, _num(_first(s, "shadow_offset_x", "offset_x"), 8.0)))
        out["offset_y"] = max(-100.0, min(100.0, _num(_first(s, "shadow_offset_y", "offset_y"), 8.0)))
        out["blur"] = max(0.0, min(60.0, _num(_first(s, "shadow_blur", "blur_size", "blur"), 12.0)))
        if opacity is not None:
            out["opacity"] = int(max(0.0, min(100.0, _num(opacity) * (100 if _num(opacity) <= 1 else 1))))
    return mine, out


_VIDEO_DELAYS = ("async_delay_filter", "gpu_delay")  # Video Delay (async) and Render Delay
_IMAGE_MASKS = ("mask_filter", "mask_filter_v2")  # Image Mask/Blend: an alpha mask becomes the Frame's picture shape
_BY_NAME = "name:"  # a filter naming another sound, until the sound has its SprigCast id
_PICTURE_FILTERS = {"blur", "chroma_key", "color_correction", "sharpen", "scroll", "color_key", "luma_key", "lut", "shape",
                    "stroke", "glow", "shadow"}


def _filters(obs: dict[str, Any], notes: list[str]) -> list[Filter]:
    out: list[Filter] = []
    for n, f in enumerate(obs.get("filters") or [], 1):
        kind = str(f.get("id") or f.get("versioned_id") or "")
        s = f.get("settings") or {}
        mapped: tuple[str, dict[str, Any]] | None = None
        side = str(s.get("sidechain_source") or "none")
        if kind == "compressor_filter" and side.casefold() != "none":  # lowered while another sound plays
            mapped = "ducking", {"sound_id": f"{_BY_NAME}{side}", "threshold_db": _num(s.get("threshold"), -35.0),
                                 "attack_ms": max(1, int(_num(s.get("attack_time"), 60))),
                                 "release_ms": max(1, int(_num(s.get("release_time"), 500)))}
        elif kind == "compressor_filter":
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
        elif kind == "obs_composite_blur":  # the Composite Blur plugin (THSV's pixelate uses it)
            pixelate = int(_num(s.get("blur_algorithm"))) == 4
            mapped = "blur", {"mode": "pixelate" if pixelate else "blur",
                              "size": max(0.5, min(64.0, _num(s.get("radius"), 10.0)))}
        elif kind in ("streamfx-filter-blur", "filter-blur"):
            mapped = "blur", {"mode": "blur", "size": max(0.5, min(64.0, _num(s.get("Filter.Blur.Size"), 5.0)))}
        elif kind == "scroll_filter":
            mapped = "scroll", {"speed_x": _num(s.get("speed_x")), "speed_y": _num(s.get("speed_y"))}
        elif kind in ("color_key_filter", "color_key_filter_v2"):
            key = _KEY_COLORS.get(str(s.get("key_color_type", "green")))
            mapped = "color_key", {"key_color": key or obs_color(s.get("key_color", 0xFF00FF00))[:7],
                                   "similarity": int(_num(s.get("similarity"), 80)),
                                   "smoothness": int(_num(s.get("smoothness"), 50))}
        elif kind in ("luma_key_filter", "luma_key_filter_v2"):
            mapped = "luma_key", {"luma_min": _num(s.get("luma_min"), 0.0), "luma_max": _num(s.get("luma_max"), 1.0),
                                  "smoothness": max(_num(s.get("luma_min_smooth")), _num(s.get("luma_max_smooth")))}
        elif kind == "clut_filter":
            path = str(s.get("image_path", ""))
            if path.lower().endswith(".cube"):
                mapped = "lut", {"look": LUT_FROM_FILE, "file": path,
                                 "amount": int(round(_num(s.get("clut_amount"), 1.0) * 100))}
            else:
                notes.append(f"{obs.get('name')}: the {f.get('name') or 'LUT'} filter uses a picture LUT; "
                             "Mood takes .cube files.")
                _missed("filter", "clut_filter:picture")
                continue
        elif kind == "basic_eq_filter":
            mapped = "eq3", {"low_db": _num(s.get("low")), "mid_db": _num(s.get("mid")), "high_db": _num(s.get("high"))}
        elif kind == "invert_polarity_filter":
            mapped = "invert_polarity", {}
        elif kind == "upward_compressor_filter":
            mapped = "upward_compressor", {"ratio": _num(s.get("ratio"), 1.5),
                                           "threshold_db": _num(s.get("threshold"), -20.0),
                                           "attack_ms": max(1, int(_num(s.get("attack_time"), 10))),
                                           "release_ms": max(1, int(_num(s.get("release_time"), 50)))}
        elif kind in _STROKE_GLOW_SHADOW:
            mapped = _stroke_glow_shadow(kind, s)
        elif kind in _VIDEO_DELAYS and str(obs.get("id") or "") == "dshow_input":
            continue  # carried over as the camera's own Video delay (_source)
        elif kind in _IMAGE_MASKS and "alpha" in str(s.get("type", "")).lower() and s.get("image_path"):
            mapped = "shape", {"shape": "image", "mask_file": str(s.get("image_path"))}
        if mapped is None:
            notes.append(f"{obs.get('name')}: the {f.get('name') or kind} filter has no match yet.")
            _missed("filter", kind)
            continue
        if mapped[0] in _PICTURE_FILTERS and str(obs.get("id") or "") in AUDIO_IDS:
            continue  # a picture filter on a sound does nothing in OBS either
        out.append(Filter(id=new_filter_id(), kind=mapped[0], name=str(f.get("name") or mapped[0]),
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
    named_sound = {str(obs.get("name")): sources[id(obs)].id for obs in raw if id(obs) in sources}
    for config in col.sources:
        for flt in config.filters:
            ref = str(flt.settings.get("sound_id", ""))
            if flt.kind == "ducking" and ref.startswith(_BY_NAME):
                flt.settings["sound_id"] = named_sound.get(ref[len(_BY_NAME):], "")

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
            flip_x, flip_y = flips(item)
            if flip_x and not flip_y and getattr(config, "kind", "") == "camera":
                config.settings["mirror"] = True  # a camera flipped left to right: SprigCast's Mirror
            elif flip_x or flip_y:
                notes.append(f"{config.name}: flipped in OBS; SprigCast can't flip it yet, so it shows unflipped.")
                _missed("item", "flip")
            blend = str(item.get("blend_type") or "normal")
            if blend != "normal":
                notes.append(f"{config.name}: uses OBS's {blend.replace('_', ' ')} blending; it blends normally here.")
                _missed("item", f"blend:{blend}")
            for when in ("show", "hide"):
                moving = item.get(f"{when}_transition")
                if isinstance(moving, dict) and moving.get("id"):
                    notes.append(f"{config.name}: its own {when} transition isn't carried over yet.")
                    _missed("item", f"{when}_transition:{moving.get('id')}")

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
    _scene_filters(scenes, by_name, sources, made, notes)
    _hotkeys(raw, made, sources, col)
    col.wrap_blooms()
    set_aside_unused(col, notes)
    sort_by_content(col)
    plays_in(col)
    drop_sound_sprigs(col, notes)
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
UNUSED_FOLDER = "Unused Scenes"


def unused_parts(col: SceneCollection) -> list[Scene]:
    """Sprigs no bloom reaches, directly or through other sprigs (a twin counts as reached with its main)."""
    by_id = {s.id: s for s in col.scenes}
    placed = {c.id: c.settings.get("scene") for c in col.sources if c.kind == PART_KIND}
    twins: dict[str, list[str]] = {}
    for s in col.scenes:
        if s.shape_of:
            twins.setdefault(s.shape_of, []).append(s.id)
    seen: set[str] = set()
    todo = [s.id for s in col.scenes if s.kind == "show"]
    while todo:
        scene = by_id.get(todo.pop())
        if scene is None or scene.id in seen:
            continue
        seen.add(scene.id)
        todo += [str(placed[i.source]) for i in scene.items if i.source in placed]
        if scene.shape_of:
            todo.append(scene.shape_of)
        todo += twins.get(scene.id, [])
    return [s for s in col.scenes if s.kind == "part" and s.id not in seen]


def set_aside_unused(col: SceneCollection, notes: list[str]) -> None:
    """Sprigs no bloom uses go in Unused Scenes (even from a divider's folder), with one note. Kept,
    not dropped: they're often a kit of widgets the streamer drags in when needed (PC, 2026-11-19: 27 of them)."""
    unused = unused_parts(col)
    if not unused:
        return
    folder = None
    was = {s.folder for s in unused} - {None}
    for scene in unused:
        if scene.shape_of is None:
            folder = folder or next((f for f in col.folders if f.name == UNUSED_FOLDER and f.holds == "part"),
                                    None) or col.add_folder(scene.canvas, UNUSED_FOLDER, holds="part")
            scene.folder = folder.id
    for f in [f for f in col.folders if f.id in was and f is not folder]:
        if not any(s.folder == f.id for s in col.scenes):
            col.folders.remove(f)  # a divider's folder that only held unused sprigs
    mains = sum(s.shape_of is None for s in unused)
    notes.append(f"{mains} unused sprig{'s are' if mains != 1 else ' is'} in {UNUSED_FOLDER}.")


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


def drop_sound_sprigs(col: SceneCollection, notes: list[str]) -> None:
    """OBS's audio scenes (only mics, desktop and app sounds in them) aren't brought in as sprigs: their sounds live
    in the Sound area, and plays_in already set where they play. The sounds stay; the sprigs and their places in
    blooms go (2026-11-21: removing them by hand later would have deleted the sounds with them)."""
    kinds = {c.id: c.kind for c in col.sources}
    dropped = []
    for part in col.parts():
        items = [i for layout in [part, *col.shapes(part)] for i in layout.items]
        if items and all(kinds.get(i.source) in SOUND_KINDS for i in items):
            col.remove_sprig(part)
            dropped.append(part.name)
    if not dropped:
        return
    for folder in [f for f in col.folders if f.holds == "part"]:
        if not any(s.folder == folder.id for s in col.scenes):
            col.folders.remove(folder)
    notes.append(f"{len(dropped)} audio scene{'s' if len(dropped) != 1 else ''} ({', '.join(dropped)}) became "
                 "sounds in the Sound area, playing in the same blooms.")


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
    from sprigcast.core.scene import source_id_prefix

    return {"audio_input": "mic", "audio_output": "desktop"}.get(kind) or source_id_prefix(kind)


def _dedupe(notes: list[str]) -> list[str]:
    """Each note once, what didn't come over at all first: the notes pop-up shows 40, and a garden with many
    muted sounds and pages pushed "Glow: ... has no match" past them (PC, 2026-11-19)."""
    seen: list[str] = []
    for note in notes:
        if note not in seen:
            seen.append(note)
    return sorted(seen, key=lambda n: not _LEFT_OUT.search(n))


_LEFT_OUT = re.compile(r"has no match|isn't supported")


def _size(config: SourceConfig, size_of: SizeOf, canvas: tuple[float, float]) -> tuple[float, float] | None:
    s = config.settings
    if config.kind in ("browser", "color", "slideshow"):
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
    flip_x, flip_y = sx < 0, sy < 0  # OBS flips with a negative scale: the picture then runs the other way from pos
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
    ax, ay = (1 - ax) if flip_x else ax, (1 - ay) if flip_y else ay
    out.x, out.y = (px - w * ax) * k, (py - h * ay) * k
    return out


def flips(item: dict[str, Any]) -> tuple[bool, bool]:
    """(left-right, upside down): OBS's Transform → Flip, kept as a negative scale."""
    sx, sy = _xy(item.get("scale"), (1.0, 1.0))
    return sx < 0, sy < 0


_ALIGN_SHARE = {a: ((i % 3) / 2, (i // 3) / 2) for i, a in enumerate(
    ("top_left", "top", "top_right", "left", "center", "right", "bottom_left", "bottom", "bottom_right"))}


def vertical_twin(path: str) -> str:
    """The stinger's vertical video beside it ("x-vertical.webm" for "x.webm"), or "". OBS's vertical canvas
    plugin keeps its own stinger outside the scene collection (PC, 2026-11-19: the vertical canvas got the
    wide video though hidden-sloth-village-stinger-vertical.webm sat next to it)."""
    if not path:
        return ""
    p = Path(path)
    for tail in ("-vertical", "_vertical", " vertical", " (Vertical)", "-Vertical", "_Vertical", " Vertical"):
        twin = p.with_name(p.stem + tail + p.suffix)
        try:
            if twin.is_file():
                return str(twin).replace("\\", "/") if "/" in path else str(twin)
        except OSError:
            continue
    return ""


def _scene_filters(scenes: list[dict[str, Any]], by_name: dict[str, dict], sources: dict[int, SourceConfig],
                   made: dict[int, Scene], notes: list[str]) -> None:
    """Filters on OBS scenes. A Move filter that only turns one thing in the scene (a barrel roll) becomes a
    Barrel roll on that item there (its own effect: the same source elsewhere doesn't spin, like in OBS), off
    until triggered; anything else is noted, not dropped quietly (PC, 2026-11-19: "THSV Barrel Roll" on the
    Elgato scene went missing without a word)."""
    for scene in scenes:
        for f in scene.get("filters") or []:
            if not isinstance(f, dict):
                continue
            kind, s, label = str(f.get("id", "")), f.get("settings") or {}, f.get("name") or f.get("id")
            target = sources.get(id(by_name.get(str(s.get("source", "")), {})))
            here = made.get(id(scene))
            item = next((i for i in here.items if target is not None and i.source == target.id), None) \
                if here is not None else None
            moves = any(_num((s.get(k) or {}).get(a)) for k in ("pos", "scale", "bounds") for a in ("x", "y"))
            turn = _num(s.get("rot"))
            if kind == "move_source_filter" and item is not None and turn and not moves:
                ms = _num(s.get("duration"), 300) if s.get("custom_duration", True) else 300
                item.effects.append(Filter(
                    id=f"f-{len(item.effects)}", kind="barrel_roll", name=str(label), enabled=False,
                    settings={"seconds": max(0.5, min(120.0, ms / 1000)), "turns": max(1, round(abs(turn) / 360)),
                              "direction": "anticlockwise" if s.get("rot_sign") == "-" else "clockwise"}))
                notes.append(f"{label}: now a Barrel roll on {target.name} in {here.name} (it needs the Barrel Roll "
                             f"plugin).")
                continue
            notes.append(f"{scene.get('name')}: the {label} filter has no match yet.")
            _missed("scene filter", kind)


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
    if kind == "move_transition":  # OBS's Move plugin: SprigCast's own Move does the same job
        return Transition(kind="move", duration_ms=duration)
    if kind == "obs_stinger_transition":
        point = int(_num(s.get("transition_point"), 500))
        matte = "right" if s.get("track_matte_layout", 0) == 0 else "below"
        path = str(s.get("path", ""))
        return Transition(kind="stinger", duration_ms=max(duration, point),
                          stinger=Stinger(path=path, vertical_path=vertical_twin(path), cut_ms=point,
                                          matte=matte if s.get("track_matte_enabled") else "none"))
    notes.append(f"{obs.get('name') or kind}: that transition has no match yet (using fade).")
    _missed("transition", kind)
    return Transition(kind="fade", duration_ms=duration)


def _transitions(data: dict[str, Any], col: SceneCollection, by_name: dict[str, dict], made: dict[int, Scene],
                 notes: list[str]) -> None:
    named = {str(t.get("name")): t for t in data.get("transitions", []) if isinstance(t, dict)}
    builtin = {"Fade": {"id": "fade_transition", "name": "Fade"}, "Cut": {"id": "cut_transition", "name": "Cut"}}
    duration = int(_num(data.get("transition_duration"), 300))
    current = str(data.get("current_transition", "Fade"))
    col.transition = _transition(named.get(current) or builtin.get(current), duration, notes) or Transition()
    # A scene's own transition becomes an "Any -> this bloom" rule. A vertical bloom switches with the main one
    # it's linked to and uses that rule (with the stinger's vertical video), so its own is only needed when the
    # main one has none. Nothing is skipped without a note (PC, 2026-11-20: the user saw one rule of five).
    ruled: set[str] = set()
    later: list[tuple[Scene, dict[str, Any], str]] = []
    for obs in [*data.get("sources", []), *data.get("groups", [])]:
        if not isinstance(obs, dict):
            continue
        override = obs.get("private_settings") or {}
        name = override.get("transition")
        if not name:
            continue
        scene = made.get(id(obs))
        if scene is None or scene.kind != "show":
            notes.append(f"{obs.get('name')}: its own transition ({name}) isn't carried over: it's a sprig here.")
            _missed("sprig transition", "override")
        elif scene.linked_to:
            later.append((scene, override, str(name)))
        else:
            _rule(scene.id, str(name), override, named, builtin, duration, col, ruled, notes)
    for scene, override, name in later:
        if scene.linked_to not in ruled:
            _rule(scene.linked_to, name, override, named, builtin, duration, col, ruled, notes)


def _rule(bloom: str, name: str, override: dict[str, Any], named: dict[str, dict], builtin: dict[str, dict],
          duration: int, col: SceneCollection, ruled: set[str], notes: list[str]) -> None:
    obs = named.get(name) or builtin.get(name) or named.get(strip_vertical(name))  # "X (Vertical)": X's
    if obs is None:
        notes.append(f"{name}: that transition isn't in the scene collection; the usual one is used instead.")
        return
    found = _transition(obs, int(_num(override.get("transition_duration"), duration)), notes)
    if found is not None:
        col.transition_rules.append(TransitionRule(from_scene=ANY, to_scene=bloom, transition=found))
        ruled.add(bloom)


# Streamlabs -------------------------------------------------------------------------------------------


def streamlabs_to_obs(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """A Streamlabs Desktop scene collection rewritten in OBS's shape (see from_obs).

    Streamlabs keeps sources with OBS's own kinds and settings, and scene items as top-left x and y
    with a scale; a dual-output item for the vertical canvas is marked display "vertical"."""
    if is_overlay(data):
        return overlay_to_obs(data)
    notes: list[str] = []
    res = data.get("baseResolutions") or {}
    base = data.get("baseResolution") or res.get("horizontal") or {}
    vbase = res.get("vertical") or {"baseWidth": 1080, "baseHeight": 1920}
    if data.get("relativeCoordinates") is True:
        # Newer Streamlabs keeps positions as if each canvas were 1080 pixels on its short side (a 720x1280
        # vertical canvas holds a 1080x1920 picture at scale 1), checked against its own themes.
        def at_1080(b: dict[str, Any], wide: bool) -> dict[str, Any]:
            w, h = _num(b.get("baseWidth"), 1920 if wide else 1080), _num(b.get("baseHeight"), 1080 if wide else 1920)
            k = 1080 / min(w, h) if min(w, h) > 0 else 1.0
            return {"baseWidth": round(w * k), "baseHeight": round(h * k)}

        base, vbase = at_1080(base, True), at_1080(vbase, False)
    sources = []
    for s in (data.get("sources") or {}).get("items", []):
        if not isinstance(s, dict):
            continue
        filters = [{"id": f.get("type"), "name": f.get("name"), "enabled": f.get("enabled", True),
                    "settings": f.get("settings") or {}} for f in (s.get("filters") or {}).get("items", [])]
        obs = {"id": s.get("type"), "name": s.get("name"), "uuid": s.get("id"),
               "settings": s.get("settings") or {}, "volume": s.get("volume", 1.0),
               "muted": s.get("muted", False), "filters": filters}
        # Streamlabs' Advanced Audio Properties, in OBS's words (_sound_details reads them)
        if s.get("forceMono"):
            obs["flags"] = OBS_FORCE_MONO
        offset = s.get("syncOffset")
        if isinstance(offset, dict):
            obs["sync"] = int(_num(offset.get("sec"))) * 1_000_000_000 + int(_num(offset.get("nsec")))
        if "audioMixers" in s:
            obs["mixers"] = int(_num(s.get("audioMixers"), OBS_ALL_TRACKS))
        if "monitoringType" in s:
            obs["monitoring_type"] = int(_num(s.get("monitoringType")))
        sources.append(obs)
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
    twins = {s["uuid"][:-2] for s in scenes if s.get("canvas_uuid")}
    for s in scenes:
        if s.get("canvas_uuid"):
            for it in s["settings"]["items"]:
                it["scale_ref"] = {"x": vbase.get("baseWidth", 1080), "y": vbase.get("baseHeight", 1920)}
                if it.get("source_uuid") in twins:  # a scene shown on the vertical canvas: its vertical items
                    it["source_uuid"] = f"{it['source_uuid']}-v"
    return obs, notes


# Streamlabs overlay (.overlay) --------------------------------------------------------------------------

OVERLAY_SUFFIX = ".overlay"
ASSETS_KEY = "_assets"  # set by the importer: the folder an .overlay's files were unpacked into
_SL_BASE = (1920.0, 1080.0)  # Streamlabs writes positions and scales as shares of the main canvas
_SL_WIDGETS = {0: "Alert Box", 1: "Tip Goal", 2: "Follower Goal", 3: "Subscriber Goal", 4: "Bit Goal",
               5: "Tip Jar", 6: "Chat Box", 7: "Event List", 8: "The Jar", 9: "Viewer Count", 10: "Stream Boss",
               11: "Credits", 12: "Spin Wheel", 13: "Sponsor Banner", 14: "Media Share", 15: "Subscriber Goal",
               17: "Poll", 18: "Emote Wall", 19: "Chat Highlight", 20: "Super Chat Goal"}


def is_overlay(data: Any) -> bool:
    """A Streamlabs overlay's config.json: scenes made of slots (not a scene collection's sceneItems)."""
    scenes = (data.get("scenes") or {}).get("items") if isinstance(data, dict) else None
    return isinstance(scenes, list) and any(isinstance(s, dict) and "slots" in s for s in scenes)


def overlay_to_obs(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """A Streamlabs overlay (File → Export Overlay File, a zip with config.json and its media) in OBS's shape.

    Streamlabs stores each slot's x and scale as a share of the main canvas's width (y of its height), for
    both canvases; files are named inside the zip (unpacked to data[ASSETS_KEY]); a scene inside another
    scales by the canvas it was made on; widgets are pages Streamlabs makes per account, so their address
    isn't in the file."""
    notes: list[str] = []
    assets = Path(str(data.get(ASSETS_KEY) or "."))
    bw, bh = _SL_BASE
    sources: list[dict[str, Any]] = []
    scenes: list[dict[str, Any]] = []
    noted: set[str] = set()
    names: dict[str, str] = {}  # Streamlabs scene id -> our scene name
    for sc in data["scenes"]["items"]:
        if isinstance(sc, dict):
            names[str(sc.get("sceneId"))] = str(sc.get("name") or "Scene")

    def note_once(text: str) -> None:
        if text not in noted:
            noted.add(text)
            notes.append(text)

    grouped: dict[str, list[str]] = {}  # what to say -> the items it's about, said once for all of them

    def note_for(label: str, text: str) -> None:
        names_ = grouped.setdefault(text, [])
        if label not in names_:
            names_.append(label)

    def asset(name: Any) -> str:
        rel = Path(str(name or "")).name  # a bare name: never outside the folder
        return str(assets / rel) if rel else ""

    shared: dict[tuple, str] = {}

    def add(key: tuple, obs: dict[str, Any]) -> str:
        if key not in shared:
            shared[key] = obs["uuid"]
            sources.append(obs)
        return shared[key]

    for sc in data["scenes"]["items"]:
        if not isinstance(sc, dict):
            continue
        name = names[str(sc.get("sceneId"))]
        by_display: dict[str, list[dict[str, Any]]] = {"horizontal": [], "vertical": []}
        for slot in (sc.get("slots") or {}).get("items", []):
            if not isinstance(slot, dict) or slot.get("sceneNodeType", "item") != "item":
                continue  # Streamlabs folders only sort the list
            content = slot.get("content") if isinstance(slot.get("content"), dict) else {}
            kind = str(content.get("nodeType") or "")
            label = str(slot.get("name") or kind)
            display = "vertical" if slot.get("display") == "vertical" else "horizontal"
            uuid = f"sl-{slot.get('id')}"
            sx, sy = _num(slot.get("scaleX"), 1 / bw) * bw, _num(slot.get("scaleY"), 1 / bh) * bh
            item: dict[str, Any] = {"pos": {"x": _num(slot.get("x")) * bw, "y": _num(slot.get("y")) * bh},
                                    "rot": _num(slot.get("rotation")), "visible": bool(slot.get("visible", True)),
                                    "locked": bool(slot.get("locked", False)), "align": 5,
                                    **{f"crop_{k}": (slot.get("crop") or {}).get(k, 0)
                                       for k in ("left", "top", "right", "bottom")}}
            if display == "vertical":
                item["scale_ref"] = {"x": 1080, "y": 1920}
            ref: str | None = None
            if kind == "ImageNode":
                ref = add(("image", content.get("filename")), {"id": "image_source", "name": label, "uuid": uuid,
                                                               "settings": {"file": asset(content.get("filename"))}})
            elif kind == "VideoNode":
                settings = {**(content.get("settings") or {}), "local_file": asset(content.get("filename")),
                            "is_local_file": True}
                ref = add(("video", content.get("filename"), json.dumps(content.get("settings"), sort_keys=True)),
                          {"id": "ffmpeg_source", "name": label, "uuid": uuid, "settings": settings})
            elif kind == "TextNode" or kind == "StreamlabelNode":
                text = content.get("textSource") if kind == "StreamlabelNode" else content
                settings = dict((text or {}).get("settings") or {})
                if kind == "StreamlabelNode":
                    note_for(label, "Streamlabs labels; they show their sample text until you set them up.")
                ref = add(("text", uuid), {"id": "text_gdiplus", "name": label, "uuid": uuid, "settings": settings})
            elif kind == "WebcamNode":
                ref = add(("camera",), {"id": "dshow_input", "name": "Camera", "uuid": "sl-camera",
                                        "settings": {"video_device_id": ""}})
                item.update(bounds_type=3, bounds_align=0,  # fill the box Streamlabs gives it, cropping the rest
                            bounds={"x": _num(content.get("width")) * bw, "y": _num(content.get("height")) * bh})
                sx = sy = 1.0
                note_once("Camera: pick your camera in its settings.")
            elif kind == "GameCaptureNode":
                ref = add(("game",), {"id": "game_capture", "name": "Game Capture", "uuid": "sl-game",
                                      "settings": {"capture_mode": "any_fullscreen"}})
                sx = sy = 1.0  # Streamlabs shows a game at its own size on both canvases
            elif kind == "WidgetNode":
                wtype = int(_num(content.get("type"), -1))
                settings = dict(content.get("settings") or {})
                ref = add(("widget", wtype, label), {"id": "browser_source", "name": label, "uuid": uuid,
                                                     "settings": settings})
                kind_name = _SL_WIDGETS.get(wtype, "")
                note_for(label if not kind_name or kind_name.replace(" ", "").casefold() in label.replace(" ", "").casefold()
                         else f"{label} ({kind_name})",
                         "Streamlabs widgets; paste each one's address from your Streamlabs dashboard.")
            elif kind == "SceneSourceNode":
                target = names.get(str(content.get("sceneId")))
                if target is None:
                    continue
                if display == "horizontal":  # made on another main canvas size: Streamlabs rescales it
                    sx *= _num(content.get("width"), bw) / bw
                    sy *= _num(content.get("height"), bh) / bh
                ref = f"sl-scene-{content.get('sceneId')}-{display}"
            elif kind == "IconLibraryNode":
                folder = Path(str(content.get("folder") or "")).name
                pictures = sorted(p for p in (assets / folder).glob("*") if p.is_file()) if folder else []
                if not pictures:
                    note_for(label, "Streamlabs icons whose pictures aren't in the file, left out.")
                    continue
                ref = add(("icon", folder), {"id": "image_source", "name": label, "uuid": uuid,
                                             "settings": {"file": str(pictures[0])}})
            else:
                note_once(f"{label}: Streamlabs' {kind or 'this kind of'} item doesn't come over yet.")
                continue
            item.update(source_uuid=ref, scale={"x": sx, "y": sy})
            by_display[display].append(item)
        scenes.append({"id": "scene", "name": name, "uuid": f"sl-scene-{sc.get('sceneId')}-horizontal",
                       "settings": {"items": by_display["horizontal"]}})
        scenes.append({"id": "scene", "name": f"{name} (Vertical)", "uuid": f"sl-scene-{sc.get('sceneId')}-vertical",
                       "canvas_uuid": "streamlabs-vertical", "settings": {"items": by_display["vertical"]}})
    if not any(s["settings"]["items"] for s in scenes if s.get("canvas_uuid")):
        scenes = [s for s in scenes if not s.get("canvas_uuid")]
    else:  # a scene shown only inside others still needs its vertical twin; empty ones that nothing uses go
        used = {it["source_uuid"] for s in scenes for it in s["settings"]["items"]}
        scenes = [s for s in scenes if not s.get("canvas_uuid") or s["settings"]["items"] or s["uuid"] in used]
    for text, labels in grouped.items():
        shown = ", ".join(labels[:4]) + (f" and {len(labels) - 4} more" if len(labels) > 4 else "")
        notes.append(f"{shown}: {text}")
    transition = data.get("transition") if isinstance(data.get("transition"), dict) else {}
    transitions = []
    if transition.get("type"):
        settings = dict(transition.get("settings") or {})
        if settings.get("path"):
            settings["path"] = asset(settings["path"])
        transitions.append({"id": transition["type"], "name": "Streamlabs", "settings": settings})
    order = [{"name": s["name"]} for s in scenes if not s.get("canvas_uuid")]
    obs = {"name": "Streamlabs overlay", "sources": sources + scenes, "groups": [], "scene_order": order,
           "current_scene": order[0]["name"] if order else None,
           "current_program_scene": order[0]["name"] if order else None,
           "resolution": {"x": bw, "y": bh},
           "canvases": [{"info": {"name": "Vertical", "uuid": "streamlabs-vertical"}}] if any(
               s.get("canvas_uuid") for s in scenes) else [],
           "transitions": transitions, "current_transition": "Streamlabs" if transitions else "Fade",
           "transition_duration": int(_num(transition.get("duration"), 300))}
    return obs, notes


def is_overlay_file(path: Path) -> bool:
    """A zip with config.json and no garden.json: a Streamlabs overlay (whatever its name ends in)."""
    import zipfile

    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
    except (OSError, zipfile.BadZipFile):
        return False
    return "config.json" in names and "garden.json" not in names


def unpack_overlay(source: Path, media: Path) -> dict[str, Any]:
    """Unpack a Streamlabs .overlay into `media` (a new folder) and return its config with ASSETS_KEY set."""
    import zipfile

    from sprigcast.core import pack

    source = Path(source)
    try:
        with zipfile.ZipFile(source) as zf:
            try:
                data = json.loads(zf.read("config.json").decode("utf-8-sig"))
            except (KeyError, ValueError) as exc:
                raise ConvertError(f"{source.name} isn't a Streamlabs overlay.") from exc
            if not isinstance(data, dict) or not is_overlay(data):
                raise ConvertError(f"{source.name} isn't a Streamlabs overlay.")
            members = [(i, pack.safe_member(i.filename)) for i in zf.infolist()
                       if not i.is_dir() and i.filename != "config.json"]
            root = pack.unzip(zf, [(i, r) for i, r in members if r is not None], media, source.name)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ConvertError(f"{source.name} can't be read.") from exc
    except pack.PackError as exc:
        raise ConvertError(str(exc)) from exc
    data[ASSETS_KEY] = str(root)
    return data


# Meld -------------------------------------------------------------------------------------------------


_MELD_VERTICAL = "meld-vertical"
_MELD_VIDEO = (".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi", ".mp3", ".wav", ".ogg", ".m4a", ".flac")


def _meld_color(value: Any, default: int = 0xFFFFFFFF) -> int:
    """Meld writes colours as Qt does, "#aarrggbb" (or "#rrggbb"); OBS wants 0xAABBGGRR."""
    text = str(value or "").lstrip("#")
    try:
        if len(text) == 6:
            a, r, g, b = 255, int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
        elif len(text) == 8:
            a, r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4, 6))
        else:
            return default
    except ValueError:
        return default
    return a << 24 | b << 16 | g << 8 | r


def _meld_kind(item: dict[str, Any]) -> str:
    """"Spark Element", "Text", "Group", … from meta.sceneObjectSourceName ("MeldStudio/Text")."""
    name = str((item.get("meta") or {}).get("sceneObjectSourceName") or "")
    return name.rpartition("/")[2] or name


def _meld_audio(track: dict[str, Any], names: dict[str, str], notes: list[str]) -> dict[str, Any] | None:
    """One of Meld's sound tracks (Mic, Game, Music…) as an OBS audio source, with its effects."""
    name = str(track.get("name") or "Sound")
    filters: list[dict[str, Any]] = []
    for node in track.get("associatedAudioNodes") or []:
        if not isinstance(node, dict):
            continue
        label = str(node.get("name") or "")
        on = not node.get("bypass", False)
        if label == "Compressor" or node.get("nodeType") == 4:
            side = names.get(str(node.get("sidechainTrackId") or ""), "")
            filters.append({"id": "compressor_filter", "name": label or "Compressor", "enabled": on, "settings": {
                "ratio": _num(node.get("ratio"), 10.0), "threshold": _num(node.get("threshold"), -18.0),
                "attack_time": round(_num(node.get("attack"), 0.006) * 1000),
                "release_time": round(_num(node.get("release"), 0.06) * 1000),
                "output_gain": _num(node.get("outputGain")), "sidechain_source": side or "none"}})
        elif label == "Voice Clarity" or node.get("nodeType") == 10:
            filters.append({"id": "noise_suppress_filter", "name": label or "Noise Suppression", "enabled": on,
                            "settings": {"suppress_level": -30}})
        elif label in ("Noise Gate", "Gate"):
            filters.append({"id": "noise_gate_filter", "name": label, "enabled": on, "settings": {}})
        elif label == "Limiter":
            filters.append({"id": "limiter_filter", "name": label, "enabled": on,
                            "settings": {"threshold": _num(node.get("threshold"), -6.0)}})
        else:
            notes.append(f"{name}: Meld's {label or 'sound'} effect has no match yet.")
            _missed("meld effect", label)
    common = {"name": name, "uuid": str(track.get("id") or name), "volume": _num(track.get("gain"), 1.0),
              "muted": bool(track.get("muted", False)), "filters": filters}
    unique = str(track.get("uniqueId") or "")
    device = re.sub(r"^Default (Input|Output) Device \(\s*(.*?)\s*\)$", r"\2", str(track.get("deviceName") or ""))
    if track.get("isInputDevice") or track.get("type") == 1:
        return {"id": "wasapi_input_capture", **common,
                "settings": {"device_id": "default" if unique in ("", "default") else device or unique}}
    if device or unique:
        return {"id": "wasapi_output_capture", **common,
                "settings": {"device_id": "default" if unique == "default" else device or unique}}
    notes.append(f"{name}: Meld's own sound track; pick the apps it should hear in its settings.")
    return {"id": "wasapi_process_output_capture", **common, "settings": {"window": ""}}


def meld_to_obs(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """A Meld Studio session (its Session.json, or File → Export) rewritten in OBS's shape (see from_obs).

    Meld keeps scenes (and verticalScenes, each linked to a main one) holding layers ("childItems") with
    their top-left x and y and size on the canvas. Spark Elements are web pages, Text is text, a Group holds
    more layers, and the session's sound tracks (Mic, Game, Music…) play in every scene."""
    notes: list[str] = []
    session = data.get("session") if isinstance(data.get("session"), dict) else data
    sources: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    shared: dict[tuple, str] = {}  # (kind, what, width, height) -> uuid: one page per size, used in many scenes
    taken: set[str] = set()
    noted: set[str] = set()

    def unique_name(name: str) -> str:
        base, n = name, 2
        while name.casefold() in taken:
            name, n = f"{base} {n}", n + 1
        taken.add(name.casefold())
        return name

    def note_once(text: str) -> None:
        if text not in noted:
            noted.add(text)
            notes.append(text)

    def source_for(item: dict[str, Any], vertical: bool) -> str | None:
        kind = _meld_kind(item)
        name = str(item.get("name") or kind or "Layer")
        w, h = round(_num(item.get("width"))), round(_num(item.get("height")))
        track = item.get("audioTrack") if isinstance(item.get("audioTrack"), dict) else {}
        file = str(item.get("filePath") or item.get("source") or item.get("file") or "")
        if item.get("url"):
            key: tuple = ("page", str(item["url"]), w, h)
            obs = {"id": "browser_source", "settings": {
                "url": str(item["url"]), "width": w or 1920, "height": h or 1080, "shutdown": False,
                "reroute_audio": bool(track.get("enabled", False))},
                "volume": _num(track.get("gain"), 1.0), "muted": bool(track.get("muted", False))}
        elif kind == "Text" or "text" in item:
            key = ("text", item.get("id"))
            settings = {"text": str(item.get("text", "")).rstrip("\n"), "color": _meld_color(item.get("foregroundColor")),
                        "font": {"face": str(item.get("fontFamily") or "Arial"),
                                 "size": int(_num(item.get("pixelSize"), 32))}}
            if item.get("isTextFromFile") and file:
                settings.update(read_from_file=True, file=file)
            obs = {"id": "text_gdiplus", "settings": settings}
        elif file:
            key = ("file", file)
            video = Path(file).suffix.lower() in _MELD_VIDEO
            obs = {"id": "ffmpeg_source" if video else "image_source",
                   "settings": {"local_file": file, "looping": True} if video else {"file": file},
                   "volume": _num(track.get("gain"), 1.0), "muted": bool(track.get("muted", False))}
        else:
            note_once(f"{name}: Meld's {kind or 'this kind of'} layers don't come over yet.")
            return None
        if key in shared:
            return shared[key]
        uuid = f"meld-{item.get('id') or len(sources)}"
        shared[key] = uuid
        sources.append({**obs, "name": unique_name(f"{name} (Vertical)" if vertical else name), "uuid": uuid})
        return uuid

    def items_of(children: list[Any], vertical: bool, origin: tuple[float, float],
                 ref: tuple[float, float]) -> list[dict[str, Any]]:
        out = []
        for child in reversed([c for c in children if isinstance(c, dict)]):  # Meld lists the top layer first
            x, y = _num(child.get("x")) - origin[0], _num(child.get("y")) - origin[1]
            w, h = _num(child.get("width")), _num(child.get("height"))
            if _meld_kind(child) == "Group" or (child.get("childItems") and "url" not in child):
                uuid = f"meld-{child.get('id') or len(groups)}"
                label = str(child.get("name") or "Group")
                if label.casefold().startswith("replace with"):
                    note_once(f"{label}: a Meld placeholder; it came over as its text, so swap in your own.")
                group = {"id": "group", "name": unique_name(f"{label} (Vertical)" if vertical else label), "uuid": uuid,
                         "settings": {"cx": w, "cy": h, "items": items_of(child.get("childItems") or [],
                                                                          vertical, (x + origin[0], y + origin[1]), ref)}}
                if vertical:
                    group["canvas_uuid"] = _MELD_VERTICAL
                groups.append(group)
            else:
                uuid = source_for(child, vertical)
            if uuid is None:
                continue
            rot = _num(child.get("rotation"))
            item: dict[str, Any] = {"source_uuid": uuid, "visible": bool(child.get("visible", True)),
                                    "locked": bool(child.get("isLocked", False)), "rot": rot,
                                    "scale": {"x": _num(child.get("scaleX"), 1.0), "y": _num(child.get("scaleY"), 1.0)},
                                    "scale_ref": {"x": ref[0], "y": ref[1]}}
            if rot:  # Meld turns a layer around its middle
                item.update(align=0, pos={"x": x + w / 2, "y": y + h / 2})
            else:
                item.update(align=5, pos={"x": x, "y": y})
            if _meld_kind(child) == "Text":  # a text box: the text sits in it as Meld drew it
                item.update(bounds_type=0)
            if _num(child.get("opacity"), 1.0) < 0.999:
                note_once(f"{child.get('name')}: see-through layers come over solid; set its opacity again.")
            out.append(item)
        return out

    names = {str(t.get("id")): str(t.get("name") or "") for t in session.get("audioTracks") or [] if isinstance(t, dict)}
    for track in session.get("audioTracks") or []:
        if isinstance(track, dict) and (obs := _meld_audio(track, names, notes)):
            obs["name"] = unique_name(obs["name"])
            sources.append(obs)

    scenes: list[dict[str, Any]] = []
    main_names: dict[str, str] = {}
    for sc in session.get("scenes") or []:
        if not isinstance(sc, dict):
            continue
        name = unique_name(str(sc.get("name") or "Scene"))
        main_names[str(sc.get("id"))] = name
        ref = (_num(sc.get("width"), 1920.0), _num(sc.get("height"), 1080.0))
        scenes.append({"id": "scene", "name": name, "uuid": f"meld-{sc.get('id')}",
                       "settings": {"items": items_of(sc.get("childItems") or [], False, (0.0, 0.0), ref)}})
    order = [{"name": s["name"]} for s in scenes]
    vertical = [sc for sc in session.get("verticalScenes") or [] if isinstance(sc, dict)]
    if vertical and session.get("verticalCanvasEnabled", True) is not False:
        for sc in vertical:
            twin = main_names.get(str(sc.get("linkedSceneId") or ""))
            name = unique_name(f"{twin or sc.get('name') or 'Scene'} (Vertical)")
            ref = (_num(sc.get("width"), 1080.0), _num(sc.get("height"), 1920.0))
            scenes.append({"id": "scene", "name": name, "uuid": f"meld-{sc.get('id')}", "canvas_uuid": _MELD_VERTICAL,
                           "settings": {"items": items_of(sc.get("childItems") or [], True, (0.0, 0.0), ref)}})

    first = next((sc for sc in session.get("scenes") or [] if isinstance(sc, dict)), {})
    current = main_names.get(str(session.get("currentScene") or "")) or (order[0]["name"] if order else None)
    move = session.get("sceneTransition") if isinstance(session.get("sceneTransition"), dict) else {}
    effects = [e for e in move.get("effects") or [] if isinstance(e, dict) and e.get("enabled", True)]
    if effects:
        note_once("Meld's moving scene transition comes over as a fade.")
    obs = {"name": "Meld", "sources": sources + scenes, "groups": groups, "scene_order": order,
           "current_scene": current, "current_program_scene": current,
           "resolution": {"x": _num(first.get("width"), 1920.0), "y": _num(first.get("height"), 1080.0)},
           "canvases": [{"info": {"name": "Vertical", "uuid": _MELD_VERTICAL}}] if any(
               s.get("canvas_uuid") for s in scenes) else [],
           "transitions": [], "current_transition": "Fade",
           "transition_duration": int(_num(move.get("duration"), 300))}
    return obs, notes


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
    if detect(data) == "meld" and path.stem.casefold() == "session":
        return "Meld"
    return path.stem.replace("_", " ")


# OBS's profile (Settings → Output and Video), the SprigCast way. A scene collection doesn't hold these: they sit
# in the profile folder next to it (obs-studio/basic/profiles/<name>/basic.ini + streamEncoder.json). Stream keys
# (service.json) are never read.

_OBS_ENCODERS = {"obs_x264": "libx264", "x264": "libx264", "jim_nvenc": "h264_nvenc", "ffmpeg_nvenc": "h264_nvenc",
                 "obs_nvenc_h264_tex": "h264_nvenc", "nvenc": "h264_nvenc", "jim_hevc_nvenc": "hevc_nvenc",
                 "obs_nvenc_hevc_tex": "hevc_nvenc", "nvenc_hevc": "hevc_nvenc", "jim_av1_nvenc": "av1_nvenc",
                 "obs_nvenc_av1_tex": "av1_nvenc", "nvenc_av1": "av1_nvenc", "h264_texture_amf": "h264_amf",
                 "amd_amf_h264": "h264_amf", "amd": "h264_amf", "h265_texture_amf": "hevc_amf",
                 "amd_amf_h265": "hevc_amf", "amd_hevc": "hevc_amf", "av1_texture_amf": "av1_amf",
                 "obs_qsv11": "h264_qsv", "obs_qsv11_v2": "h264_qsv", "qsv": "h264_qsv",
                 "obs_qsv11_hevc": "hevc_qsv", "obs_qsv11_av1": "av1_qsv"}
CLIP_LENGTHS = (15, 30, 45, 60)


def encoder_words(name: str) -> str:
    """'h264_nvenc' -> 'NVIDIA H.264', 'libx264' -> 'x264 (CPU)'."""
    if name == "libx264":
        return "x264 (CPU)"
    codec, _, maker = name.partition("_")
    return f"{ {'nvenc': 'NVIDIA', 'amf': 'AMD', 'qsv': 'Intel'}.get(maker, maker)} " \
           f"{ {'h264': 'H.264', 'hevc': 'HEVC', 'av1': 'AV1'}.get(codec, codec)}"


def find_profile(collection_path: Path) -> Path | None:
    """The OBS profile folder that goes with a scene collection in obs-studio/basic/scenes: the one OBS last used
    (global.ini or user.ini → [Basic] ProfileDir), or the only one there is."""
    basic = collection_path.parent.parent
    profiles = basic / "profiles"
    if collection_path.parent.name.lower() != "scenes" or not profiles.is_dir():
        return None
    for ini in (basic.parent / "user.ini", basic.parent / "global.ini"):
        folder = _ini(ini).get("Basic", {}).get("ProfileDir")
        if folder and (profiles / folder / "basic.ini").is_file():
            return profiles / folder
    found = [p for p in profiles.iterdir() if (p / "basic.ini").is_file()]
    return found[0] if len(found) == 1 else None


def _ini(path: Path) -> dict[str, dict[str, str]]:
    """An OBS .ini as {section: {key: value}} ({} when it isn't there or can't be read)."""
    import configparser

    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # OBS's keys are case-sensitive
    try:
        parser.read_string(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, configparser.Error):
        return {}
    return {name: dict(parser[name]) for name in parser.sections()}


def apply_profile(col: SceneCollection, folder: Path, notes: list[str]) -> None:
    """Frame rate, output size, recording folder and format, clip length and the stream's encoder settings."""
    ini = _ini(folder / "basic.ini")
    video, output = ini.get("Video", {}), ini.get("Output", {})
    advanced = output.get("Mode", "Simple").lower() == "advanced"
    out = ini.get("AdvOut" if advanced else "SimpleOutput", {})
    changed: list[str] = []
    fps = _obs_fps(video)
    base = (int(_num(video.get("BaseCX"))), int(_num(video.get("BaseCY"))))
    scaled = (int(_num(video.get("OutputCX"))), int(_num(video.get("OutputCY"))))
    wide = next((c for c in col.canvases if c.width >= c.height), None)
    if wide is not None and fps:
        wide.fps = fps
        changed.append(f"{fps} fps")
    if wide is not None and all(base) and all(scaled) and scaled != base and min(scaled) < min(wide.width, wide.height):
        wide.output_short_side = min(scaled)
        changed.append(f"sent out at {scaled[0]}x{scaled[1]}")
    folder_key, format_key = ("RecFilePath", "RecFormat2") if advanced else ("FilePath", "RecFormat2")
    rec_folder = out.get(folder_key) or out.get("FilePath")
    if rec_folder:
        col.recording.folder = rec_folder
        changed.append("recordings folder")
    container = (out.get(format_key) or out.get("RecFormat") or "").lower()
    if container in ("mkv", "mp4", "fragmented_mp4", "hybrid_mp4"):
        col.recording.container = "mkv" if container == "mkv" else "mp4"
        changed.append(f"records as {col.recording.container.upper()}")
    seconds = int(_num(out.get("RecRBTime")))
    if seconds:
        col.recording.replay_seconds = min(CLIP_LENGTHS, key=lambda n: abs(n - seconds))
        changed.append(f"{col.recording.replay_seconds}-second clips")
    _stream_encoder(col, folder, out, advanced, changed, notes)
    if changed:
        notes.append(f"From your OBS profile ({folder.name}): {', '.join(changed)}.")


def _obs_fps(video: dict[str, str]) -> int:
    kind = int(_num(video.get("FPSType"), 0))  # 0 common, 1 whole number, 2 fraction
    if kind == 1:
        return int(_num(video.get("FPSInt")))
    if kind == 2:
        den = _num(video.get("FPSDen"), 1) or 1
        return int(round(_num(video.get("FPSNum")) / den))
    common = str(video.get("FPSCommon") or "")
    return int(round(_num(common.split()[0]))) if common else 0


def _stream_encoder(col: SceneCollection, folder: Path, out: dict[str, str], advanced: bool, changed: list[str],
                    notes: list[str]) -> None:
    encoder = col.stream_encoder
    name = out.get("Encoder" if advanced else "StreamEncoder") or ""
    if name in _OBS_ENCODERS:
        encoder.encoder = _OBS_ENCODERS[name]
        changed.append(f"{encoder_words(encoder.encoder)} encoder")
    elif name:
        _missed("encoder", name)
    bitrate = int(_num(out.get("VBitrate")))
    if advanced:
        try:
            settings = json.loads((folder / "streamEncoder.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            settings = {}
        if not isinstance(settings, dict):
            settings = {}
        bitrate = int(_num(settings.get("bitrate"), bitrate))
        rate = str(settings.get("rate_control", "")).lower()
        if rate in ("cbr", "vbr", "cqp"):
            encoder.rate_control = rate  # type: ignore[assignment]
            if rate == "cqp" and "cqp" in settings:
                encoder.cq_level = int(_num(settings["cqp"], encoder.cq_level))
        if "keyint_sec" in settings:
            encoder.keyint_sec = max(0, int(_num(settings["keyint_sec"])))
        preset = str(settings.get("preset2") or "")
        if preset in ("p1", "p2", "p3", "p4", "p5", "p6", "p7"):
            encoder.preset = preset  # type: ignore[assignment]
        if settings.get("tune") in ("hq", "ll", "ull"):
            encoder.tuning = settings["tune"]
        if settings.get("multipass") in ("disabled", "qres", "fullres"):
            encoder.multipass = settings["multipass"]
        if settings.get("profile") in ("high", "main", "baseline"):
            encoder.profile = settings["profile"]
        if encoder.encoder == "libx264" and settings.get("preset"):
            encoder.x264_preset = str(settings["preset"])
        if rate:
            changed.append(f"{rate.upper()}")
    if bitrate:
        for place in col.outputs:
            if place.type == "stream":
                place.bitrate_kbps = bitrate
        notes.append(f"OBS streamed at {bitrate} kbps; set each place's bitrate in Settings → Stream if it differs.")
