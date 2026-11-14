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


class Filter(BaseModel):
    """An effect on a source (color correction, chroma key, noise gate...). See core/filters.py."""

    id: str
    kind: str
    name: str
    enabled: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)


class SourceConfig(BaseModel):
    """A source defined once and referenced by id from any number of scenes."""

    id: str
    kind: str  # "color", "image", later "display_capture", "camera", ...
    name: str
    settings: dict[str, Any] = Field(default_factory=dict)
    filters: list[Filter] = Field(default_factory=list)  # applied top to bottom


class SceneItem(BaseModel):
    source: str
    x: float = 0.0  # top-left corner, canvas pixels
    y: float = 0.0
    scale: float = 1.0
    # Height scale when the item is stretched (aspect unlocked); None keeps the shape (= scale).
    scale_y: float | None = None
    rotation: float = 0.0  # degrees, clockwise, around the item's centre
    crop: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom in source pixels
    visible: bool = True
    locked: bool = False

    @property
    def sy(self) -> float:
        """The height scale: scale_y when stretched, else the same as the width scale."""
        return self.scale if self.scale_y is None else self.scale_y

    def drawn_size(self, source_size: tuple[int, int]) -> tuple[float, float]:
        """Width and height on the canvas, in canvas pixels (crop and both scales applied)."""
        w, h = self.cropped_size(source_size)
        return w * self.scale, h * self.sy

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
        w, h = cw * self.scale, ch * self.sy
        cx, cy = self.x + w / 2, self.y + h / 2
        rad = math.radians(self.rotation)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        corners = [(-w / 2, -h / 2, u0, v0), (w / 2, -h / 2, u1, v0), (-w / 2, h / 2, u0, v1), (w / 2, h / 2, u1, v1)]
        return [(cx + dx * cos_r - dy * sin_r, cy + dx * sin_r + dy * cos_r, u, v) for dx, dy, u, v in corners]


class SprigState(BaseModel):
    """A named look of a sprig for Live, like "Alert showing": the items it hides."""

    name: str
    hide: list[str] = Field(default_factory=list)  # source ids of the sprig's items hidden in this state


STATE_FADE_S = 0.3  # how long items take to fade in or out when a sprig changes state


class Scene(BaseModel):
    """A show (a main scene you switch to on air) or a part (a reusable piece placed inside other scenes).

    A part is laid out on its canvas's size and appears in other scenes through a source of kind
    "scene" whose settings name it ({"scene": part id}). Parts are never shown on air by themselves.

    A part can have one layout per canvas shape: its other shapes are scenes with `shape_of` set to
    the main part's id. A show on the vertical canvas then gets the part's vertical layout, and falls
    back to the main layout when there is none.
    """

    id: str
    name: str
    canvas: str
    kind: Literal["show", "part"] = "show"
    linked_to: str | None = None  # switching to that scene also switches to this one
    shape_of: str | None = None  # parts only: this is the layout of that part for another canvas
    folder: str | None = None  # shows only: the SceneFolder it's listed under (just for tidiness)
    states: list[SprigState] = Field(default_factory=list)  # main parts only: looks to switch between in Live
    state: str | None = None  # main parts only: the current state's name; None shows the sprig as built
    items: list[SceneItem] = Field(default_factory=list)  # index 0 draws first (bottom)


FOLDER_COLOURS = ("#ff5fa2", "#3ddc84", "#4cc9f0", "#ffd23f", "#ff8c42", "#b388ff")


class SceneFolder(BaseModel):
    """A named group of shows on one canvas, like "Main Scenes" or "Browser Scenes".

    Folders only organise the lists; they never change what's on air.
    """

    id: str
    name: str
    canvas: str
    colour: str = FOLDER_COLOURS[0]
    collapsed: bool = False
    live: bool = False  # its shows are the buttons in Live mode (see live_scenes)


PART_KIND = "scene"  # source kind that places a part inside another scene


def _bounds(items: list[SceneItem], sizes: dict[str, tuple[int, int]]) -> tuple[float, float, float, float] | None:
    """Left, top, right, bottom around the visible items whose size is known (rotation ignored)."""
    boxes = []
    for item in items:
        size = sizes.get(item.source)
        if not item.visible or not size:
            continue
        w, h = item.cropped_size(size)
        if w and h:
            boxes.append((item.x, item.y, item.x + w * item.scale, item.y + h * item.sy))
    if not boxes:
        return None
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def _keep_between_edges(pos: float, length: float, extent: float, new_length: float, new_extent: float) -> float:
    """Where an item of new_length goes so it sits the same share of the way between the edges."""
    room, new_room = extent - length, new_extent - new_length
    if room <= 0 or new_room <= 0:
        return (pos + length / 2) * new_extent / extent - new_length / 2  # fills the axis: keep the centre
    return pos / room * new_room


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
    enhanced: bool = False  # Twitch only: Enhanced Broadcasting (several qualities, chosen by Twitch)
    enhanced_vertical: bool = False  # with enhanced: also send the vertical canvas (Twitch dual format)
    enhanced_max_tracks: int = 0  # with enhanced: most qualities to send; 0 = let Twitch decide
    enhanced_max_kbps: int = 0  # with enhanced: most bandwidth for all qualities together; 0 = let Twitch decide
    ignore_recommendations: bool = False  # allow a bitrate above what the service accepts
    auto_bitrate: bool = True  # lower the bitrate when the upload struggles, climb back after (not with enhanced)


class EncoderSettings(BaseModel):
    """How video is encoded (Settings → Output), like OBS's NVENC options. x264 gets the closest match."""

    rate_control: Literal["cbr", "vbr", "cqp"] = "cbr"
    cq_level: int = 20  # with cqp: lower is better quality and bigger files
    keyint_sec: int = 2  # seconds between keyframes; 0 = auto (2)
    preset: Literal["p1", "p2", "p3", "p4", "p5", "p6", "p7"] = "p5"
    tuning: Literal["hq", "ll", "ull"] = "hq"
    multipass: Literal["disabled", "qres", "fullres"] = "disabled"
    profile: Literal["high", "main", "baseline"] = "high"
    lookahead: bool = False
    adaptive_quant: bool = True
    b_frames: int = 0  # none: SprigCast's own lighter NVENC path needs none (2 was the old default)
    audio_kbps: int = 160


class RecordingSettings(BaseModel):
    folder: str = ""  # empty: Videos/SprigCast
    canvases: list[str] = Field(default_factory=lambda: ["horizontal", "vertical"])  # which canvases to record
    bitrate_kbps: int = 12000
    container: Literal["mkv", "mp4"] = "mkv"
    replay_seconds: int = 30  # replay buffer length; saved clips land in the recordings folder
    short_side: int | None = None  # record shrunk so the short side is this (720: 1280x720); None = canvas size
    encoder: EncoderSettings = Field(default_factory=lambda: EncoderSettings(rate_control="vbr", audio_kbps=192, b_frames=0))
    # No B-frames for recordings: NVENC needs ~50 MB of extra GPU-mapped buffers per encoder for them,
    # and at recording bitrates the quality difference is invisible. Streams skip them too since 2026-11.


class AudioSettings(BaseModel):
    """Mixer-wide audio options. Per-source ones (volume, mute, monitor, hidden, duck_voice) live in
    each audio source's settings."""

    ducking: bool = False  # lower everything else while a voice source (your mic) is talking
    duck_amount_db: float = -14.0
    duck_threshold_db: float = -38.0
    duck_attack_ms: float = 250.0
    duck_release_ms: float = 900.0
    duck_hold_ms: float = 400.0


TRANSITION_KINDS = ("cut", "fade", "slide", "swipe", "wipe", "leaf", "bloom", "stinger")


class Stinger(BaseModel):
    """A short video played over the change, usually with see-through parts (WebM VP9 or MOV ProRes 4444).

    The scenes swap at cut_ms, while the video covers the screen. A track matte video holds the picture
    in one half and a black and white matte in the other: white shows the new scene, black the old one.
    """

    path: str = ""
    vertical_path: str = ""  # its own video for vertical canvases; empty = the main one, filled and centred
    cut_ms: int = 500
    matte: Literal["none", "right", "below"] = "none"  # where the matte sits in a track matte video
    matte_only: bool = False  # use the matte to swap scenes, but do not show the picture half
    volume_db: float = 0.0
    muted: bool = False


class Transition(BaseModel):
    """How a canvas changes scenes.

    cut swaps at once; fade crossfades; slide pushes the old scene out; swipe slides the new one over
    the old; wipe sweeps a soft edge across (or follows a black and white image: a luma wipe); leaf and
    bloom open the new scene through a growing leaf or flower; stinger plays a video over the swap.
    """

    kind: Literal["cut", "fade", "slide", "swipe", "wipe", "leaf", "bloom", "stinger"] = "fade"
    duration_ms: int = 300
    direction: Literal["left", "right", "up", "down"] = "left"  # slide, swipe and wipe: where it moves to
    softness: float = 0.15  # wipe, leaf and bloom: how soft the edge is, 0 sharp .. 1 very soft
    wipe_image: str = ""  # wipe: a black and white image, dark parts change first
    stinger: Stinger = Field(default_factory=Stinger)


ANY = ""  # a TransitionRule end that matches every bloom


class TransitionRule(BaseModel):
    """Use `transition` when going from one bloom to another, instead of the usual one.

    Ends are scene ids of main blooms; ANY matches every bloom. A rule set on a bloom also covers
    its linked twins on other canvases, so the horizontal and vertical change together.
    """

    from_scene: str = ANY
    to_scene: str = ANY
    transition: Transition = Field(default_factory=lambda: Transition(kind="stinger"))


class SceneCollection(BaseModel):
    version: int = FORMAT_VERSION
    canvases: list[Canvas]
    sources: list[SourceConfig] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    folders: list[SceneFolder] = Field(default_factory=list)
    active_scenes: dict[str, str] = Field(default_factory=dict)  # canvas id -> scene id
    outputs: list[Output] = Field(default_factory=list)
    recording: RecordingSettings = Field(default_factory=RecordingSettings)
    stream_encoder: EncoderSettings = Field(default_factory=EncoderSettings)
    transition: Transition = Field(default_factory=Transition)
    transition_rules: list[TransitionRule] = Field(default_factory=list)  # first match wins, see transition_for
    audio: AudioSettings = Field(default_factory=AudioSettings)
    hotkeys: dict[str, str] = Field(default_factory=dict)  # action -> key combo, e.g. "scene:brb" -> "Ctrl+Alt+2"

    @model_validator(mode="after")
    def _check_references(self) -> "SceneCollection":
        canvas_ids = {c.id for c in self.canvases}
        source_ids = {s.id for s in self.sources}
        scene_ids = {s.id for s in self.scenes}
        folder_canvas = {f.id: f.canvas for f in self.folders}
        for folder in self.folders:
            if folder.canvas not in canvas_ids:
                raise ValueError(f"folder {folder.id!r} uses unknown canvas {folder.canvas!r}")
        for scene in self.scenes:
            if scene.folder is not None and folder_canvas.get(scene.folder) != scene.canvas:
                scene.folder = None  # a missing or other-canvas folder: just list it loose
            if scene.canvas not in canvas_ids:
                raise ValueError(f"scene {scene.id!r} uses unknown canvas {scene.canvas!r}")
            if scene.shape_of is not None and scene.shape_of not in scene_ids:
                raise ValueError(f"scene {scene.id!r} is a shape of unknown part {scene.shape_of!r}")
            if scene.linked_to is not None and scene.linked_to not in scene_ids:
                raise ValueError(f"scene {scene.id!r} links to unknown scene {scene.linked_to!r}")
            for item in scene.items:
                if item.source not in source_ids:
                    raise ValueError(f"scene {scene.id!r} uses unknown source {item.source!r}")
        for canvas_id, scene_id in self.active_scenes.items():
            if canvas_id not in canvas_ids or scene_id not in scene_ids:
                raise ValueError(f"active scene {canvas_id!r} -> {scene_id!r} is invalid")
            if self.scene(scene_id).kind == "part":
                raise ValueError(f"part {scene_id!r} can't be the active scene of {canvas_id!r}")
        for output in self.outputs:
            if output.canvas not in canvas_ids:
                raise ValueError(f"output {output.id!r} uses unknown canvas {output.canvas!r}")
        # Rules for blooms that were deleted since are dropped quietly.
        self.transition_rules = [r for r in self.transition_rules
                                 if r.from_scene in scene_ids | {ANY} and r.to_scene in scene_ids | {ANY}]
        return self

    # Lookups -------------------------------------------------------------

    def canvas(self, canvas_id: str) -> Canvas:
        return next(c for c in self.canvases if c.id == canvas_id)

    def source(self, source_id: str) -> SourceConfig:
        return next(s for s in self.sources if s.id == source_id)

    def scene(self, scene_id: str) -> Scene:
        return next(s for s in self.scenes if s.id == scene_id)

    def folders_for(self, canvas_id: str) -> list[SceneFolder]:
        return [f for f in self.folders if f.canvas == canvas_id]

    def folder(self, folder_id: str) -> SceneFolder:
        return next(f for f in self.folders if f.id == folder_id)

    def add_folder(self, canvas_id: str, name: str) -> SceneFolder:
        used = len(self.folders_for(canvas_id))
        # The first folder on a canvas is its Live folder, like "Main Scenes"; change it any time.
        folder = SceneFolder(id=self.new_id("folder"), name=name, canvas=canvas_id,
                             colour=FOLDER_COLOURS[used % len(FOLDER_COLOURS)], live=used == 0)
        self.folders.append(folder)
        return folder

    def remove_folder(self, folder_id: str) -> None:
        """The folder goes; its shows stay, listed loose."""
        self.folders = [f for f in self.folders if f.id != folder_id]
        for scene in self.scenes:
            if scene.folder == folder_id:
                scene.folder = None

    def grouped_scenes(self, canvas_id: str) -> list[tuple[SceneFolder | None, list[Scene]]]:
        """The shows on a canvas as (folder, shows): loose shows first (folder None), then each folder."""
        shows = self.scenes_for(canvas_id)
        groups: list[tuple[SceneFolder | None, list[Scene]]] = [(None, [s for s in shows if s.folder is None])]
        groups += [(f, [s for s in shows if s.folder == f.id]) for f in self.folders_for(canvas_id)]
        return groups

    def live_scenes(self, canvas_id: str) -> list[tuple[SceneFolder | None, list[Scene]]]:
        """What Live mode shows as buttons: the shows in the canvas's Live folders.

        With no Live folder on a canvas (no folders yet, or all switched off), every show is listed.
        """
        live = [(f, shows) for f, shows in self.grouped_scenes(canvas_id) if f is not None and f.live]
        return live if live else self.grouped_scenes(canvas_id)

    def scenes_for(self, canvas_id: str) -> list[Scene]:
        """The shows on a canvas (parts are listed by parts())."""
        return [s for s in self.scenes if s.canvas == canvas_id and s.kind == "show"]

    def parts(self) -> list[Scene]:
        """Parts as the user sees them: one entry per part, whatever shapes it has."""
        return [s for s in self.scenes if s.kind == "part" and s.shape_of is None]

    def part_scenes(self) -> list[Scene]:
        """Every part layout, main and other shapes: what the renderer draws."""
        return [s for s in self.scenes if s.kind == "part"]

    def main_part(self, scene: Scene) -> Scene:
        """The part a shape belongs to (a main part or a show is returned as is)."""
        if scene.shape_of is None:
            return scene
        return next((s for s in self.scenes if s.id == scene.shape_of), scene)

    def shapes(self, part: Scene) -> list[Scene]:
        """A part's layouts: the main one first, then one per other canvas."""
        part = self.main_part(part)
        return [part] + [s for s in self.scenes if s.shape_of == part.id]

    def shape_for(self, part: Scene, canvas_id: str) -> Scene:
        """The layout of a part to use on a canvas: its shape for that canvas, else its main layout."""
        shapes = self.shapes(part)
        return next((s for s in shapes if s.canvas == canvas_id), shapes[0])

    def part_of(self, source: SourceConfig) -> Scene | None:
        """The (main) part a nested-part source shows, or None."""
        if source.kind != PART_KIND:
            return None
        part_id = source.settings.get("scene")
        return next((s for s in self.scenes if s.id == part_id and s.kind == "part" and s.shape_of is None), None)

    @staticmethod
    def hidden_here(source: SourceConfig) -> set[str]:
        """Sources inside a part that one placement of it hides (its own tweak; other placements still show them)."""
        return {str(s) for s in source.settings.get("hide", [])}

    @staticmethod
    def placement_view(source: SourceConfig, shape: Scene) -> Scene:
        """The part layout as this placement shows it: the same, with its hidden items switched off."""
        return SceneCollection.hide_items(shape, {str(s) for s in source.settings.get("hide", [])})

    @staticmethod
    def hide_items(shape: Scene, hidden: set[str]) -> Scene:
        """A copy of a layout with these sources switched off (the layout itself when there are none)."""
        if not hidden:
            return shape
        view = shape.model_copy(deep=True)
        for item in view.items:
            if item.source in hidden:
                item.visible = False
        return view

    def state_hidden(self, part: Scene) -> set[str]:
        """What the sprig's current Live state hides (in every layout of it)."""
        main = self.main_part(part)
        state = next((s for s in main.states if s.name == main.state), None)
        return set(state.hide) if state else set()

    def set_state(self, part_id: str, name: str | None) -> None:
        """Switch a sprig to one of its states, or back to as-built with None."""
        main = self.main_part(self.scene(part_id))
        if name is not None and all(s.name != name for s in main.states):
            raise ValueError(f"{main.name!r} has no state {name!r}")
        main.state = name

    def parts_inside(self, scene: Scene) -> list[Scene]:
        """Parts placed directly in a scene (as main parts)."""
        out = []
        sources = {s.id: s for s in self.sources}
        for item in scene.items:
            source = sources.get(item.source)  # an item can outlive its source for a moment while it is removed
            part = self.part_of(source) if source is not None else None
            if part is not None and part not in out:
                out.append(part)
        return out

    def inner_shapes(self, scene: Scene) -> list[Scene]:
        """The part layouts a scene draws: each placed part in the shape for the scene's canvas."""
        return [self.shape_for(p, scene.canvas) for p in self.parts_inside(scene)]

    def part_uses(self, part_id: str) -> list[Scene]:
        """Shows and parts that place this part (a part counts once, whichever of its shapes places it)."""
        out: list[Scene] = []
        for s in self.scenes:
            if any(p.id == part_id for p in self.parts_inside(s)):
                user = self.main_part(s)
                if user not in out:
                    out.append(user)
        return out

    def contains(self, scene: Scene, part_id: str, _seen: frozenset = frozenset()) -> bool:
        """True if `scene` is the part or places it, at any depth and in any shape.

        Used to refuse a part inside itself. Shapes count as their part.
        """
        target = next((s.shape_of or s.id for s in self.scenes if s.id == part_id), part_id)
        main = self.main_part(scene)
        if main.id == target:
            return True
        if main.id in _seen:
            return False
        seen = _seen | {main.id}
        return any(self.contains(p, target, seen) for shape in self.shapes(main) for p in self.parts_inside(shape))

    def part_order(self) -> list[Scene]:
        """Part layouts in the order to render them: each after the layouts it draws. Cycles are skipped."""
        order: list[Scene] = []
        state: dict[str, str] = {}

        def visit(part: Scene) -> None:
            if state.get(part.id) in ("done", "busy"):
                return
            state[part.id] = "busy"
            for inner in self.inner_shapes(part):
                visit(inner)
            state[part.id] = "done"
            order.append(part)

        for part in self.part_scenes():
            visit(part)
        return order

    def add_shape(self, part: Scene, canvas_id: str, new_id: str,
                  sizes: dict[str, tuple[int, int]] | None = None) -> Scene:
        """Give a part a layout for another canvas, starting from its main layout scaled to fit.

        The layout moves as one group: it shrinks or grows by the smaller of the two canvas ratios
        and keeps where it sits between the edges, so a webcam frame in the top-right corner stays a
        webcam frame in the top-right corner. `sizes` (source id -> size) gives the group's exact
        bounds; without them, positions just scale with the canvas. Nested parts get their own
        placement sources, because a placement's size follows the layout it sits in. Placements on
        that canvas are refitted so they show the new layout where they showed the old one.
        """
        part = self.main_part(part)
        existing = next((s for s in self.shapes(part) if s.canvas == canvas_id), None)
        if existing is not None:
            return existing
        src, dst = self.canvas(part.canvas), self.canvas(canvas_id)
        ratio = min(dst.width / src.width, dst.height / src.height)
        box = _bounds(part.items, sizes or {})
        if box is None:
            def move(x: float, y: float) -> tuple[float, float]:
                return x * dst.width / src.width, y * dst.height / src.height
        else:
            left, top, right, bottom = box
            new_left = _keep_between_edges(left, right - left, src.width, (right - left) * ratio, dst.width)
            new_top = _keep_between_edges(top, bottom - top, src.height, (bottom - top) * ratio, dst.height)

            def move(x: float, y: float) -> tuple[float, float]:
                return new_left + (x - left) * ratio, new_top + (y - top) * ratio
        items = []
        for item in part.items:
            source = self.source(item.source)
            if source.kind == PART_KIND:
                copy = source.model_copy(deep=True)
                copy.id = self.new_id(source.id)
                self.sources.append(copy)
                source = copy
            x, y = move(item.x, item.y)
            items.append(item.model_copy(update={"source": source.id, "x": x, "y": y, "scale": item.scale * ratio,
                                                 "scale_y": None if item.scale_y is None else item.scale_y * ratio}))
        shape = Scene(id=new_id, name=part.name, canvas=canvas_id, kind="part", shape_of=part.id, items=items)
        self.scenes.append(shape)
        self._refit_placements(part, canvas_id, (src.width, src.height), (dst.width, dst.height))
        return shape

    def make_part(self, scene: Scene, indices: list[int], name: str) -> tuple[Scene, SourceConfig]:
        """Move some of a scene's items into a new part and put the part where they were.

        The part is laid out on the scene's canvas and the items keep their positions, so nothing
        moves on screen. The placement goes where the top-most moved item was. Returns the part and
        its placement source.
        """
        chosen = sorted({i for i in indices if 0 <= i < len(scene.items)})
        if not chosen:
            raise ValueError("no items to make a part from")
        part = Scene(id=self.new_id("part"), name=name, canvas=scene.canvas, kind="part",
                     items=[scene.items[i] for i in chosen])
        self.scenes.append(part)
        placement = SourceConfig(id=self.new_id("part-src"), kind=PART_KIND, name=name, settings={"scene": part.id})
        self.sources.append(placement)
        at = chosen[-1] - (len(chosen) - 1)  # where the top-most item lands once the others are gone
        scene.items = [item for i, item in enumerate(scene.items) if i not in set(chosen)]
        scene.items.insert(at, SceneItem(source=placement.id))
        return part, placement

    def remove_shape(self, shape: Scene) -> None:
        """Drop a part's extra layout; shows on that canvas go back to its main layout, refitted."""
        if shape.shape_of is None:
            return
        part = self.main_part(shape)
        self.scenes = [s for s in self.scenes if s.id != shape.id]
        for item in shape.items:
            self.remove_source_if_unused(item.source)
        main = self.canvas(part.canvas)
        here = self.canvas(shape.canvas)
        self._refit_placements(part, shape.canvas, (here.width, here.height), (main.width, main.height))

    def _refit_placements(self, part: Scene, canvas_id: str, old: tuple[int, int], new: tuple[int, int]) -> None:
        """Placements of `part` on a canvas switch from a layout of size `old` to one of size `new`.

        Each keeps its centre and its size relative to "fit to canvas", so a part that filled the
        canvas still fills it.
        """
        canvas = self.canvas(canvas_id)
        fit_old = min(canvas.width / old[0], canvas.height / old[1])
        fit_new = min(canvas.width / new[0], canvas.height / new[1])
        placements = {s.id for s in self.sources if self.part_of(s) is part}
        for scene in self.scenes:
            if scene.canvas != canvas_id:
                continue
            for item in scene.items:
                if item.source not in placements:
                    continue
                w, h = item.cropped_size(old)
                cx, cy = item.x + w * item.scale / 2, item.y + h * item.sy / 2
                item.crop = (0, 0, 0, 0)
                item.scale = item.scale * fit_new / fit_old
                item.scale_y = None if item.scale_y is None else item.scale_y * fit_new / fit_old
                item.x = cx - new[0] * item.scale / 2
                item.y = cy - new[1] * item.sy / 2

    def bloom_key(self, scene_id: str | None) -> str | None:
        """The main bloom a scene belongs to: itself, or the scene it is linked to."""
        scene = next((s for s in self.scenes if s.id == scene_id), None)
        return (scene.linked_to or scene.id) if scene is not None else scene_id

    def transition_for(self, before: str | None, after: str | None) -> Transition:
        """The transition for switching from scene `before` to `after`.

        The most exact rule wins: from and to both named, then just the to bloom, then just the from
        bloom; with no rule it is the usual transition.
        """
        if self.transition_rules:
            a, b = self.bloom_key(before), self.bloom_key(after)
            for wanted in ((a, b), (ANY, b), (a, ANY)):
                for rule in self.transition_rules:
                    if (rule.from_scene, rule.to_scene) == wanted:
                        return rule.transition
        return self.transition

    def active_scene(self, canvas_id: str) -> Scene | None:
        scene_id = self.active_scenes.get(canvas_id)
        return self.scene(scene_id) if scene_id else None

    # Changes -------------------------------------------------------------

    def linked_with(self, scene_id: str) -> list[Scene]:
        """The scene and the scenes on other canvases that switch together with it."""
        scene = self.scene(scene_id)
        return [scene] + [o for o in self.scenes if o is not scene and o.kind == "show"
                          and o.canvas != scene.canvas and (o.linked_to == scene.id or scene.linked_to == o.id)]

    def set_active(self, scene_id: str) -> None:
        """Make a scene active on its canvas, and switch any scenes linked to it (in either direction)."""
        scene = self.scene(scene_id)
        if scene.kind == "part":
            return  # parts are only shown inside other scenes
        self.active_scenes[scene.canvas] = scene.id
        for other in self.scenes:
            if other.linked_to == scene.id or (scene.linked_to == other.id):
                self.active_scenes[other.canvas] = other.id

    def new_id(self, prefix: str) -> str:
        taken = {c.id for c in self.canvases} | {s.id for s in self.sources} | {s.id for s in self.scenes}
        taken |= {f.id for f in self.folders}
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
