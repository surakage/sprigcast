"""Scene collection: canvases, sources, scenes, outputs. Saved as JSON."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

FORMAT_VERSION = 2


class Canvas(BaseModel):
    id: str  # "horizontal", "vertical"
    width: int
    height: int
    fps: int = 60
    enabled: bool = True


class SourceConfig(BaseModel):
    """A source defined once and referenced by id from any number of scenes."""

    id: str
    kind: str  # "color", "image", later "display_capture", "camera", ...
    name: str
    settings: dict[str, Any] = Field(default_factory=dict)


class SceneItem(BaseModel):
    source: str
    x: float = 0.0  # top-left corner, canvas pixels
    y: float = 0.0
    scale: float = 1.0
    rotation: float = 0.0  # degrees, clockwise, around the item's centre
    crop: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom in source pixels
    visible: bool = True
    locked: bool = False

    def cropped_size(self, source_size: tuple[int, int]) -> tuple[int, int]:
        w, h = source_size
        left, top, right, bottom = self.crop
        return max(0, w - left - right), max(0, h - top - bottom)

    def quad(self, source_size: tuple[int, int]) -> list[tuple[float, float, float, float]]:
        """Corners as (x, y, u, v) in draw order top-left, top-right, bottom-left, bottom-right.

        x and y are canvas pixels; u and v are texture coordinates (0..1, v=0 is the image's top row).
        """
        sw, sh = source_size
        cw, ch = self.cropped_size(source_size)
        if sw == 0 or sh == 0 or cw == 0 or ch == 0:
            return []
        left, top, right, bottom = self.crop
        u0, v0 = left / sw, top / sh
        u1, v1 = (sw - right) / sw, (sh - bottom) / sh
        w, h = cw * self.scale, ch * self.scale
        cx, cy = self.x + w / 2, self.y + h / 2
        rad = math.radians(self.rotation)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        corners = [(-w / 2, -h / 2, u0, v0), (w / 2, -h / 2, u1, v0), (-w / 2, h / 2, u0, v1), (w / 2, h / 2, u1, v1)]
        return [(cx + dx * cos_r - dy * sin_r, cy + dx * sin_r + dy * cos_r, u, v) for dx, dy, u, v in corners]


class Scene(BaseModel):
    id: str
    name: str
    canvas: str
    linked_to: str | None = None  # switching to that scene also switches to this one
    items: list[SceneItem] = Field(default_factory=list)  # index 0 draws first (bottom)


class Output(BaseModel):
    id: str
    type: Literal["stream", "recording"]
    service: str | None = None
    canvas: str
    bitrate_kbps: int = 6000
    key_ref: str | None = None  # name of the secret in the OS keyring, never the key itself
    server: str = ""  # ingest URL for services without a fixed one (TikTok, Kick, custom)
    enabled: bool = False  # included when you press Go live
    size: tuple[int, int] | None = None  # encoded size, e.g. (1280, 720); None = the canvas size
    fps: int | None = None  # None = the canvas fps
    bandwidth_test: bool = False  # Twitch only: stream reaches Twitch but viewers never see it
    twitch_account: bool = False  # Twitch only: fetch the key from the connected account at Go live


class RecordingSettings(BaseModel):
    folder: str = ""  # empty: Videos/SprigCast
    canvases: list[str] = Field(default_factory=lambda: ["horizontal", "vertical"])  # which canvases to record
    bitrate_kbps: int = 12000
    container: Literal["mkv", "mp4"] = "mkv"


class Transition(BaseModel):
    """How a canvas changes scenes: a cut, or a fade that crossfades over duration_ms."""

    kind: Literal["cut", "fade"] = "fade"
    duration_ms: int = 300


class SceneCollection(BaseModel):
    version: int = FORMAT_VERSION
    canvases: list[Canvas]
    sources: list[SourceConfig] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    active_scenes: dict[str, str] = Field(default_factory=dict)  # canvas id -> scene id
    outputs: list[Output] = Field(default_factory=list)
    recording: RecordingSettings = Field(default_factory=RecordingSettings)
    transition: Transition = Field(default_factory=Transition)
    hotkeys: dict[str, str] = Field(default_factory=dict)  # action -> key combo, e.g. "scene:brb" -> "Ctrl+Alt+2"

    @model_validator(mode="after")
    def _check_references(self) -> "SceneCollection":
        canvas_ids = {c.id for c in self.canvases}
        source_ids = {s.id for s in self.sources}
        scene_ids = {s.id for s in self.scenes}
        for scene in self.scenes:
            if scene.canvas not in canvas_ids:
                raise ValueError(f"scene {scene.id!r} uses unknown canvas {scene.canvas!r}")
            if scene.linked_to is not None and scene.linked_to not in scene_ids:
                raise ValueError(f"scene {scene.id!r} links to unknown scene {scene.linked_to!r}")
            for item in scene.items:
                if item.source not in source_ids:
                    raise ValueError(f"scene {scene.id!r} uses unknown source {item.source!r}")
        for canvas_id, scene_id in self.active_scenes.items():
            if canvas_id not in canvas_ids or scene_id not in scene_ids:
                raise ValueError(f"active scene {canvas_id!r} -> {scene_id!r} is invalid")
        for output in self.outputs:
            if output.canvas not in canvas_ids:
                raise ValueError(f"output {output.id!r} uses unknown canvas {output.canvas!r}")
        return self

    # Lookups -------------------------------------------------------------

    def canvas(self, canvas_id: str) -> Canvas:
        return next(c for c in self.canvases if c.id == canvas_id)

    def source(self, source_id: str) -> SourceConfig:
        return next(s for s in self.sources if s.id == source_id)

    def scene(self, scene_id: str) -> Scene:
        return next(s for s in self.scenes if s.id == scene_id)

    def scenes_for(self, canvas_id: str) -> list[Scene]:
        return [s for s in self.scenes if s.canvas == canvas_id]

    def active_scene(self, canvas_id: str) -> Scene | None:
        scene_id = self.active_scenes.get(canvas_id)
        return self.scene(scene_id) if scene_id else None

    # Changes -------------------------------------------------------------

    def set_active(self, scene_id: str) -> None:
        """Make a scene active on its canvas, and switch any scenes linked to it (in either direction)."""
        scene = self.scene(scene_id)
        self.active_scenes[scene.canvas] = scene.id
        for other in self.scenes:
            if other.linked_to == scene.id or (scene.linked_to == other.id):
                self.active_scenes[other.canvas] = other.id

    def new_id(self, prefix: str) -> str:
        taken = {c.id for c in self.canvases} | {s.id for s in self.sources} | {s.id for s in self.scenes}
        taken |= {o.id for o in self.outputs}
        n = 1
        while f"{prefix}-{n}" in taken:
            n += 1
        return f"{prefix}-{n}"

    def remove_source_if_unused(self, source_id: str) -> None:
        if not any(item.source == source_id for scene in self.scenes for item in scene.items):
            self.sources = [s for s in self.sources if s.id != source_id]

    # Files ---------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "SceneCollection":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def from_json(cls, text: str) -> "SceneCollection":
        return cls.model_validate_json(text)

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.model_dump(mode="json"), indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic on the same drive, so a crash never leaves half a file
