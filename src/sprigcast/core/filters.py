"""Filters: effects added to a source, like OBS filters.

Video filters run on the GPU, inside the compositor's shader, so they cost almost nothing.
They always apply in a fixed order whatever their place in the list: scroll, blur or pixelate, sharpen,
chroma/color/luma key, color correction, color look (LUT), then shape. Shadow, glow and stroke are drawn
behind the source in one extra pass each, following its shape and transparency. A source has at most one
filter of each video kind.

Audio filters run on the mixer thread, one 1024-sample block at a time, in list order.
Plugins can add audio filters:

    from sprigcast.core.filters import AudioFilter, Field, register_filter

    @register_filter("my_echo", "Echo", "audio", [Field("mix", "Mix", 0.3, 0.0, 1.0, 0.01)])
    class Echo(AudioFilter):
        def process(self, block):          # float32 (N, 2), return the same shape
            return block * (1 - self.settings["mix"])
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any, Literal

import numpy as np

from sprigcast.core.looks import FROM_FILE, LOOKS, SHAPE_IDS, SHAPES

SAMPLE_RATE = 48000


@dataclass(frozen=True)
class Field:
    """One setting of a filter, shown as a slider (numbers), checkbox, color button or list."""

    key: str
    label: str
    default: Any
    minimum: float = 0.0
    maximum: float = 1.0
    step: float = 1.0
    suffix: str = ""
    choices: tuple[tuple[str, str], ...] = ()  # (value, label) for a drop-down
    file_types: str = ""  # a "...file" field: the file dialog's filter, like "LUT (*.cube)"
    show_when: tuple[str, tuple[str, ...]] | None = None  # (other key, its values) for the row to show

    @property
    def kind(self) -> str:
        if self.key.endswith("sound_id"):
            return "sound"  # one of SprigCast's sounds, picked from a list
        if self.choices:
            return "choice"
        if isinstance(self.default, bool):
            return "bool"
        if isinstance(self.default, str) and self.key.endswith("color"):
            return "color"
        if isinstance(self.default, str) and self.key.endswith("file"):
            return "file"
        if isinstance(self.default, int):
            return "int"
        if isinstance(self.default, float):
            return "float"
        return "text"


@dataclass
class FilterKind:
    kind: str
    label: str
    media: Literal["video", "audio"]
    fields: list[Field]
    processor: type | None = None  # audio: an AudioFilter subclass
    builtin: bool = True
    status: Callable[[str], str] | None = None  # a plugin's note for a source id, shown in the Filters window
    module: str = ""  # a plugin's module, for unregister_filters

    @property
    def defaults(self) -> dict[str, Any]:
        return {f.key: f.default for f in self.fields}


_KINDS: dict[str, FilterKind] = {}


def register_filter(kind: str, label: str, media: Literal["video", "audio"], fields: list[Field],
                    status: Callable[[str], str] | None = None):
    """Register an audio filter class (used as a decorator).

    Video filters are drawn by the GPU, so a plugin can only switch on one the drawing already knows:
    "pixelate", "blackout", "barrel_roll" and "freeze" (see PLUGIN_VIDEO_KINDS). Register them with
    `register_filter(kind, label, "video", fields)(None)`; until a plugin does, they do nothing.
    status(source_id) can return a short note shown beside the filter's name ("Downloading 40%").
    """

    import sys

    module = sys._getframe(1).f_globals.get("__name__", "")  # the plugin, so removing it can take these away

    def wrap(cls):
        if kind in _KINDS and _KINDS[kind].builtin:
            return cls  # built in (Freeze, since 2026-11-20): the plugin adds its extras, the filter stays put
        _KINDS[kind] = FilterKind(kind, label, media, list(fields), cls, builtin=False, module=module, status=status)
        return cls

    return wrap


def unregister_filters(module: str) -> list[str]:
    """Forget the filter kinds a plugin module registered (when it's removed)."""
    gone = [k for k, info in _KINDS.items() if module and info.module == module]
    for kind in gone:
        del _KINDS[kind]
    return gone


def filter_kind(kind: str) -> FilterKind | None:
    return _KINDS.get(kind)


def filter_kinds(media: str | None = None) -> list[FilterKind]:
    return [k for k in _KINDS.values() if media is None or k.media == media]


def settings_for(kind: str, settings: dict[str, Any]) -> dict[str, Any]:
    """The filter's defaults overlaid with its saved settings (unknown keys dropped)."""
    info = _KINDS.get(kind)
    if info is None:
        return dict(settings)
    return {key: settings.get(key, default) for key, default in info.defaults.items()}


# Video ----------------------------------------------------------------------------------------


def _video(kind: str, label: str, fields: list[Field]) -> None:
    _KINDS[kind] = FilterKind(kind, label, "video", fields)


_video("color_correction", "Color Correction", [
    Field("gamma", "Gamma", 0.0, -3.0, 3.0, 0.01),
    Field("contrast", "Contrast", 0.0, -4.0, 4.0, 0.01),
    Field("brightness", "Brightness", 0.0, -1.0, 1.0, 0.01),
    Field("saturation", "Saturation", 0.0, -1.0, 5.0, 0.01),
    Field("hue_shift", "Hue Shift", 0.0, -180.0, 180.0, 0.5, "°"),
    Field("opacity", "Opacity", 100, 0, 100, 1, "%"),
    Field("color", "Color Multiply", "#ffffff"),
])
_video("chroma_key", "Chroma Key", [
    Field("key_color", "Key Color", "#00ff00"),
    Field("similarity", "Similarity", 400, 1, 1000, 1),
    Field("smoothness", "Smoothness", 80, 1, 1000, 1),
    Field("spill", "Key Color Spill Reduction", 100, 1, 1000, 1),
])
_video("luma_key", "Luma Key", [
    Field("luma_min", "Luma Min", 0.0, 0.0, 1.0, 0.01),
    Field("luma_max", "Luma Max", 1.0, 0.0, 1.0, 0.01),
    Field("smoothness", "Smoothness", 0.0, 0.0, 1.0, 0.01),
])
_video("color_key", "Color Key", [
    Field("key_color", "Key Color", "#000000"),
    Field("similarity", "Similarity", 80, 1, 1000, 1),
    Field("smoothness", "Smoothness", 50, 1, 1000, 1),
])
_video("blur", "Blur", [
    Field("mode", "Type", "blur", choices=(("blur", "Blur"), ("pixelate", "Pixelate"))),
    Field("size", "Size", 8.0, 0.0, 64.0, 0.5, " px"),
])
_video("shadow", "Shadow", [
    Field("color", "Color", "#000000"),
    Field("opacity", "Opacity", 60, 0, 100, 1, "%"),
    Field("offset_x", "Offset X", 8.0, -100.0, 100.0, 1.0, " px"),
    Field("offset_y", "Offset Y", 8.0, -100.0, 100.0, 1.0, " px"),
    Field("blur", "Softness", 12.0, 0.0, 60.0, 0.5, " px"),
])
_video("glow", "Glow", [
    Field("color", "Color", "#4fd88f"),
    Field("strength", "Strength", 80, 0, 100, 1, "%"),
    Field("size", "Size", 16.0, 1.0, 60.0, 0.5, " px"),
])
_video("stroke", "Stroke", [
    Field("color", "Color", "#ffffff"),
    Field("opacity", "Opacity", 100, 0, 100, 1, "%"),
    Field("width", "Width", 4.0, 0.5, 40.0, 0.5, " px"),
])
_video("sharpen", "Sharpen", [Field("sharpness", "Sharpness", 0.08, 0.0, 1.0, 0.01)])
_video("scroll", "Scroll", [
    Field("speed_x", "Horizontal Speed", 0.0, -500.0, 500.0, 1.0, " px/s"),
    Field("speed_y", "Vertical Speed", 0.0, -500.0, 500.0, 1.0, " px/s"),
])


_video("lut", "Color look (LUT)", [
    Field("look", "Look", "canopy", choices=LOOKS + ((FROM_FILE, "From a .cube file…"),)),
    Field("file", "Cube file", "", file_types="LUT (*.cube)", show_when=("look", (FROM_FILE,))),
    Field("amount", "Strength", 100, 0, 100, 1, "%"),
])
_video("shape", "Shape", [
    Field("shape", "Shape", "rounded", choices=SHAPES),
    Field("mask_file", "Mask", "", file_types="Images (*.png *.webp *.jpg *.jpeg)",
          show_when=("shape", ("image",))),
    Field("roundness", "Roundness", 12, 0, 50, 1, "%", show_when=("shape", ("rounded",))),
    Field("stretch", "Stretch", False,
          show_when=("shape", tuple(name for name, _label in SHAPES if name not in ("rounded", "image")))),
    Field("feather", "Feather", 0.0, 0.0, 40.0, 0.5, " px"),
    Field("border", "Border", 0, 0, 40, 1, " px"),
    Field("border_color", "Border color", "#3ecf7f"),
])


PLUGIN_VIDEO_KINDS = ("pixelate", "blackout", "barrel_roll", "freeze")  # drawn by the GPU, offered by plugins

# Freeze is always in the filter list (user, 2026-11-20: "they should be in filters"): switched on, the picture
# holds still for its time (0: until it's switched off). The Freeze plugin adds hotkeys, right-click and
# freeze.start on top. The engine holds the picture (freeze_of); nothing for the shader.
_video("freeze", "Freeze", [Field("seconds", "Time", 5.0, 0.0, 600.0, 0.5, " s")])


def parse_color(text: str) -> tuple[float, float, float]:
    text = str(text).strip().lstrip("#")
    try:
        r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except (ValueError, IndexError):
        return 1.0, 1.0, 1.0
    return r / 255, g / 255, b / 255


@dataclass(frozen=True)
class Halo:
    """Something drawn behind a source in its own shape: a shadow, a glow or a stroke."""

    mode: int  # 1 soft (shadow, glow), 2 hard edge (stroke)
    color: tuple[float, float, float]
    alpha: float
    radius: float  # canvas pixels: softness, glow size or stroke width
    offset: tuple[float, float] = (0.0, 0.0)  # canvas pixels
    boost: float = 1.0  # a glow is brighter than an even blur of the edge


@dataclass
class VideoEffects:
    """Shader uniforms for one source. The defaults leave the image untouched."""

    scroll: tuple[float, float] = (0.0, 0.0)  # pixels per second
    sharpen: float = 0.0
    key_mode: int = 0  # 0 none, 1 chroma, 2 luma
    key_color: tuple[float, float, float] = (0.0, 1.0, 0.0)
    similarity: float = 0.4
    smoothness: float = 0.08
    spill: float = 0.1
    luma: tuple[float, float, float] = (0.0, 1.0, 0.0)  # min, max, smoothness
    gamma: float = 1.0  # exponent applied to the color
    contrast: float = 1.0
    brightness: float = 0.0
    saturation: float = 1.0
    hue: float = 0.0  # radians
    opacity: float = 1.0
    tint: tuple[float, float, float] = (1.0, 1.0, 1.0)
    lut: str = ""  # "look:<name>" or a .cube path; "" = none
    lut_amount: float = 1.0
    shape: int = 0  # 0 none, else looks.SHAPE_IDS
    roundness: float = 0.0  # rounded rectangle: corner radius as a share of the short side (0 .. 0.5)
    stretch: bool = False
    feather: float = 0.0  # canvas pixels
    border: float = 0.0
    border_color: tuple[float, float, float] = (0.24, 0.81, 0.5)
    mask: str = ""  # shape "image": the picture's path
    blur: float = 0.0  # source pixels: a soft blur radius
    pixelate: float = 0.0  # source pixels: block size
    halos: tuple[Halo, ...] = ()  # shadow, glow and stroke, drawn behind in this order
    blackout: float = 0.0  # 0 .. 1 toward black (Hide it plugin)
    spin: tuple[float, float, int] = (0.0, 0.0, 0)  # (turns, seconds, run): a barrel roll (Barrel Roll plugin)
    active: bool = field(default=False, compare=False)


def freeze_of(filters) -> tuple[int, float] | None:
    """A source's Freeze filter while it's on: (run, until). run goes up each time it's
    switched on (a new freeze catches a new picture); until is the time.time() it ends by, 0 for no end. A
    freeze saved on and found long after (SprigCast closed mid-freeze) has run out, so it never sticks."""
    flt = next((f for f in filters if f.kind == "freeze" and f.enabled), None)
    if flt is None:
        return None
    until = float(flt.settings.get("until", 0) or 0)
    return int(flt.settings.get("run", 0) or 0), until


def with_item(base: VideoEffects, item: VideoEffects) -> VideoEffects:
    """A source's effects with an item's own on top (Pixelate, Blackout, Barrel roll on that item only)."""
    import dataclasses

    return dataclasses.replace(base, pixelate=item.pixelate or base.pixelate,
                               blackout=max(item.blackout, base.blackout),
                               spin=item.spin if item.spin[0] else base.spin, active=True)


def active(flt: Filter) -> bool:
    """On, and doing something: a Freeze whose time ran out counts as off (switching it on freezes again)."""
    if not flt.enabled:
        return False
    if flt.kind == "freeze":
        import time

        until = float(flt.settings.get("until", 0) or 0)
        return not until or time.time() < until
    return True


def switch(flt: Filter, on: bool) -> None:
    """Switch a filter on or off. A barrel roll plays once, so switching it on (from off) plays it again:
    the engines time a roll by its `run`, and the same run again would sit still. A Freeze switched on (from
    off, or after its time ran out) catches the picture showing now and holds it for its time."""
    if on and not active(flt) and flt.kind in ("barrel_roll", "freeze"):
        settings = {**flt.settings, "run": int(flt.settings.get("run", 0) or 0) + 1}
        if flt.kind == "freeze":
            import time

            seconds = float(settings.get("seconds", 5.0) or 0)
            settings["until"] = time.time() + seconds if seconds > 0 else 0
        flt.settings = settings
    flt.enabled = on


def video_effects(filters) -> VideoEffects:
    """Fold a source's enabled video filters into shader uniforms."""
    fx = VideoEffects()
    halos: dict[str, Halo] = {}
    for flt in filters:
        if not flt.enabled:
            continue
        s = settings_for(flt.kind, flt.settings)
        if flt.kind == "color_correction":
            g = float(s["gamma"])
            fx.gamma = 1.0 / (1.0 + g) if g >= 0 else 1.0 - g
            fx.contrast = max(0.0, 1.0 + float(s["contrast"]))
            fx.brightness = float(s["brightness"])
            fx.saturation = max(0.0, 1.0 + float(s["saturation"]))
            fx.hue = math.radians(float(s["hue_shift"]))
            fx.opacity = max(0.0, min(1.0, float(s["opacity"]) / 100))
            fx.tint = parse_color(s["color"])
        elif flt.kind == "chroma_key":
            fx.key_mode = 1
            fx.key_color = parse_color(s["key_color"])
            fx.similarity = float(s["similarity"]) / 1000
            fx.smoothness = max(0.001, float(s["smoothness"]) / 1000)
            fx.spill = max(0.001, float(s["spill"]) / 1000)
        elif flt.kind == "color_key" and fx.key_mode == 0:
            fx.key_mode = 3
            fx.key_color = parse_color(s["key_color"])
            fx.similarity = float(s["similarity"]) / 1000
            fx.smoothness = max(0.001, float(s["smoothness"]) / 1000)
        elif flt.kind == "blur":
            size = max(0.0, float(s["size"]))
            if size <= 0:
                continue
            if s["mode"] == "pixelate":
                fx.pixelate = max(1.0, size)
            else:
                fx.blur = size
        elif flt.kind in ("shadow", "glow", "stroke"):
            halo = _halo(flt.kind, s)
            if halo is None:
                continue
            halos[flt.kind] = halo
        elif flt.kind == "luma_key" and fx.key_mode == 0:
            fx.key_mode = 2
            fx.luma = (float(s["luma_min"]), float(s["luma_max"]), float(s["smoothness"]))
        elif flt.kind == "sharpen":
            fx.sharpen = float(s["sharpness"])
        elif flt.kind == "scroll":
            fx.scroll = (float(s["speed_x"]), float(s["speed_y"]))
        elif flt.kind == "lut":
            source = str(s["file"]).strip() if s["look"] == FROM_FILE else f"look:{s['look']}"
            if not source or float(s["amount"]) <= 0:
                continue
            fx.lut, fx.lut_amount = source, max(0.0, min(1.0, float(s["amount"]) / 100))
        elif flt.kind in PLUGIN_VIDEO_KINDS:
            if flt.kind not in _KINDS:  # its plugin isn't added (any more): leave the picture alone
                continue
            if flt.kind == "freeze":  # nothing for the shader: the engine holds the picture (freeze_of)
                continue
            if flt.kind == "pixelate":
                fx.pixelate = max(1.0, float(s.get("size", 24)))
            elif flt.kind == "blackout":
                fx.blackout = max(0.0, min(1.0, float(s.get("amount", 100)) / 100))
            else:
                turns = float(s.get("turns", 1)) * (-1.0 if s.get("direction") == "anticlockwise" else 1.0)
                fx.spin = (turns, max(0.1, float(s.get("seconds", 2.0))), int(flt.settings.get("run", 0)))  # run isn't a field: each trigger bumps it
        elif flt.kind == "shape":
            shape = str(s["shape"])
            if shape not in SHAPE_IDS or (shape == "image" and not str(s["mask_file"]).strip()):
                continue
            fx.shape = SHAPE_IDS[shape]
            fx.roundness = max(0.0, min(0.5, float(s["roundness"]) / 100))
            fx.stretch = bool(s["stretch"])
            fx.feather = max(0.0, float(s["feather"]))
            fx.border = max(0.0, float(s["border"]))
            fx.border_color = parse_color(s["border_color"])
            fx.mask = str(s["mask_file"]).strip() if shape == "image" else ""
        else:
            continue
        fx.active = True
    fx.halos = tuple(halos[k] for k in ("shadow", "glow", "stroke") if k in halos)
    return fx


def _halo(kind: str, s: dict[str, Any]) -> Halo | None:
    color = parse_color(s["color"])
    if kind == "shadow":
        alpha = float(s["opacity"]) / 100
        return Halo(1, color, alpha, max(0.0, float(s["blur"])), (float(s["offset_x"]), float(s["offset_y"]))) \
            if alpha > 0 else None
    if kind == "glow":
        alpha = float(s["strength"]) / 100
        return Halo(1, color, alpha, max(1.0, float(s["size"])), boost=2.5) if alpha > 0 else None
    alpha = float(s["opacity"]) / 100
    return Halo(2, color, alpha, max(0.5, float(s["width"]))) if alpha > 0 else None


# Audio ----------------------------------------------------------------------------------------


def db_to_gain(db: float) -> float:
    return 10 ** (db / 20)


class AudioFilter:
    """Processes one source's audio, one block at a time. Keeps its own state between blocks."""

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    def process(self, block: np.ndarray) -> np.ndarray:
        return block


def _audio(kind: str, label: str, fields: list[Field]):
    def wrap(cls):
        _KINDS[kind] = FilterKind(kind, label, "audio", fields, cls)
        return cls

    return wrap


def _coeff(ms: float) -> float:
    """Per-sample smoothing factor for an attack/release time."""
    return math.exp(-1.0 / max(1.0, ms / 1000 * SAMPLE_RATE))


def _envelope(level: np.ndarray, start: float, attack: float, release: float) -> tuple[np.ndarray, float]:
    """Peak follower over one block (level >= 0). Returns the envelope and its last value."""
    out = np.empty_like(level)
    env = start
    for i, x in enumerate(level):
        c = attack if x > env else release
        env = c * env + (1 - c) * x
        out[i] = env
    return out, env


def _blockwise(n: int, step: int = 32):
    """Process audio in small chunks so per-sample loops stay cheap in Python."""
    return range(0, n, step)


@_audio("gain", "Gain", [Field("db", "Gain", 0.0, -30.0, 30.0, 0.1, " dB")])
class GainFilter(AudioFilter):
    def process(self, block):
        return block * db_to_gain(float(self.settings["db"]))


@_audio("noise_gate", "Noise Gate", [
    Field("close_db", "Close Threshold", -32.0, -96.0, 0.0, 0.5, " dB"),
    Field("open_db", "Open Threshold", -26.0, -96.0, 0.0, 0.5, " dB"),
    Field("attack_ms", "Attack Time", 25, 1, 1000, 1, " ms"),
    Field("hold_ms", "Hold Time", 200, 0, 5000, 1, " ms"),
    Field("release_ms", "Release Time", 150, 1, 5000, 1, " ms"),
])
class NoiseGate(AudioFilter):
    """Mutes the source while it is quieter than the close threshold (like OBS's noise gate)."""

    STEP = 32

    def __init__(self, settings):
        super().__init__(settings)
        self.open = False
        self.held = 0.0  # seconds left before closing
        self.level = 0.0  # gate gain, 0 closed .. 1 open

    def process(self, block):
        s = self.settings
        open_at, close_at = db_to_gain(float(s["open_db"])), db_to_gain(float(s["close_db"]))
        step_s = self.STEP / SAMPLE_RATE
        attack = step_s / max(0.001, float(s["attack_ms"]) / 1000)
        release = step_s / max(0.001, float(s["release_ms"]) / 1000)
        hold = float(s["hold_ms"]) / 1000
        out = np.empty_like(block)
        for i in _blockwise(len(block), self.STEP):
            chunk = block[i:i + self.STEP]
            peak = float(np.max(np.abs(chunk))) if len(chunk) else 0.0
            if peak >= open_at:
                self.open, self.held = True, hold
            elif self.open and peak < close_at:
                self.held -= step_s
                if self.held <= 0:
                    self.open = False
            start = self.level
            if self.open:  # (not "below 1": fully open, it stayed and didn't flutter by a release step)
                self.level = min(1.0, self.level + attack)
            else:
                self.level = max(0.0, self.level - release)
            ramp = np.linspace(start, self.level, len(chunk), dtype=np.float32)[:, None]
            out[i:i + self.STEP] = chunk * ramp
        return out


@_audio("compressor", "Compressor", [
    Field("ratio", "Ratio", 10.0, 1.0, 32.0, 0.5, ":1"),
    Field("threshold_db", "Threshold", -18.0, -60.0, 0.0, 0.5, " dB"),
    Field("attack_ms", "Attack", 6, 1, 500, 1, " ms"),
    Field("release_ms", "Release", 60, 1, 1000, 1, " ms"),
    Field("output_db", "Output Gain", 0.0, -32.0, 32.0, 0.1, " dB"),
])
class Compressor(AudioFilter):
    """Turns loud parts down by `ratio` above the threshold, then applies makeup gain."""

    STEP = 32

    def __init__(self, settings):
        super().__init__(settings)
        self.env = 0.0

    def _params(self) -> tuple[float, float, float, float, float]:
        s = self.settings
        return (max(1.0, float(s["ratio"])), float(s["threshold_db"]), float(s["attack_ms"]),
                float(s["release_ms"]), float(s["output_db"]))

    def process(self, block):
        ratio, threshold, attack_ms, release_ms, output_db = self._params()
        makeup = db_to_gain(output_db)
        attack = _coeff(attack_ms / self.STEP)
        release = _coeff(release_ms / self.STEP)
        n = len(block)
        if not n:
            return block
        peaks = np.array([np.max(np.abs(block[i:i + self.STEP])) for i in _blockwise(n, self.STEP)])
        env, self.env = _envelope(peaks, self.env, attack, release)
        env_db = 20 * np.log10(np.maximum(env, 1e-6))
        over = np.maximum(0.0, env_db - threshold)
        gains = db_to_gain(-over * (1 - 1 / ratio)) * makeup
        per_sample = np.repeat(gains, self.STEP)[:n].astype(np.float32)[:, None]
        return block * per_sample


@_audio("limiter", "Limiter", [
    Field("threshold_db", "Threshold", -6.0, -60.0, 0.0, 0.5, " dB"),
    Field("release_ms", "Release", 60, 1, 1000, 1, " ms"),
])
class Limiter(Compressor):
    """Never louder than the threshold: a compressor with an infinite ratio, plus a hard clip for peaks."""

    def _params(self):
        return 1000.0, float(self.settings["threshold_db"]), 0.0, float(self.settings["release_ms"]), 0.0

    def process(self, block):
        out = super().process(block)
        ceiling = db_to_gain(float(self.settings["threshold_db"]))
        return np.clip(out, -ceiling, ceiling)


@_audio("invert_polarity", "Invert Polarity", [])
class InvertPolarity(AudioFilter):
    """Flips the wave upside down: fixes a mic that cancels out against another copy of itself."""

    def process(self, block):
        return -block


# Each sound's loudness (peak, after its filters and volume), refreshed by the mixer every block,
# so a filter can follow another sound (Ducking). Keyed by source id.
SOUND_LEVELS: dict[str, float] = {}


@_audio("ducking", "Ducking", [
    Field("sound_id", "When this plays", ""),
    Field("amount_db", "Lower by", -15.0, -60.0, 0.0, 0.5, " dB"),
    Field("threshold_db", "Starts at", -35.0, -60.0, 0.0, 0.5, " dB"),
    Field("attack_ms", "Fade down", 60, 1, 2000, 1, " ms"),
    Field("hold_ms", "Hold", 300, 0, 5000, 1, " ms"),
    Field("release_ms", "Fade up", 500, 1, 5000, 1, " ms"),
])
class Ducking(AudioFilter):
    """Lowers this sound while another one is playing (music under your mic, game under alerts)."""

    def __init__(self, settings):
        super().__init__(settings)
        self.gain = 1.0
        self.held = 0.0

    def process(self, block):
        s = self.settings
        n = len(block)
        if not n:
            return block
        block_s = n / SAMPLE_RATE
        level = SOUND_LEVELS.get(str(s["sound_id"]), 0.0)
        if s["sound_id"] and level > db_to_gain(float(s["threshold_db"])):
            self.held = float(s["hold_ms"]) / 1000
        else:
            self.held = max(0.0, self.held - block_s)
        target = db_to_gain(float(s["amount_db"])) if self.held > 0 else 1.0
        start = self.gain
        if abs(target - start) < 1e-4:
            self.gain = target
            return block if target == 1.0 else block * np.float32(target)
        tau = float(s["attack_ms"] if target < start else s["release_ms"]) / 1000
        self.gain = start + (target - start) * (1 - math.exp(-block_s / max(tau, 1e-3)))
        return block * np.linspace(start, self.gain, n, dtype=np.float32)[:, None]


class FFmpegAudioFilter(AudioFilter):
    """Runs one of FFmpeg's audio filters (fast C code) through PyAV, one block at a time.

    Some FFmpeg filters hold a few ms of audio back; the first blocks are padded with silence so
    every call still returns exactly as many samples as it was given."""

    def graph_spec(self) -> list[tuple[str, str]]:  # [(filter name, its arguments)]
        raise NotImplementedError

    def __init__(self, settings):
        super().__init__(settings)
        self._graph = None
        self._built_for: tuple | None = None
        self._pts = 0
        self._pending = np.zeros((0, 2), dtype=np.float32)

    def _build(self, spec: list[tuple[str, str]]) -> None:
        import av

        self._graph = None  # the old one goes now, not whenever Python's cycle collector gets to it
        graph = av.filter.Graph()
        # One thread: left to choose, FFmpeg starts a pool per graph (15 on the user's PC), kept until the graph
        # is collected; every settings change made a new graph. A block of sound is far too small to share out.
        graph.threads = 1
        last = graph.add_abuffer(format="fltp", sample_rate=SAMPLE_RATE, layout="stereo")
        for name, args in [*spec, ("aformat", "sample_fmts=fltp:channel_layouts=stereo")]:  # same shape out
            node = graph.add(name, args)
            last.link_to(node)
            last = node
        sink = graph.add("abuffersink")
        last.link_to(sink)
        graph.configure()
        self._graph, self._built_for, self._pts = graph, tuple(spec), 0

    def process(self, block):
        import av

        spec = self.graph_spec()
        if tuple(spec) != self._built_for:
            self._build(spec)  # settings changed: a fresh graph (FFmpeg filters are fixed once set up)
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(block.T, dtype=np.float32), format="fltp",
                                           layout="stereo")
        frame.sample_rate, frame.pts = SAMPLE_RATE, self._pts
        self._pts += len(block)
        self._graph.push(frame)
        pulled = [self._pending]
        while True:
            try:
                out = self._graph.pull()
            except (av.BlockingIOError, av.EOFError):
                break
            pulled.append(out.to_ndarray().T.astype(np.float32, copy=False))
        joined = np.concatenate(pulled) if len(pulled) > 1 else self._pending
        n = len(block)
        if len(joined) < n:
            joined = np.concatenate([np.zeros((n - len(joined), 2), np.float32), joined])
        self._pending = joined[n:]
        return joined[:n]


def _lin(db: float) -> float:
    return float(db_to_gain(float(db)))


@_audio("eq3", "Equalizer", [
    Field("low_db", "Low", 0.0, -20.0, 20.0, 0.1, " dB"),
    Field("mid_db", "Mid", 0.0, -20.0, 20.0, 0.1, " dB"),
    Field("high_db", "High", 0.0, -20.0, 20.0, 0.1, " dB"),
])
class Equalizer3(FFmpegAudioFilter):
    """Bass below ~250 Hz, mids around 1.5 kHz, treble above ~5 kHz."""

    def graph_spec(self):
        s = self.settings
        return [("bass", f"g={float(s['low_db']):.2f}:f=250"),
                ("equalizer", f"f=1500:t=q:w=0.8:g={float(s['mid_db']):.2f}"),
                ("treble", f"g={float(s['high_db']):.2f}:f=5000")]


@_audio("expander", "Expander", [
    Field("ratio", "Ratio", 2.0, 1.0, 20.0, 0.1, ":1"),
    Field("threshold_db", "Threshold", -40.0, -60.0, 0.0, 0.5, " dB"),
    Field("attack_ms", "Attack", 10, 1, 100, 1, " ms"),
    Field("release_ms", "Release", 50, 1, 1000, 1, " ms"),
])
class Expander(FFmpegAudioFilter):
    """The gentle cousin of the noise gate: quiet sounds (room noise, breaths) get quieter still."""

    def graph_spec(self):
        s = self.settings
        return [("agate", f"mode=downward:threshold={_lin(s['threshold_db']):.6f}:ratio={float(s['ratio']):.2f}"
                          f":attack={float(s['attack_ms']):.1f}:release={float(s['release_ms']):.1f}:range=0.001")]


@_audio("upward_compressor", "Upward Compressor", [
    Field("ratio", "Ratio", 1.5, 1.0, 20.0, 0.1, ":1"),
    Field("threshold_db", "Threshold", -20.0, -60.0, 0.0, 0.5, " dB"),
    Field("attack_ms", "Attack", 10, 1, 100, 1, " ms"),
    Field("release_ms", "Release", 50, 1, 1000, 1, " ms"),
])
class UpwardCompressor(FFmpegAudioFilter):
    """Lifts quiet parts toward the threshold: soft voices come forward without loud ones getting louder."""

    def graph_spec(self):
        s = self.settings
        return [("acompressor", f"mode=upward:threshold={_lin(s['threshold_db']):.6f}:ratio={float(s['ratio']):.2f}"
                                f":attack={float(s['attack_ms']):.1f}:release={float(s['release_ms']):.1f}")]


@_audio("noise_suppression", "Noise Suppression", [
    Field("amount_db", "Suppression", 12.0, 1.0, 97.0, 1.0, " dB"),
])
class NoiseSuppression(FFmpegAudioFilter):
    """Takes out steady background noise (fans, hum, hiss) while you talk, not just between words."""

    def graph_spec(self):
        return [("afftdn", f"nr={float(self.settings['amount_db']):.1f}:nf=-50:tn=1")]


@_audio("low_cut", "Low Cut", [
    Field("hz", "Cut below", 100, 20, 400, 5, " Hz"),
])
class LowCut(FFmpegAudioFilter):
    """Takes out rumble below a pitch: desk bumps, fans, a boomy mic. Voices keep their body."""

    def graph_spec(self):
        return [("highpass", f"f={int(self.settings['hz'])}:p=2")]


@_audio("de_esser", "De-esser", [
    Field("amount", "Amount", 0.5, 0.0, 1.0, 0.05),
    Field("hz", "Around", 6000, 3000, 12000, 100, " Hz"),
])
class DeEsser(FFmpegAudioFilter):
    """Softens sharp "s" and "sh" sounds that hiss on a bright mic."""

    def graph_spec(self):
        s = self.settings
        return [("deesser", f"i={float(s['amount']):.2f}:m=0.5:f={int(s['hz']) / SAMPLE_RATE * 2:.4f}:s=o")]


class AudioChain:
    """A source's enabled audio filters, run in list order. Rebuilt by the engine when they change."""

    def __init__(self, filters):
        self.signature = _signature(filters)
        self.processors: list[AudioFilter] = []
        for flt in filters:
            info = _KINDS.get(flt.kind)
            if not flt.enabled or info is None or info.media != "audio" or info.processor is None:
                continue
            self.processors.append(info.processor(settings_for(flt.kind, flt.settings)))

    def matches(self, filters) -> bool:
        return self.signature == _signature(filters)

    def update(self, filters) -> bool:
        """Push new settings into the existing processors (keeps their state). False if the chain changed shape."""
        wanted = [f for f in filters if f.enabled and _KINDS.get(f.kind) and _KINDS[f.kind].media == "audio"
                  and _KINDS[f.kind].processor is not None]
        if len(wanted) != len(self.processors) or any(
            type(p) is not _KINDS[f.kind].processor for p, f in zip(self.processors, wanted)
        ):
            return False
        for proc, flt in zip(self.processors, wanted):
            proc.settings = settings_for(flt.kind, flt.settings)
        self.signature = _signature(filters)
        return True

    def process(self, block: np.ndarray) -> np.ndarray:
        for proc in self.processors:
            block = proc.process(block)
        return block


def _signature(filters) -> tuple:
    return tuple((f.id, f.kind, f.enabled, tuple(sorted(f.settings.items()))) for f in filters)
