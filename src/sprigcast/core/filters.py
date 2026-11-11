"""Filters: effects added to a source, like OBS filters.

Video filters run on the GPU, inside the compositor's shader, so they cost almost nothing.
They always apply in a fixed order whatever their place in the list: scroll, sharpen,
chroma/luma key, then color correction. A source has at most one filter of each video kind.

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
from typing import Any, Literal

import numpy as np

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

    @property
    def kind(self) -> str:
        if self.choices:
            return "choice"
        if isinstance(self.default, bool):
            return "bool"
        if isinstance(self.default, str) and self.key.endswith("color"):
            return "color"
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

    @property
    def defaults(self) -> dict[str, Any]:
        return {f.key: f.default for f in self.fields}


_KINDS: dict[str, FilterKind] = {}


def register_filter(kind: str, label: str, media: Literal["video", "audio"], fields: list[Field]):
    """Register an audio filter class (used as a decorator). Video filters are built in only."""

    def wrap(cls):
        _KINDS[kind] = FilterKind(kind, label, media, list(fields), cls, builtin=False)
        return cls

    return wrap


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
_video("sharpen", "Sharpen", [Field("sharpness", "Sharpness", 0.08, 0.0, 1.0, 0.01)])
_video("scroll", "Scroll", [
    Field("speed_x", "Horizontal Speed", 0.0, -500.0, 500.0, 1.0, " px/s"),
    Field("speed_y", "Vertical Speed", 0.0, -500.0, 500.0, 1.0, " px/s"),
])


def parse_color(text: str) -> tuple[float, float, float]:
    text = str(text).strip().lstrip("#")
    try:
        r, g, b = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except (ValueError, IndexError):
        return 1.0, 1.0, 1.0
    return r / 255, g / 255, b / 255


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
    active: bool = field(default=False, compare=False)


def video_effects(filters) -> VideoEffects:
    """Fold a source's enabled video filters into shader uniforms."""
    fx = VideoEffects()
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
        elif flt.kind == "luma_key" and fx.key_mode == 0:
            fx.key_mode = 2
            fx.luma = (float(s["luma_min"]), float(s["luma_max"]), float(s["smoothness"]))
        elif flt.kind == "sharpen":
            fx.sharpen = float(s["sharpness"])
        elif flt.kind == "scroll":
            fx.scroll = (float(s["speed_x"]), float(s["speed_y"]))
        else:
            continue
        fx.active = True
    return fx


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
            target = 1.0 if self.open else 0.0
            start = self.level
            if target > self.level:
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
