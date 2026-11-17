"""A frozen snapshot of what to draw, for the native engine (docs/native-engine.md, phase 2).

The engine never reads SprigCast's live Python objects. Whenever what is shown changes, Python builds a
snapshot here (plain numbers in C structs) and hands it over once; the engine then draws from its own copy on
its own threads, so drawing never needs Python's lock.

A snapshot holds scenes as they are drawn: blooms on their canvas, and sprigs at their layout size (each sprig is
drawn once to its own picture, which the scenes that place it then show, as render/compositor.py does).
"""

from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass, field

import numpy as np

from sprigcast import sprigcore
from sprigcast.core import looks
from sprigcast.core.filters import VideoEffects
from sprigcast.core.scene import ALIGNS, BOUNDS, Scene, SceneItem

log = logging.getLogger(__name__)

NO_EFFECTS = VideoEffects()


@dataclass
class SceneDraw:
    """One scene in a snapshot: its key name, what it holds, the size it's drawn at, and items fading."""

    key: str
    scene: Scene
    size: tuple[int, int]
    fades: dict[str, float] | None = None


@dataclass
class Built:
    """A snapshot ready to hand over. Holds the arrays its struct points into, so keep it until handed over."""

    struct: sprigcore.Snapshot
    arrays: list = field(default_factory=list)
    looks: set[str] = field(default_factory=set)   # looks and shape pictures its effects use
    masks: set[str] = field(default_factory=set)


def _floats(target, values) -> None:
    for i, v in enumerate(values):
        target[i] = float(v)


def effects_struct(fx: VideoEffects) -> sprigcore.Effects:
    e = sprigcore.Effects()
    _floats(e.scroll, fx.scroll)
    e.sharpen = fx.sharpen
    e.key_mode = int(fx.key_mode)
    _floats(e.key_color, fx.key_color)
    e.similarity, e.smoothness, e.spill = fx.similarity, fx.smoothness, fx.spill
    _floats(e.luma, fx.luma)
    e.gamma, e.contrast, e.brightness = fx.gamma, fx.contrast, fx.brightness
    e.saturation, e.hue, e.opacity = fx.saturation, fx.hue, fx.opacity
    _floats(e.tint, fx.tint)
    e.lut = sprigcore.key(fx.lut) if fx.lut else 0
    e.lut_amount = fx.lut_amount
    e.shape = int(fx.shape)
    e.roundness, e.stretch, e.feather, e.border = fx.roundness, int(bool(fx.stretch)), fx.feather, fx.border
    _floats(e.border_color, fx.border_color)
    e.mask = sprigcore.key(fx.mask) if fx.mask else 0
    e.blur, e.pixelate, e.blackout = fx.blur, fx.pixelate, fx.blackout
    e.spin_turns, e.spin_seconds, e.spin_run = float(fx.spin[0]), float(fx.spin[1]), int(fx.spin[2])
    halos = fx.halos[: sprigcore.MAX_HALOS]
    e.halo_count = len(halos)
    for i, h in enumerate(halos):
        e.halos[i].mode = int(h.mode)
        _floats(e.halos[i].color, h.color)
        e.halos[i].alpha, e.halos[i].radius, e.halos[i].boost = h.alpha, h.radius, h.boost
        _floats(e.halos[i].offset, h.offset)
    return e


def item_struct(item: SceneItem, fade: float, effects: int, part: str | None) -> sprigcore.Item:
    it = sprigcore.Item()
    it.source = sprigcore.key(item.source)
    it.part = sprigcore.key(part) if part else 0
    it.x, it.y, it.scale = item.x, item.y, item.scale
    it.scale_y = -1.0 if item.scale_y is None else item.scale_y
    it.rotation = item.rotation
    it.align = ALIGNS.index(item.align) if item.align in ALIGNS else 4
    for i, v in enumerate(item.crop):
        it.crop[i] = int(v)
    it.bounds = BOUNDS.index(item.bounds) if item.bounds in BOUNDS else 0
    _floats(it.bounds_size, item.bounds_size)
    it.bounds_align = ALIGNS.index(item.bounds_align) if item.bounds_align in ALIGNS else 4
    it.fade = fade
    it.effects = effects
    return it


def build(scenes: list[SceneDraw], effects: dict[str, VideoEffects],
          parts: dict[tuple[str, str], str] | None = None) -> Built:
    """The snapshot of `scenes`. effects: source id -> its filters. parts: (source id, scene key) -> the key of
    the sprig drawing that placement shows (a sprig placed in a bloom)."""
    parts = parts or {}
    entries, items, fx_list, fx_index = [], [], [], {}
    used_looks, used_masks = set(), set()
    for draw in scenes:
        first = len(items)
        for item in draw.scene.items:
            if not item.visible:
                continue
            fx = effects.get(item.source)
            index = -1
            if fx is not None and fx != NO_EFFECTS:
                index = fx_index.get(id(fx))
                if index is None:
                    index = fx_index[id(fx)] = len(fx_list)
                    fx_list.append(effects_struct(fx))
                    if fx.lut:
                        used_looks.add(fx.lut)
                    if fx.mask:
                        used_masks.add(fx.mask)
            fade = draw.fades.get(item.source, 1.0) if draw.fades else 1.0
            items.append(item_struct(item, fade, index, parts.get((item.source, draw.key))))
        entries.append(sprigcore.SceneEntry(sprigcore.key(draw.key), int(draw.size[0]), int(draw.size[1]),
                                            first, len(items) - first))
    scene_arr = (sprigcore.SceneEntry * max(1, len(entries)))(*entries)
    item_arr = (sprigcore.Item * max(1, len(items)))(*items)
    fx_arr = (sprigcore.Effects * max(1, len(fx_list)))(*fx_list)
    struct = sprigcore.Snapshot(scene_arr, len(entries), item_arr, len(items), fx_arr, len(fx_list))
    return Built(struct, [scene_arr, item_arr, fx_arr], used_looks, used_masks)


class Resources:
    """Loads the looks and shape pictures a snapshot uses into a device, once each (like Compositor._look
    and _mask). load_errors says why one couldn't be loaded."""

    def __init__(self):
        self.loaded: set[str] = set()
        self.load_errors: dict[str, str] = {}

    def ensure(self, device: sprigcore.Device, built: Built) -> None:
        for name in built.looks - self.loaded:
            self.loaded.add(name)
            try:
                table = looks.look(name[5:]) if name.startswith("look:") else looks.read_cube(name)
            except looks.LutError as exc:
                self.load_errors[name] = str(exc)
                continue
            device.set_look(name, table)
        for path in built.masks - self.loaded:
            self.loaded.add(path)
            try:
                from PIL import Image

                with Image.open(path) as img:
                    rgba = img.convert("RGBA")
            except Exception as exc:  # noqa: BLE001 (any unreadable picture just means no shape)
                self.load_errors[path] = f"Can't open that picture: {exc}"
                continue
            rgba.thumbnail((1024, 1024))
            data = np.asarray(rgba, dtype=np.uint8)
            device.set_mask(path, data, bool(data[..., 3].min() == 255))

    def forget(self) -> None:
        self.loaded.clear()
        self.load_errors.clear()


def hand_over(device: sprigcore.Device, built: Built, resources: Resources | None = None) -> None:
    """Loads what the snapshot needs and gives it to the engine."""
    if resources is not None:
        resources.ensure(device, built)
    device.set_snapshot(built.struct)


__all__ = ["Built", "Resources", "SceneDraw", "build", "hand_over", "ctypes"]
