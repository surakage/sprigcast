"""Scene collection: canvases, sources, scenes, outputs. Saved as JSON."""

from __future__ import annotations

import json
import math
import operator
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

FORMAT_VERSION = 2


DOWNSCALE_FILTERS = ("bilinear", "area", "bicubic", "lanczos")


class Canvas(BaseModel):
    """A canvas: the base size you build on. Viewers and recordings get the output size (scaled down if set)."""

    id: str  # "horizontal", "vertical"
    width: int
    height: int
    fps: int = 60
    enabled: bool = True
    output_short_side: int | None = None  # what's sent out: short side scaled to this (720: 1280x720); None = base
    downscale: Literal["bilinear", "area", "bicubic", "lanczos"] = "bicubic"  # how the output is scaled down

    def output_size(self) -> tuple[int, int]:
        """The size streams and recordings get by default: the base size, or scaled to output_short_side."""
        short = min(self.width, self.height)
        if not self.output_short_side or short <= self.output_short_side:
            return self.width, self.height
        k = self.output_short_side / short
        return max(2, round(self.width * k / 2) * 2), max(2, round(self.height * k / 2) * 2)


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

    @field_validator("filters", mode="before")
    @classmethod
    def _drop_retired_filters(cls, value):
        """Filters of features taken out of SprigCast (docs/REVISIT.md) leave older gardens as they load."""
        if isinstance(value, list):
            return [f for f in value if not (isinstance(f, dict) and f.get("kind") in RETIRED_FILTER_KINDS)]
        return value


RETIRED_FILTER_KINDS = ("vst", "background")  # VST3 sound filters, Background remover (removed 2026-11-17)

ALIGNS = ("top_left", "top", "top_right", "left", "center", "right", "bottom_left", "bottom", "bottom_right")
BOUNDS = ("off", "fit", "fill", "stretch")  # off: drawn at its scale; else squeezed into a box of bounds_size


def align_share(align: str) -> tuple[float, float]:
    """How far across and down a box the pin point sits: top_left (0, 0), center (0.5, 0.5)..."""
    i = ALIGNS.index(align) if align in ALIGNS else 4
    return (i % 3) / 2, (i // 3) / 2


CARD_W = "card_w"  # a clock card's settings: the box width its places are laid out for (keep_cards_put)


class SceneItem(BaseModel):
    source: str
    x: float = 0.0  # top-left corner of its box (unrotated), canvas pixels
    y: float = 0.0
    scale: float = 1.0
    # Height scale when the item is stretched (aspect unlocked); None keeps the shape (= scale).
    scale_y: float | None = None
    rotation: float = 0.0  # degrees, clockwise, around the pin point
    align: str = "center"  # the pin point: rotation turns around it and Place's X and Y are where it is
    crop: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom in source pixels
    bounds: str = "off"  # see BOUNDS
    bounds_size: tuple[float, float] = (0.0, 0.0)  # the box, canvas pixels (bounds on)
    bounds_align: str = "center"  # where the picture sits in its box when it doesn't fill it
    visible: bool = True
    locked: bool = False

    @property
    def sy(self) -> float:
        """The height scale: scale_y when stretched, else the same as the width scale."""
        return self.scale if self.scale_y is None else self.scale_y

    @property
    def bounded(self) -> bool:
        return self.bounds in BOUNDS[1:] and self.bounds_size[0] > 0 and self.bounds_size[1] > 0

    def drawn_size(self, source_size: tuple[int, int]) -> tuple[float, float]:
        """Width and height of its box on the canvas, in canvas pixels (crop and both scales, or the bounds)."""
        if self.bounded:
            return self.bounds_size
        w, h = self.cropped_size(source_size)
        return w * self.scale, h * self.sy

    def cropped_size(self, source_size: tuple[int, int]) -> tuple[int, int]:
        w, h = source_size
        left, top, right, bottom = self.crop
        return max(0, w - left - right), max(0, h - top - bottom)

    def picture_scale(self, source_size: tuple[int, int]) -> tuple[float, float]:
        """The scales the picture is actually drawn at (the bounds decide them when on)."""
        if not self.bounded:
            return self.scale, self.sy
        cw, ch = self.cropped_size(source_size)
        if not cw or not ch:
            return self.scale, self.sy
        bw, bh = self.bounds_size
        if self.bounds == "stretch":
            return bw / cw, bh / ch
        k = (min if self.bounds == "fit" else max)(bw / cw, bh / ch)
        return k, k

    def picture_rect(self, source_size: tuple[int, int]) -> tuple[float, float, float, float]:
        """The whole (cropped) picture inside its box: left, top, width, height; may spill out with "fill"."""
        cw, ch = self.cropped_size(source_size)
        sx, sy = self.picture_scale(source_size)
        pw, ph = cw * sx, ch * sy
        if not self.bounded:
            return 0.0, 0.0, pw, ph
        bw, bh = self.bounds_size
        bx, by = align_share(self.bounds_align)
        return (bw - pw) * bx, (bh - ph) * by, pw, ph

    def pin(self, source_size: tuple[int, int]) -> tuple[float, float]:
        """Where the pin point is on the canvas (rotation never moves it)."""
        w, h = self.drawn_size(source_size)
        ax, ay = align_share(self.align)
        return self.x + w * ax, self.y + h * ay

    def set_pin(self, source_size: tuple[int, int], x: float, y: float) -> None:
        w, h = self.drawn_size(source_size)
        ax, ay = align_share(self.align)
        self.x, self.y = x - w * ax, y - h * ay

    def set_align(self, source_size: tuple[int, int], align: str) -> None:
        """A new pin point, keeping the picture exactly where it is on the canvas."""
        before = self.quad(source_size)
        self.align = align
        after = self.quad(source_size)
        if before and after:
            self.x += before[0][0] - after[0][0]
            self.y += before[0][1] - after[0][1]

    def box(self, source_size: tuple[int, int]) -> list[tuple[float, float]]:
        """Its box's corners on the canvas (top-left, top-right, bottom-left, bottom-right), rotation applied.

        The same as quad()'s corners unless bounds leave room around the picture. Empty while the size is unknown."""
        if not self.quad(source_size):
            return []
        bw, bh = self.drawn_size(source_size)
        ax, ay = align_share(self.align)
        cx, cy = self.x + bw * ax, self.y + bh * ay
        rad = math.radians(self.rotation)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        ox, oy = -bw * ax, -bh * ay
        return [(cx + dx * cos_r - dy * sin_r, cy + dx * sin_r + dy * cos_r)
                for dx, dy in ((ox, oy), (ox + bw, oy), (ox, oy + bh), (ox + bw, oy + bh))]

    def quad(self, source_size: tuple[int, int]) -> list[tuple[float, float, float, float]]:
        """Corners as (x, y, u, v) in draw order top-left, top-right, bottom-left, bottom-right.

        x and y are canvas pixels; u and v are texture coordinates (0..1, v=0 is the image's top row).
        With bounds, the picture sits in its box (bounds_align) and "fill" cuts off what spills out.
        """
        sw, sh = source_size
        cw, ch = self.cropped_size(source_size)
        if sw == 0 or sh == 0 or cw == 0 or ch == 0:
            return []
        left, top, right, bottom = self.crop
        bw, bh = self.drawn_size(source_size)
        sx, sy = self.picture_scale(source_size)
        px, py, pw, ph = self.picture_rect(source_size)
        x0, y0, x1, y1 = max(0.0, px), max(0.0, py), min(bw, px + pw), min(bh, py + ph)  # the part inside the box
        if x1 <= x0 or y1 <= y0:
            return []
        u0, v0 = (left + (x0 - px) / sx) / sw, (top + (y0 - py) / sy) / sh
        u1, v1 = (left + (x1 - px) / sx) / sw, (top + (y1 - py) / sy) / sh
        ax, ay = align_share(self.align)
        cx, cy = self.x + bw * ax, self.y + bh * ay  # the pin point: everything turns around it
        rad = math.radians(self.rotation)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        ox, oy = -bw * ax, -bh * ay
        corners = [(ox + x0, oy + y0, u0, v0), (ox + x1, oy + y0, u1, v0), (ox + x0, oy + y1, u0, v1),
                   (ox + x1, oy + y1, u1, v1)]
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
    folder: str | None = None  # shows and main parts: the SceneFolder it's listed under (just for tidiness)
    states: list[SprigState] = Field(default_factory=list)  # main parts only: looks to switch between in Live
    state: str | None = None  # main parts only: the current state's name; None shows the sprig as built
    items: list[SceneItem] = Field(default_factory=list)  # index 0 draws first (bottom)


FOLDER_COLOURS = ("#ff5fa2", "#3ddc84", "#4cc9f0", "#ffd23f", "#ff8c42", "#b388ff")


class SceneFolder(BaseModel):
    """A named group of shows on one canvas, like "Main Scenes", or of parts, like "Browser Scenes".

    Folders only organise the lists; they never change what's on air. Part folders hold parts of
    every shape, so their canvas is just where they were made.
    """

    id: str
    name: str
    canvas: str
    colour: str = FOLDER_COLOURS[0]
    collapsed: bool = False
    live: bool = False  # its shows are the buttons in Live mode (see live_scenes)
    holds: Literal["show", "part"] = "show"


# Shares of a portrait canvas's height that TikTok, Shorts and Reels cover with their own tabs (top) and
# captions (bottom), from app/safe_zones.py: corner cards and bands start inside the rest.
PORTRAIT_TOP, PORTRAIT_BOTTOM = 0.12, 0.225
CARD_ROW = 0.14  # a corner card's height, as a share of the canvas's short side (Stream timer, Now playing)
PART_KIND = "scene"  # source kind that places a part inside another scene
SOUND_KINDS = ("audio_input", "audio_output", "app_audio")  # sounds: global, in no bloom, both shapes


def _bounds(items: list[SceneItem], sizes: dict[str, tuple[int, int]]) -> tuple[float, float, float, float] | None:
    """Left, top, right, bottom around the visible items whose size is known (rotation ignored)."""
    boxes = []
    for item in items:
        size = sizes.get(item.source)
        if not item.visible or not size:
            continue
        w, h = item.drawn_size(size)
        if w and h:
            boxes.append((item.x, item.y, item.x + w, item.y + h))
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
    account: str = ""  # YouTube channel or Kick username after Sign in ("" = not signed in): shown, never used to sign in
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
    """How video is encoded (Settings → Stream → Quality), like OBS's NVENC options. x264 gets the closest match."""

    encoder: str = "auto"  # an FFmpeg encoder name (h264_nvenc, hevc_amf, libx264...); auto = best on this PC
    x264_preset: str = "veryfast"  # x264 (CPU) only: ultrafast .. slow
    x264_tune: str = ""  # x264 only: "", film, animation, grain, stillimage, zerolatency
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


# Sound tracks 1-5 a recording can keep, by the mixer track each one is (audio/mixer.py):
# 1 everything, 2 everything except sounds kept out of VODs (music), 3 voice, 4 and 5 your own picks.
AUDIO_TRACKS = ("recording", "vod", "voice", "track4", "track5")
TRACK_NAMES = ("Everything", "VOD-safe", "Voice", "Track 4", "Track 5")
AUDIO_DEVICE_KINDS = ("audio_input", "audio_output")


class RecordingSettings(BaseModel):
    folder: str = ""  # empty: Videos/SprigCast
    canvases: list[str] = Field(default_factory=lambda: ["horizontal", "vertical"])  # which canvases to record
    bitrate_kbps: int = 12000
    container: Literal["mkv", "mp4"] = "mkv"
    replay_seconds: int = 30  # clip length (15, 30, 45 or 60 in Settings); saved clips land in the recordings folder
    clip_shapes: Literal["horizontal", "vertical", "both"] = "both"  # which canvases a clip saves
    clips_on_start: bool = False  # keep clips ready from the moment SprigCast opens
    # Clips kept ready with nothing streamed or recorded pause after this many minutes without a clip
    # saved (0: never). The buffer is an encoder running all the time: ~175 MB and ~11% of the GPU's
    # encoder for a whole idle hour on the user's PC.
    clips_idle_minutes: int = 15
    keep_screenshots: int = 0  # keep only the newest this many screenshots (0: keep all)
    keep_clips: int = 0  # keep only the newest this many clip files (0: keep all)
    voice_track: bool = True  # old setting, read once: now track 3 in audio_tracks (kept in step with it)
    audio_tracks: list[int] = Field(default_factory=lambda: [1, 3])  # which of sound tracks 1-5 recordings keep
    # Source record: a second file per canvas without these sources (picture and sound), e.g. no alerts.
    source_record: bool = False
    source_record_hide: list[str] = Field(default_factory=list)  # source ids left out
    short_side: int | None = None  # record shrunk so the short side is this (720: 1280x720); None = canvas size
    # Same as stream: while live, a canvas being streamed is recorded from the stream's own encoder (its size,
    # bitrate and stream sound) instead of a second encoder. Lighter on the PC, like OBS's "Same as stream".
    same_as_stream: bool = True  # on for everyone since the Settings review (2026-11-17): one encoder, not two
    # Split long recordings into several files, with nothing lost between them (0: one file). FAT32 drives
    # can't hold a file over 4 GB.
    record_when_live: bool = False  # going live starts a recording too, and ending the stream stops that one
    split_minutes: int = 0
    split_gb: float = 0.0
    encoder: EncoderSettings = Field(default_factory=lambda: EncoderSettings(rate_control="vbr", audio_kbps=192, b_frames=0))
    # No B-frames for recordings: NVENC needs ~50 MB of extra GPU-mapped buffers per encoder for them,
    # and at recording bitrates the quality difference is invisible. Streams skip them too since 2026-11.

    @model_validator(mode="before")
    @classmethod
    def _tracks_from_voice(cls, data):
        """Files from before tracks 1-5: "voice_track" on means tracks 1 and 3, off means track 1."""
        if isinstance(data, dict) and "audio_tracks" not in data and "voice_track" in data:
            data = {**data, "audio_tracks": [1, 3] if data["voice_track"] else [1]}
        return data

    @model_validator(mode="after")
    def _tidy_tracks(self) -> "RecordingSettings":
        tracks = sorted({t for t in self.audio_tracks if 1 <= t <= len(AUDIO_TRACKS)})
        self.audio_tracks = tracks or [1]  # a recording always has sound
        self.voice_track = 3 in self.audio_tracks
        return self

    def track_ids(self) -> tuple[str, ...]:
        """The mixer tracks a recording keeps, in track order (see AUDIO_TRACKS)."""
        return tuple(AUDIO_TRACKS[t - 1] for t in self.audio_tracks)


class AudioSettings(BaseModel):
    """Mixer-wide audio options. Per-source ones (volume, mute, monitor, hidden, duck_voice) live in
    each audio source's settings."""

    ducking: bool = False  # lower everything else while a voice source (your mic) is talking
    duck_amount_db: float = -14.0
    duck_threshold_db: float = -38.0
    duck_attack_ms: float = 250.0
    duck_release_ms: float = 900.0
    duck_hold_ms: float = 400.0
    track_names: list[str] = Field(default_factory=lambda: list(TRACK_NAMES))  # names of tracks 1-5 in files
    # Global devices: the Desktop audio and Mic set in Settings → Audio. Off: they are not opened at all
    # and leave the Sound list, so only the mics and desktop sounds you add yourself play.
    global_devices: bool = True
    global_sources: list[str] | None = None  # their source ids; None (older files): the first desktop and mic
    # Hearing it yourself: the device monitoring plays on ("Listen through"); "" = the Windows default.
    monitor_device: str = ""
    # Ask Windows not to turn other apps down when SprigCast opens a mic (communications ducking).
    windows_ducking_off: bool = True
    # Push to talk on the mic: "talk" (heard only while the key is held), "mute" (silent while held) or "off".
    push_mode: Literal["off", "talk", "mute"] = "off"
    push_key: str = ""  # the key, as hotkeys are written ("F13", "Ctrl+Alt+T")
    push_release_ms: int = 250  # "Let go after": letting go counts this much later (0, 100, 250 or 500)

    def push_mic(self, collection: "SceneCollection") -> str:
        """The mic push to talk holds: the global mic, else the first mic (its source id, "" for none)."""
        ids = collection.global_device_ids()
        mics = [s for s in collection.sources if s.kind == "audio_input"]
        return next((s.id for s in mics if s.id in ids), mics[0].id if mics else "")

    def track_name(self, number: int) -> str:
        names = [*self.track_names, *TRACK_NAMES[len(self.track_names):]]
        return names[number - 1].strip() or TRACK_NAMES[number - 1]


TRANSITION_KINDS = ("cut", "fade", "move", "slide", "swipe", "wipe", "leaf", "bloom", "stinger")
MOVE_EASINGS = ("smooth", "ease_out", "ease_in", "linear", "spring")  # render/move.py ease()
MOVE_UNMATCHED = ("fade", "scale", "slide")  # what is only in one of the two blooms


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
    bloom open the new scene through a growing leaf or flower; stinger plays a video over the swap; move
    glides what both blooms show from its old spot to its new one (render/move.py).
    """

    kind: Literal["cut", "fade", "move", "slide", "swipe", "wipe", "leaf", "bloom", "stinger"] = "fade"
    duration_ms: int = 300
    direction: Literal["left", "right", "up", "down"] = "left"  # slide, swipe and wipe: where it moves to
    softness: float = 0.15  # wipe, leaf and bloom: how soft the edge is, 0 sharp .. 1 very soft
    wipe_image: str = ""  # wipe: a black and white image, dark parts change first
    easing: Literal["smooth", "ease_out", "ease_in", "linear", "spring"] = "smooth"  # move: how it speeds up and settles
    unmatched: Literal["fade", "scale", "slide"] = "fade"  # move: what only one bloom shows fades, scales or slides
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


# SceneCollection._by_id's answers, by id(collection): (the sources and scenes they were made from, sources by id,
# scenes by id). Kept here, not on the model: a field or private attribute would count when two collections are
# compared.
_BY_ID: dict[int, tuple] = {}


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
        folders = {f.id: f for f in self.folders}
        for folder in self.folders:
            if folder.canvas not in canvas_ids:
                raise ValueError(f"folder {folder.id!r} uses unknown canvas {folder.canvas!r}")
        for scene in self.scenes:
            if scene.folder is not None and not self._fits_folder(scene, folders.get(scene.folder)):
                scene.folder = None  # a missing or wrong folder: just list it loose
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
        for source in self.sources:  # "SFX & alerts" is just "SFX" now (a name you typed yourself stays)
            if source.kind == "app_audio" and source.name == "SFX & alerts" and source.settings.get("preset") == "sfx":
                source.name = "SFX"
        # Rules for blooms that were deleted since are dropped quietly.
        self.transition_rules = [r for r in self.transition_rules
                                 if r.from_scene in scene_ids | {ANY} and r.to_scene in scene_ids | {ANY}]
        return self

    @staticmethod
    def _fits_folder(scene: Scene, folder: SceneFolder | None) -> bool:
        if folder is None or folder.holds != scene.kind:
            return False
        if scene.kind == "part":
            return scene.shape_of is None  # a part's other shapes are listed with it
        return folder.canvas == scene.canvas

    # Lookups -------------------------------------------------------------

    def canvas(self, canvas_id: str) -> Canvas:
        return next(c for c in self.canvases if c.id == canvas_id)

    def source(self, source_id: str) -> SourceConfig:
        return next(s for s in self.sources if s.id == source_id)

    def scene(self, scene_id: str) -> Scene:
        return next(s for s in self.scenes if s.id == scene_id)

    # Global audio devices ---------------------------------------------------------------

    def global_device_ids(self) -> list[str]:
        """The global Desktop audio and Mic: the ones listed, or (older files) the first of each kind."""
        ids = {s.id for s in self.sources}
        if self.audio.global_sources is not None:
            return [i for i in self.audio.global_sources if i in ids]
        firsts = [next((s.id for s in self.sources if s.kind == kind), None) for kind in AUDIO_DEVICE_KINDS]
        return [i for i in firsts if i]

    def device_off(self, source: SourceConfig) -> bool:
        """True for a global device while global devices are switched off: not opened, not listed."""
        return (not self.audio.global_devices and source.kind in AUDIO_DEVICE_KINDS
                and source.id in self.global_device_ids())

    def sources_in_use(self) -> list[SourceConfig]:
        """Every source the engine should open (all of them, less global devices that are off)."""
        if self.audio.global_devices:
            return list(self.sources)
        off = set(self.global_device_ids())
        return [s for s in self.sources if not (s.kind in AUDIO_DEVICE_KINDS and s.id in off)]

    # Sound per bloom ---------------------------------------------------------------------

    def bloom_choices(self) -> list[Scene]:
        """Blooms a sound can be set to play in: every bloom that isn't a twin of another (linked)."""
        return [s for s in self.scenes if s.kind == "show" and s.linked_to is None]

    def sound_blooms(self, source: SourceConfig) -> list[str]:
        """The blooms a sound plays in (settings["blooms"], deleted ones left out); [] means every bloom."""
        picked = source.settings.get("blooms") or []
        valid = {s.id for s in self.bloom_choices()}
        return [b for b in picked if b in valid]

    def sound_plays(self, source: SourceConfig) -> bool:
        """True when the sound plays in a bloom on air right now (on any shape that's switched on)."""
        blooms = self.sound_blooms(source)
        if not blooms:
            return True
        on_air = {self.bloom_key(self.active_scenes.get(c.id)) for c in self.canvases if c.enabled}
        on_air.discard(None)
        return not on_air or bool(on_air & set(blooms))

    def sounds_off_here(self) -> frozenset[str]:
        """Ids of the sounds that stay quiet in the blooms on air now (picked for other blooms only)."""
        return frozenset(s.id for s in self.sources if s.settings.get("blooms") and not self.sound_plays(s))

    def folders_for(self, canvas_id: str) -> list[SceneFolder]:
        """The bloom folders on a canvas."""
        return [f for f in self.folders if f.canvas == canvas_id and f.holds == "show"]

    def part_folders(self) -> list[SceneFolder]:
        """The sprig folders, like "Nested Scenes", "Audio Scenes" or "Browser Scenes"."""
        return [f for f in self.folders if f.holds == "part"]

    def folder(self, folder_id: str) -> SceneFolder:
        return next(f for f in self.folders if f.id == folder_id)

    def add_folder(self, canvas_id: str, name: str, holds: str = "show") -> SceneFolder:
        used = len(self.folders_for(canvas_id) if holds == "show" else self.part_folders())
        # The first bloom folder on a canvas is its Live folder, like "Main Scenes"; change it any time.
        folder = SceneFolder(id=self.new_id("folder"), name=name, canvas=canvas_id, holds=holds,
                             colour=FOLDER_COLOURS[(used + (holds == "part")) % len(FOLDER_COLOURS)],
                             live=holds == "show" and used == 0)
        self.folders.append(folder)
        return folder

    def remove_folder(self, folder_id: str) -> None:
        """The folder goes; what was in it stays, listed loose."""
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

    def grouped_parts(self) -> list[tuple[SceneFolder | None, list[Scene]]]:
        """The main parts as (folder, parts): loose ones first (folder None), then each sprig folder."""
        parts = self.parts()
        groups: list[tuple[SceneFolder | None, list[Scene]]] = [(None, [p for p in parts if p.folder is None])]
        groups += [(f, [p for p in parts if p.folder == f.id]) for f in self.part_folders()]
        return groups

    def parts_on(self, canvas_id: str) -> set[str]:
        """The ids of the (main) parts that belong with this canvas: each has a layout made for it, or a bloom
        on it (or a part that does) places it. One walk for all of them: asking part by part walked every scene
        again for every part and every part above it."""
        on: set[str] = set()
        inside: dict[str, list[str]] = {}  # a part -> the parts it places
        fresh: list[str] = []
        for scene in self.scenes:
            main = self.main_part(scene)
            placed = [p.id for p in self.parts_inside(scene)]
            if scene.kind == "part":
                inside.setdefault(main.id, []).extend(placed)
                if scene.canvas == canvas_id:
                    fresh.append(main.id)
            elif scene.kind == "show" and scene.canvas == canvas_id:
                fresh.extend(placed)
        while fresh:
            part_id = fresh.pop()
            if part_id not in on:
                on.add(part_id)
                fresh.extend(inside.get(part_id, ()))
        return on

    def part_on(self, part: Scene, canvas_id: str) -> bool:
        """True if a part belongs with this canvas: it has a layout made for it, or a bloom on it (or a part
        that does) places it. Build lists only these, so each canvas stays organised on its own; a part
        shared by both canvases shows on both. (For many parts ask parts_on once.)"""
        return self.main_part(part).id in self.parts_on(canvas_id)

    def sprig_kind(self, part: Scene, canvas_id: str | None = None, _seen: frozenset = frozenset()) -> str:
        """What a sprig shows, as a source kind for its icon: "camera" for a webcam sprig, "chat" for a chat
        one... A colour behind something else and its sound don't count; nested sprigs count as what they
        show. "empty" when it holds nothing yet, "nested" when it holds two or more sprigs (and nothing else),
        "scene" when it mixes kinds: the sprig icon."""
        part = self.main_part(part)
        if part.id in _seen:
            return "scene"
        layout = self.shape_for(part, canvas_id) if canvas_id else part
        kinds: list[str] = []
        sprigs = others = 0
        for item in layout.items:
            try:
                source = self.source(item.source)
            except StopIteration:
                continue
            inner = self.part_of(source) if source.kind == PART_KIND else None
            if inner is not None:
                sprigs += 1
                kind = self.sprig_kind(inner, canvas_id, _seen | {part.id})
                if kind != "empty":  # an empty sprig inside shows nothing
                    kinds.append(kind)
            else:
                others += 1
                kinds.append(source.kind)
        if sprigs >= 2 and not others:
            return "nested"
        if not kinds:
            return "empty"
        found = set(kinds)
        visual = found - {"audio_input", "audio_output", "app_audio"}
        if visual:
            found = visual
            if len(found) > 1:
                found.discard("color")  # a backdrop behind the real thing
        return found.pop() if len(found) == 1 else "scene"

    def grouped_parts_on(self, canvas_id: str) -> list[tuple[SceneFolder | None, list[Scene]]]:
        """grouped_parts() for one canvas: its parts only. A folder whose parts are all on the other canvas
        is left out; an empty folder (just made) stays, so things can be put in it."""
        out = []
        on = self.parts_on(canvas_id)
        for folder, parts in self.grouped_parts():
            here = [p for p in parts if p.id in on]
            if folder is None or here or not parts:
                out.append((folder, here))
        return out

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
        return self._by_id()[1].get(scene.shape_of, scene)

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
        part = self._by_id()[1].get(source.settings.get("scene"))
        return part if part is not None and part.kind == "part" and part.shape_of is None else None

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
        if not hidden or not any(item.visible and item.source in hidden for item in shape.items):
            return shape
        # A shallow copy sharing the items it doesn't change: views are only drawn, never edited. A deep copy of
        # each sprig was most of the renderer's sprig work when a bloom switch brought in 17 (PC, 2026-11-17).
        items = [item.model_copy(update={"visible": False}) if item.visible and item.source in hidden else item
                 for item in shape.items]
        return shape.model_copy(update={"items": items})

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

    def _by_id(self) -> tuple[dict[str, SourceConfig], dict[str, Scene]]:
        """(sources by id, scenes by id), the first of each id as a search from the front finds it.

        Made again only when a source or scene was added, removed or replaced: the look-ups below run for every
        sprig every frame and for every row of Build's list, and searching the lists each time took 0.8-1.1 s on
        the window's thread with a garden of 88 scenes and 149 sources (PC, 2026-11-19).
        """
        sources, scenes = self.sources, self.scenes
        kept = _BY_ID.get(id(self))
        if kept is not None:
            (old_sources, old_scenes), by_source, by_scene = kept
            if (len(old_sources) == len(sources) and len(old_scenes) == len(scenes)
                    and all(map(operator.is_, old_sources, sources)) and all(map(operator.is_, old_scenes, scenes))):
                return by_source, by_scene
        by_source = {s.id: s for s in reversed(sources)}
        by_scene = {s.id: s for s in reversed(scenes)}
        if len(_BY_ID) > 16:  # collections come and go (undo steps, copies for a save): no need to keep theirs
            _BY_ID.clear()
        _BY_ID[id(self)] = ((list(sources), list(scenes)), by_source, by_scene)
        return by_source, by_scene

    def parts_inside(self, scene: Scene) -> list[Scene]:
        """Parts placed directly in a scene (as main parts)."""
        out = []
        sources, scenes = self._by_id()
        for item in scene.items:
            source = sources.get(item.source)  # an item can outlive its source for a moment while it is removed
            if source is None or source.kind != PART_KIND:
                continue
            part = scenes.get(source.settings.get("scene"))
            if part is not None and part.kind == "part" and part.shape_of is None and part not in out:
                out.append(part)
        return out

    def inner_shapes(self, scene: Scene) -> list[Scene]:
        """The part layouts a scene draws: each placed part in the shape for the scene's canvas."""
        return [self.shape_for(p, scene.canvas) for p in self.parts_inside(scene)]

    def part_users(self) -> dict[str, list[Scene]]:
        """Every placed part's id -> the shows and parts that place it (a part counts once, whichever of its
        shapes places it). One walk for all parts: Build's list asks for each sprig's count."""
        users: dict[str, list[Scene]] = {}
        for s in self.scenes:
            placed = self.parts_inside(s)
            if placed:
                user = self.main_part(s)
                for p in placed:
                    by = users.setdefault(p.id, [])
                    if not any(u is user for u in by):
                        by.append(user)
        return users

    def part_uses(self, part_id: str) -> list[Scene]:
        """Shows and parts that place this part (a part counts once, whichever of its shapes places it)."""
        return self.part_users().get(part_id, [])

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
        for part in self.part_scenes():
            self._visit_parts(part, state, order)
        return order

    def _visit_parts(self, part: Scene, state: dict[str, str], order: list[Scene]) -> None:
        """part_order's walk. A method, not a nested function: one that calls itself is a reference cycle
        that Python's cleanup has to find, and part_order runs every frame."""
        if state.get(part.id) in ("done", "busy"):
            return
        state[part.id] = "busy"
        for inner in self.inner_shapes(part):
            self._visit_parts(inner, state, order)
        state[part.id] = "done"
        order.append(part)

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
                                                 "scale_y": None if item.scale_y is None else item.scale_y * ratio,
                                                 "bounds_size": (item.bounds_size[0] * ratio,
                                                                 item.bounds_size[1] * ratio)}))
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

    def bloom_sprig(self, show: Scene) -> Scene | None:
        """The sprig a bloom shows (blooms hold sprigs only): the one named after it, else its top one."""
        sources = {c.id: c for c in self.sources}
        parts = [p for p in (self.part_of(sources[i.source]) for i in reversed(show.items) if i.source in sources)
                 if p is not None]
        return next((p for p in parts if p.name == show.name), parts[0] if parts else None)

    def wrap_blooms(self) -> list[Scene]:
        """Blooms hold sprigs only. A bloom with anything else in it gets one sprig named after it, holding
        everything it had, placed full canvas, so nothing changes on air. A bloom's vertical twin (linked_to)
        becomes that sprig's vertical shape. New sprigs go in the Nested Scenes folder. Returns them."""
        made: list[Scene] = []
        by_bloom: dict[str, Scene] = {}
        kinds = {c.id: c.kind for c in self.sources}
        loose = [s for s in self.scenes if s.kind == "show" and any(kinds.get(i.source) != PART_KIND for i in s.items)]
        loose.sort(key=lambda s: s.linked_to is not None)  # main shapes first, their twins after
        for show in loose:
            twin = by_bloom.get(show.linked_to or "")
            if twin is not None and not any(sh.canvas == show.canvas for sh in self.shapes(twin)):
                shape = Scene(id=self.new_id(f"{twin.id}-{show.canvas}"), name=twin.name, canvas=show.canvas,
                              kind="part", shape_of=twin.id, items=show.items)
                self.scenes.append(shape)
                placement = SourceConfig(id=self.new_id("part-src"), kind=PART_KIND, name=twin.name,
                                         settings={"scene": twin.id})
                self.sources.append(placement)
                show.items = [SceneItem(source=placement.id)]
                continue
            taken = {p.name for p in self.parts()}
            name, n = show.name, 2
            while name in taken:
                name, n = f"{show.name} {n}", n + 1
            part, _placement = self.make_part(show, list(range(len(show.items))), name)
            folder = next((f for f in self.part_folders() if f.name == "Nested Scenes"), None) \
                or self.add_folder(show.canvas, "Nested Scenes", holds="part")
            part.folder = folder.id
            by_bloom[show.id] = part
            made.append(part)
        return made

    def own_sprig(self, scene: Scene, source_id: str, size: tuple[int, int], name: str,
                  corner: str | None = None, height_share: float | None = None,
                  band_share: float | None = None) -> Scene:
        """Put a source in a sprig of its own, on top of this bloom and the blooms linked to it (each
        shape gets a layout fitted to it). It fills the canvas (overlays like Snap), or with corner
        ("top_right" ...) it sits small in that corner (a clock); "bottom_center" is a wide band along
        the bottom (captions), above the bottom corners' cards. height_share: in a corner, its box is this share of the canvas's short side
        tall (a card, so its words start readable), instead of a third of it wide. band_share: a band's
        share of a landscape canvas's width (else 0.72). Sizes follow the canvas, so 720p and 1080p look
        the same. On a portrait canvas, corners and bands keep out of where TikTok, Shorts and Reels draw
        their tabs and captions (app/safe_zones.py). It starts in a free spot: if another plugin's sprig is
        already in that corner it takes the next free one, and a band narrows, then rises, to clear what's
        there (see _free_spot). Returns the sprig."""
        def fitted(canvas_id: str, there: Scene | None) -> SceneItem:
            if there is None:
                there = Scene(id="", name="", canvas=canvas_id)
            taken = self.overlay_boxes(there, canvas_id) if corner else []
            return self.readable_place(size, corner, height_share, band_share, there, source_id, taken)

        self._mark_card_box(source_id, size)
        blooms = self.linked_with(scene.id) if scene.kind == "show" else [scene]
        part = Scene(id=self.new_id("part"), name=name, canvas=scene.canvas, kind="part",
                     items=[fitted(scene.canvas, blooms[0] if blooms else scene)])
        self.scenes.append(part)
        for bloom in blooms[1:]:
            shape = self.add_shape(part, bloom.canvas, self.new_id("part"))
            shape.items = [fitted(bloom.canvas, bloom)]
        for bloom in blooms:
            placement = SourceConfig(id=self.new_id("part-src"), kind=PART_KIND, name=name, settings={"scene": part.id})
            self.sources.append(placement)
            bloom.items.append(SceneItem(source=placement.id))  # last is on top
        return part

    def _mark_card_box(self, source_id: str, size: tuple[int, int]) -> None:
        """A clock card placed now is laid out for its own_sprig width (see keep_cards_put)."""
        from sprigcast.core.source import source_class

        config = next((s for s in self.sources if s.id == source_id), None)
        if config is not None and getattr(source_class(config.kind), "card_box", None) and CARD_W not in config.settings:
            config.settings = {**config.settings, CARD_W: float(size[0])}

    def keep_cards_put(self, sizes: dict[str, tuple[int, int]]) -> tuple[bool, list[SourceConfig]]:
        """Clock cards (Countdown, Stream timer, Local time: a class with card_box) have a box as wide as the card,
        which changes with their settings. When one's width isn't the one its places were laid out for, move each
        place so the card stays put: one on the left of its canvas keeps its left edge, one on the right its right
        edge, one in the middle its centre. A card from before its box hugged it was laid out for the old box
        (card_box: (width, the side the card sat on in it)). Returns (whether a place moved: save the garden, the
        sources whose settings changed: if only those, the light way will do)."""
        from sprigcast.core.source import source_class

        moved, touched = False, []
        canvases = {c.id: c for c in self.canvases}
        for config in self.sources:
            box = getattr(source_class(config.kind), "card_box", None)
            size = sizes.get(config.id)
            if not box or not size or size[0] <= 0:
                continue
            new_w = float(size[0])
            laid = config.settings.get(CARD_W)
            try:
                laid_w, side = (float(laid), None) if laid is not None else (float(box[0]), box[1])
            except (TypeError, ValueError):
                laid_w, side = new_w, None
            if abs(new_w - laid_w) >= 0.5:
                for scene in self.scenes:
                    canvas = canvases.get(scene.canvas)
                    for item in scene.items:
                        if item.source != config.id or item.bounded or canvas is None:
                            continue
                        grow = (new_w - laid_w) * item.scale
                        where = side
                        if where is None:
                            middle = item.x + laid_w * item.scale / 2
                            where = "left" if middle < canvas.width / 3 else "right" if middle > canvas.width * 2 / 3 \
                                else "centre"
                        shift = grow if where == "right" else grow / 2 if where == "centre" else 0.0
                        if shift:
                            item.x -= shift
                            moved = True
            if laid is None or abs(new_w - laid_w) >= 0.5:
                config.settings = {**config.settings, CARD_W: new_w}
                touched.append(config)
        return moved, touched

    def overlay_boxes(self, scene: Scene, canvas_id: str) -> list[tuple[float, float, float, float]]:
        """Where the plugin sprigs on top of this bloom (a clock, now playing: a sprig holding one smaller
        thing) sit on its canvas, as (left, top, right, bottom). What fills the canvas doesn't count: the
        bloom's own sprig, a camera, Snap. Used to start a new one in a free spot."""
        from sprigcast.core.source import source_class  # plugin kinds register at run time

        canvas = self.canvas(canvas_id)
        configs = {s.id: s for s in self.sources}
        boxes = []
        for placed in scene.items:
            config = configs.get(placed.source)
            part = self.part_of(config) if config is not None else None
            if part is None:
                continue
            shape = self.shape_for(part, canvas_id)
            if len(shape.items) != 1:
                continue
            inner = shape.items[0]
            inner_config = configs.get(inner.source)
            cls = source_class(inner_config.kind) if inner_config is not None else None
            size = getattr(cls, "own_sprig", None)
            if not size:
                continue
            w, h = inner.drawn_size(tuple(size))
            if w * h >= 0.6 * canvas.width * canvas.height:
                continue
            k = placed.scale  # the placement is normally 1:1 at 0, 0
            left, top = placed.x + inner.x * k, placed.y + inner.y * k
            boxes.append((left, top, left + w * k, top + h * placed.sy))
        return boxes

    def shown_view(self, layout: Scene) -> tuple[float, float, float]:
        """How a sprig's layout shows on its canvas: (k, x, y), its point (u, v) lands at (x + k u, y + k v).
        Taken from the bloom on air (else the bloom showing it biggest), through every sprig it sits in; a
        bloom, or a sprig no bloom shows, is (1, 0, 0). Used so a plugin added to a sprig shown at a third of
        the size starts big enough to read where it shows (PC test 2026-11-17: "miniscule")."""
        if layout.kind != "part":
            return 1.0, 0.0, 0.0
        configs = {s.id: s for s in self.sources}
        canvases = {c.id: (c.width, c.height) for c in self.canvases}
        found: list[tuple[float, float, float]] = []

        def walk(scene: Scene, k: float, x: float, y: float, path: tuple[str, ...]) -> None:
            for placed in scene.items:
                config = configs.get(placed.source)
                part = self.part_of(config) if config is not None else None
                if part is None or part.id in path or len(path) >= 16:
                    continue
                shape = self.shape_for(part, scene.canvas)
                size = canvases.get(shape.canvas)
                if not size:
                    continue
                left, top, _, _ = placed.picture_rect(size)
                sx, _ = placed.picture_scale(size)
                view = (k * abs(sx), x + k * (placed.x + left), y + k * (placed.y + top))
                if shape.id == layout.id:
                    found.append(view)
                else:
                    walk(shape, *view, (*path, part.id))

        on_air = self.active_scenes.get(layout.canvas)
        blooms = [b for b in self.scenes if b.kind == "show" and b.canvas == layout.canvas]
        blooms.sort(key=lambda b: b.id != on_air)
        for bloom in blooms:
            walk(bloom, 1.0, 0.0, 0.0, (bloom.id,))
            if found and bloom.id == on_air:
                return found[0]
        return max(found, key=lambda v: v[0]) if found else (1.0, 0.0, 0.0)

    def view_region(self, layout: Scene) -> tuple[float, float, float, float, float, float, float]:
        """Where a layout shows on its canvas: (k, x, y) from shown_view, and the part of the canvas it covers
        (left, top, width, height), in canvas pixels."""
        canvas = self.canvas(layout.canvas)
        k, x, y = self.shown_view(layout)
        left, top = max(0.0, x), max(0.0, y)
        right, bottom = min(float(canvas.width), x + k * canvas.width), min(float(canvas.height), y + k * canvas.height)
        if right - left < 1 or bottom - top < 1:  # shown off the canvas: as if it filled it
            return 1.0, 0.0, 0.0, 0.0, 0.0, float(canvas.width), float(canvas.height)
        return k, x, y, left, top, right - left, bottom - top

    def readable_place(self, size: tuple[int, int], corner: str | None, height_share: float | None,
                       band_share: float | None, layout: Scene, source_id: str,
                       taken: list[tuple[float, float, float, float]] | None = None) -> SceneItem:
        """A plugin's starting place in this layout (see _free_spot): its size and corner as on the canvas, then
        turned into the layout's own units, so in a sprig shown small it still starts readable where it
        shows, inside the part of the canvas the sprig covers. taken: boxes in the layout's units."""
        canvas = self.canvas(layout.canvas)
        k, vx, vy, left, top, width, height = self.view_region(layout)
        whole = k == 1.0 and vx == 0.0 and vy == 0.0
        to_canvas = [(vx + k * b[0], vy + k * b[1], vx + k * b[2], vy + k * b[3]) for b in (taken or [])]
        if whole:
            room, short = canvas, None
        else:  # the part of the canvas the sprig covers, with sizes still taken from the whole canvas
            room, short = Canvas(id=canvas.id, width=max(1, round(width)), height=max(1, round(height))), \
                min(canvas.width, canvas.height)
            to_canvas = [(b[0] - left, b[1] - top, b[2] - left, b[3] - top) for b in to_canvas]
        if corner:
            spot = _free_spot(room, size, corner, height_share, band_share, to_canvas, source_id, short)
        else:  # an overlay (Snap): fills what shows
            scale = min(room.width / size[0], room.height / size[1])
            spot = SceneItem(source=source_id, scale=scale, x=(room.width - size[0] * scale) / 2,
                             y=(room.height - size[1] * scale) / 2)
        if whole:
            return spot
        gx, gy = left + spot.x, top + spot.y  # on the canvas, then in the sprig's own units
        return SceneItem(source=source_id, scale=spot.scale / k, x=(gx - vx) / k, y=(gy - vy) / k)

    def starting_place(self, item: SceneItem, kind: str, layout: Scene) -> None:
        """Give an item that's about to go into this layout the place and size the + menu gives it, for things
        added another way (the Control API's item.add, OBS's CreateInput and CreateSceneItem): a plugin's card
        starts readable in a free corner (see _free_spot), a band along the bottom, an overlay like Snap fills
        the canvas, and text is the same share of the canvas at 720p, 1080p or 4K. Anything else keeps its
        place (0, 0, as big as the source), as in OBS."""
        from sprigcast.core.source import source_class  # plugin kinds register at run time

        cls = source_class(kind)
        if cls is None:
            return
        canvas = self.canvas(layout.canvas)
        size = getattr(cls, "own_sprig", None)
        corner = getattr(cls, "own_sprig_corner", None)
        if size:
            self._mark_card_box(item.source, tuple(size))
            taken = self.overlay_boxes(layout, layout.canvas) + self.plugin_boxes(layout) if corner else []
            spot = self.readable_place(tuple(size), corner, getattr(cls, "own_sprig_height", None),
                                       getattr(cls, "own_sprig_band", None), layout, item.source, taken)
            item.x, item.y, item.scale, item.scale_y = spot.x, spot.y, spot.scale, None
        elif getattr(cls, "sharp", False):  # text: the same share of the canvas, however big the sprig shows
            k = self.shown_view(layout)[0]
            item.scale, item.scale_y = min(canvas.width, canvas.height) / 1080 / max(k, 0.01), None

    def plugin_boxes(self, layout: Scene) -> list[tuple[float, float, float, float]]:
        """Where the plugin cards placed straight in this layout (not in a sprig of their own) sit, as (left,
        top, right, bottom): what starting_place keeps clear of. Overlays that fill the canvas don't count."""
        from sprigcast.core.source import source_class

        canvas = self.canvas(layout.canvas)
        configs = {s.id: s for s in self.sources}
        boxes = []
        for placed in layout.items:
            config = configs.get(placed.source)
            cls = source_class(config.kind) if config is not None else None
            size = getattr(cls, "own_sprig", None)
            if not size or not getattr(cls, "own_sprig_corner", None):
                continue
            w, h = placed.drawn_size(tuple(size))
            if w * h >= 0.6 * canvas.width * canvas.height:
                continue
            boxes.append((placed.x, placed.y, placed.x + w, placed.y + h))
        return boxes

    def new_bloom(self, name: str, canvas_ids: list[str]) -> Scene:
        """A new empty bloom (what the API's bloom.new and OBS's CreateScene make): one bloom per canvas, switching
        together, holding one sprig named after it placed full canvas, so things added to the bloom go in its
        sprig as they do in Build. Returns the bloom on the first canvas."""
        first: Scene | None = None
        for canvas_id in canvas_ids:
            bloom = Scene(id=self.new_id("scene"), name=name, canvas=canvas_id,
                          linked_to=first.id if first is not None else None)
            self.scenes.append(bloom)
            self.active_scenes.setdefault(canvas_id, bloom.id)
            first = first or bloom
        assert first is not None
        self.give_sprig(first)
        return first

    def give_sprig(self, bloom: Scene) -> Scene:
        """The sprig a bloom shows; an empty bloom (and its twins) gets one named after it first, with a layout
        per canvas, placed full canvas."""
        found = self.bloom_sprig(bloom)
        if found is not None:
            return found
        family = self.linked_with(bloom.id)
        taken = {p.name for p in self.parts()}
        name, n = bloom.name, 2
        while name in taken:
            name, n = f"{bloom.name} {n}", n + 1
        folder = next((f for f in self.part_folders() if f.name == "Nested Scenes"), None)
        part = Scene(id=self.new_id("part"), name=name, canvas=family[0].canvas, kind="part",
                     folder=folder.id if folder is not None else None)
        self.scenes.append(part)
        for member in family:
            if member.canvas != part.canvas and not any(sh.canvas == member.canvas for sh in self.shapes(part)):
                self.scenes.append(Scene(id=self.new_id(f"{part.id}-{member.canvas}"), name=name,
                                         canvas=member.canvas, kind="part", shape_of=part.id))
            placement = SourceConfig(id=self.new_id("part-src"), kind=PART_KIND, name=name, settings={"scene": part.id})
            self.sources.append(placement)
            member.items.append(SceneItem(source=placement.id))
        return part

    def remove_bloom(self, bloom: Scene) -> None:
        """Remove a bloom and its twins on other canvases. Its sprig goes too unless another bloom shows it;
        sources stay while anything else uses them."""
        blooms = self.linked_with(bloom.id)
        gone = {b.id for b in blooms}
        sprigs = [p for p in (self.part_of(self.source(i.source)) for b in blooms for i in b.items
                              if any(c.id == i.source for c in self.sources)) if p is not None]
        self.scenes = [s for s in self.scenes if s.id not in gone]
        for other in self.scenes:
            if other.linked_to in gone:
                other.linked_to = None
        for canvas_id, scene_id in list(self.active_scenes.items()):
            if scene_id in gone:
                rest = self.scenes_for(canvas_id)
                if rest:
                    self.active_scenes[canvas_id] = rest[0].id
                else:
                    self.active_scenes.pop(canvas_id)
        for b in blooms:
            for item in b.items:
                self.remove_source_if_unused(item.source)
        for sprig in {p.id: p for p in sprigs}.values():
            if not self.part_uses(sprig.id) and not any(self.part_of(c) is sprig for c in self.sources):
                shapes = self.shapes(sprig)
                self.scenes = [s for s in self.scenes if s not in shapes]
                for shape in shapes:
                    for item in shape.items:
                        self.remove_source_if_unused(item.source)

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
                if item.bounded:  # its box stays; the new layout fits into it
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
            # A capture's own sound (core/capture_sound.py) goes with it.
            self.sources = [s for s in self.sources
                            if s.id != source_id and s.settings.get("capture_of") != source_id]

    # Files ---------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "SceneCollection":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    @classmethod
    def from_json(cls, text: str) -> "SceneCollection":
        return cls.model_validate_json(text)

    def snapshot(self) -> "SceneCollection":
        """A full copy, independent of this one, made in pydantic's compiled code. model_copy(deep=True) walks
        everything in Python: while live, other threads share Python's lock and one copy took 300+ ms on the
        window's thread (PC test, 2026-11-17). This is a few ms and barely gives the lock away."""
        return type(self).model_validate(self.model_dump())

    def to_text(self) -> str:
        """The garden as saved (JSON, indented), written by compiled code. json.dumps with an indent runs in
        Python, slow on the window's thread while live. -inf and NaN stay as they were (not null)."""
        from pydantic_core import to_json

        return to_json(self.model_dump(), indent=2, inf_nan_mode="constants").decode("utf-8")

    def save(self, path: Path, text: str | None = None) -> None:
        """Write it now (on this thread). Any save of the same file queued earlier (save_later) is dropped."""
        write_garden(path, text if text is not None else self.to_text(), next_save(path))

    def save_later(self, path: Path) -> None:
        """Write it on a worker: the text is taken now, the disk (and its backup copies) wait for the worker.
        A newer save of the same file, now or later, wins over this one."""
        from sprigcast.core import workers

        text, ticket = self.to_text(), next_save(path)
        workers.run(write_garden, path, text, ticket, name="garden-save")


# Saves of a garden file: one at a time, newest wins. A save queued on a worker (save_later) that a newer one
# (on any thread) overtook is skipped, so an older garden never lands on top of a newer one.
_save_lock = threading.Lock()
_save_tickets: dict[str, int] = {}


def next_save(path: Path) -> int:
    with _save_lock:
        key = str(Path(path))
        _save_tickets[key] = _save_tickets.get(key, 0) + 1
        return _save_tickets[key]


def write_garden(path: Path, text: str, ticket: int) -> bool:
    """Write the file if this is still the newest save asked for. True if written."""
    path = Path(path)
    with _save_lock:
        if _save_tickets.get(str(path), 0) != ticket:
            return False  # a newer save came along: it writes (or wrote) the file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        from sprigcast.core import backups

        backups.keep(path)  # the last good save, and one copy a day (core/backups.py)
        tmp.replace(path)  # atomic on the same drive, so a crash never leaves half a file
        return True


CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
PART_CORNER_W = 0.45  # in a sprig shown small, a corner plugin is at most this share of the part's width
PART_CORNER_H = 0.3  # ...and of its height
SIDE_BUTTONS = 0.15  # share of a portrait canvas's width TikTok, Shorts and Reels use for buttons down the right
SIDE_BUTTONS_FROM = 0.36  # ...from this share of its height down


def _overlaps(a, b, gap: float = 0.0) -> bool:
    return a[0] < b[2] + gap and b[0] < a[2] + gap and a[1] < b[3] + gap and b[1] < a[3] + gap


def _free_spot(canvas: Canvas, size: tuple[int, int], corner: str, height_share: float | None,
               band_share: float | None, taken: list, source_id: str, short: float | None = None) -> SceneItem:
    """A plugin's starting place (SceneCollection.own_sprig): its corner, or the next free corner when another
    plugin is there, and with every corner taken it stacks above (or below) what's in its own; a band along the
    bottom narrows (to 60% of its width at most), then rises (up to under the top corners' cards) to clear
    them. If nothing is free it takes its own spot anyway. short: the whole canvas's short side, when `canvas`
    is only the part of it a sprig covers (sizes follow the whole canvas, but never more than fits the part)."""
    room = min(canvas.width, canvas.height)
    part = short is not None  # only part of the canvas: TikTok's bars are the whole canvas's, not this part's
    short = room if short is None else short
    margin = min(short * 0.035, room * 0.05)
    portrait = canvas.height > canvas.width and not part
    card_row = min(short * CARD_ROW, PART_CORNER_H * canvas.height) if part else short * CARD_ROW  # corner cards' height
    top = max(margin, canvas.height * PORTRAIT_TOP) if portrait else margin
    bottom = max(margin, canvas.height * PORTRAIT_BOTTOM) if portrait else margin

    def placed(scale: float, x: float, y: float) -> SceneItem:
        return SceneItem(source=source_id, scale=scale, x=x, y=y)

    if "center" in corner:  # a wide band across the middle (captions): most of the width
        tall = canvas.height > canvas.width  # a phone canvas, or a tall part of one
        scale = canvas.width * (0.9 if tall else band_share or 0.72) / size[0]
        if part:
            scale = min(scale, PART_CORNER_H * canvas.height / size[1])
        if "bottom" in corner:  # above the bottom corners' cards (now playing), not on them
            bottom += card_row + margin
        floor = canvas.height - bottom
        if floor - size[1] * scale < margin:  # a small part: no room above the cards, so at its bottom
            floor = canvas.height - margin

        def band(k: float, y_bottom: float):
            w, h = size[0] * k, size[1] * k
            return ((canvas.width - w) / 2, y_bottom - h, (canvas.width + w) / 2, y_bottom)

        first = band(scale, floor)
        hit = [b for b in taken if _overlaps(first, b)]
        if not hit:
            return placed(scale, first[0], first[1])
        middle = canvas.width / 2  # narrower, still centred, between what's at the sides
        room = min(min(abs(b[0] - middle), abs(b[2] - middle)) for b in hit if b[0] > middle or b[2] < middle) \
            if any(b[0] > middle or b[2] < middle for b in hit) else 0.0
        narrow = 2 * (room - margin) / size[0]
        if narrow >= scale * 0.6:
            box = band(min(scale, narrow), floor)
            if not any(_overlaps(box, b) for b in taken):
                return placed(min(scale, narrow), box[0], box[1])
        for k in ((1.0,) if not part else (1.0, 0.8, 0.64, 0.5)):  # a crowded small part: a smaller band
            y_bottom = floor  # rise above them
            for _ in range(len(taken) + 1):
                box = band(scale * k, y_bottom)
                hit = [b for b in taken if _overlaps(box, b)]
                if not hit:
                    if box[1] >= top + (0 if part else card_row + margin) - 0.5:  # under the top corners' cards
                        return placed(scale * k, box[0], box[1])
                    break
                y_bottom = min(b[1] for b in hit) - margin
        return placed(scale, first[0], first[1])

    if height_share:
        scale = short * height_share / size[1]
    else:
        scale = short * 0.36 / size[0]  # about a third of the short side
    if part:  # in a small part, small enough that the four corners still fit side by side
        if canvas.height > canvas.width:  # a tall part: cards stack down its sides instead
            wide, tall = 0.9, PART_CORNER_H
        else:
            wide, tall = PART_CORNER_W * (1.4 if size[0] > 4 * size[1] else 1.0), PART_CORNER_H  # a long card hugs its words
        scale = min(scale, wide * canvas.width / size[0], tall * canvas.height / size[1])
    w, h = size[0] * scale, size[1] * scale

    def at(spot: str):
        x = margin if "left" in spot else canvas.width - w - margin
        y = top if "top" in spot else canvas.height - h - bottom
        if portrait and "right" in spot and y + h > canvas.height * SIDE_BUTTONS_FROM:
            x -= canvas.width * SIDE_BUTTONS  # clear of the like / comment / share buttons
        return (x, y, x + w, y + h)

    vertical, side = corner.split("_", 1) if "_" in corner else ("bottom", "left")
    other_side = "right" if side == "left" else "left"
    other_vertical = "top" if vertical == "bottom" else "bottom"
    order = [corner, f"{vertical}_{other_side}", f"{other_vertical}_{side}", f"{other_vertical}_{other_side}"]
    for spot in [c for c in order if c in CORNERS] or [corner]:
        box = at(spot)
        if not any(_overlaps(box, b) for b in taken):
            return placed(scale, box[0], box[1])
    box = at(corner if corner in CORNERS else "bottom_left")  # every corner taken: stack on its own corner's
    for _ in range(len(taken) + 1):  # neighbour, above it (or below it, at the top)
        hit = [b for b in taken if _overlaps(box, b)]
        if not hit:
            return placed(scale, box[0], box[1])
        y = min(b[1] for b in hit) - margin - h if "bottom" in corner else max(b[3] for b in hit) + margin
        if y < top - 0.5 or y + h > canvas.height - bottom + 0.5:
            break
        box = (box[0], y, box[2], y + h)
    box = at(corner if corner in CORNERS else "bottom_left")
    return placed(scale, box[0], box[1])


def drawing_fps(canvases) -> int:
    """How often the canvases are drawn: the fastest canvas that is on. A canvas that is off (the vertical one on a
    light PC) doesn't count: a 30 fps canvas beside a 60 fps one that's off ticked at 60 (light-PC test
    2026-11-19, twice the work on a weak PC)."""
    on = [c for c in canvases if c.enabled] or list(canvases)
    return int(max((c.fps for c in on), default=60))
