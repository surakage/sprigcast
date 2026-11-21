"""The render thread: paces ticks, applies UI commands, draws every enabled canvas."""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from types import SimpleNamespace
from typing import Any

import numpy as np

from sprigcast.audio import listen
from sprigcast.audio.mixer import Ducking
from sprigcast.audio.native_mixer import make_mixer, make_monitor
from sprigcast.audio.monitor import Monitor  # noqa: F401 (Python's mixer plays with it, via make_monitor)
from sprigcast.core.events import (
    AudioLevels,
    EngineError,
    ReplaceCollection,
    Shutdown,
    SourceInfo,
    Stats,
    SwitchBlooms,
    UpdateSourceSettings,
)
from sprigcast.core.filters import SOUND_LEVELS, AudioChain, video_effects, with_item
from sprigcast.core import drawn, pacing, retire
from sprigcast.core.mailbox import Mailbox
from sprigcast.core.scene import PART_KIND, STATE_FADE_S, Scene, SceneCollection, SceneItem, drawing_fps, item_key
from sprigcast.core.source import Source, create_source, source_class
from sprigcast.render import move
from sprigcast.render.stinger import StingerPlayer, open_stinger

log = logging.getLogger(__name__)


SLEEP_AFTER = 5.0  # seconds a source with its eye off (in a bloom still on screen) stays ready, so a quick
# hide and show is instant. Off air, a source sleeps as soon as nothing draws it: when a cut lands, or when a
# transition out of its bloom ends (the user's rule, 2026-11-16: the transition is the handover).
# Cameras and capture cards take a second or two to open, so they wait longer before closing; closing
# them lets the camera's own app (Elgato Camera Hub: 11% CPU, 22% GPU on the user's PC) go idle too.
CAMERA_SLEEP_AFTER = 30.0
# A camera that's needed again soon after it closed waits longer next time: twice as long each time it comes
# back within CAMERA_BACK_SOON_S, up to CAMERA_SLEEP_MAX. On the PC (2026-11-16) one closed and opened 14 times
# an hour as blooms switched, each time starting Camera Hub again. Left closed that long, it's back to 30 s.
CAMERA_SLEEP_MAX = 300.0
CAMERA_BACK_SOON_S = 600.0
GRAPHICS_LOST_TICKS = 30  # frames failing this many ticks in a row: the graphics driver reset, start graphics again
GRAPHICS_RETRY_S = 2.0  # while a new start of the graphics fails, try again this often
# Starts of the graphics close together wait longer each time (2, 4, 8 ... up to 60 s): each one makes every
# source again. The native simple run (2026-11-18) restarted ~10 times a minute for 5 minutes on a picture that
# kept failing, and memory climbed ~1 GB a minute.
GRAPHICS_RESTART_CALM_S = 120.0  # a start this long after the last one counts as the first again
GRAPHICS_RESTART_MAX_S = 60.0
FIRST_UPLOAD_BYTES = 24 * 1024 * 1024  # first pictures after a wake copied to the card per tick, at most (+1)
WAKE_BUDGET_S = 0.004  # waking sources takes at most about this of one tick; the rest wake on the next ticks
WAKE_LOG_S = 0.003  # a source this slow on a frame that woke some is named in the "waking frame" log line
SLOW_STEP_S = 0.008  # a step of a frame this long: note where the window's thread is (the late-frame line says)
LATE_LOG_EVERY_S = 10.0  # late-frame log lines at most this often (the rest are counted in the next one)
GPU_STALL_S = 0.3  # the card behind this long stopped the whole app (the window too): log who was using it
GPU_SKIP_MAX_S = 2.0  # the card stuck longer than this (a lost device?): draw anyway, waiting as before
SOURCE_BUDGET_MS = 5.0  # a source averaging more than this per frame is drawn less often (see _charge)
CHARGE_CAP_MS = 4 * SOURCE_BUDGET_MS  # the most one frame adds to a source's average (_charge)
BUDGET_MARGIN = 0.8  # ...and drawn more often again only under this share of the lower step's budget
WAKE_WAIT_S = 2.0  # a switch to a bloom whose camera is closed shows the old bloom until it opens, at most this
STAGED_VIEW = "~next:"  # + canvas id: the engine's view of the bloom Build edits there (Engine._native_views)
PART_VIEW = "~part"  # the engine's view of the sprig open in Build
PREVIEW_FPS = 30  # the UI draws previews about 30 times a second; copying more is wasted
FAIL_RETRY_S = 1.0  # a source whose picture keeps failing is asked again about once a second, not every frame
CLOSE_ALL_S = 4.0  # quitting: sources close side by side for at most this long


class Frame:
    """One finished canvas image, top row first: RGBA bytes, or NV12 (Y plane then CbCr) at an output's size."""

    __slots__ = ("canvas_id", "width", "height", "data", "time", "fmt", "canvas_size")

    def __init__(self, canvas_id: str, width: int, height: int, data: bytes, time: float = 0.0, fmt: str = "rgba",
                 canvas_size: tuple[int, int] | None = None):
        self.canvas_id, self.width, self.height, self.data = canvas_id, width, height, data
        self.time = time  # perf_counter() at the start of the tick that drew it
        self.fmt = fmt
        self.canvas_size = canvas_size or (width, height)  # the canvas it shows (a preview may be smaller)


def _engine_writer(callback) -> bool:
    """Whether a frame tap's owner (a Recorder) goes through the engine's own writer (Recorder.uses_writer), so
    the native engine can hand it its frames itself (link_writer): asked whatever the writer is doing right now,
    so its output stays the same for the whole recording."""
    owner = getattr(callback, "__self__", None)
    uses = getattr(owner, "uses_writer", None)
    if owner is None or uses is None or not callable(getattr(owner, "link_writer", None)):
        return False
    try:
        return bool(uses())
    except Exception:  # noqa: BLE001 (never let a question break the tick)
        return False


def _engine_outputs(native) -> int:
    from sprigcast import sprigcore

    return int(getattr(native, "max_outputs", sprigcore.ENGINE_OUTPUTS))


def _wants_gpu(callback) -> bool:
    """Whether a frame tap's owner (a Recorder) takes frames on the card right now (wants_gpu_frames)."""
    wants = getattr(getattr(callback, "__self__", None), "wants_gpu_frames", None)
    if wants is None:
        return False
    try:
        return bool(wants())
    except Exception:  # noqa: BLE001 (never let a question break the tick)
        return False


WARM_STINGERS = 4  # stinger clips kept open ahead of a switch, at most
STINGER_WAIT_S = 2.0  # a stinger clip that has not started by then is swapped for a quick fade


def stinger_path(transition, canvas) -> str:
    """The clip a canvas plays: the vertical one on a tall canvas when there is one."""
    sting = transition.stinger
    if canvas.height > canvas.width and sting.vertical_path:
        return sting.vertical_path
    return sting.path


@dataclass
class Running:
    """A transition in progress on one canvas."""

    outgoing: object  # the Scene it changes from
    began: float | None  # perf_counter time; None while a stinger clip is still opening
    duration: float  # seconds
    settings: object  # the Transition
    stinger: StingerPlayer | None = None
    wipe_map: str = ""  # wipe image path
    queued: float = field(default_factory=time.perf_counter)


class Engine:
    def __init__(self, collection: SceneCollection):
        self.commands: queue.Queue = queue.Queue()  # UI -> engine
        self.events: queue.Queue = queue.Queue()  # engine -> UI
        self.frames: dict[str, Mailbox[Frame]] = {c.id: Mailbox() for c in collection.canvases}
        self.part_frames: Mailbox[Frame] = Mailbox()  # the part open in the Part editor, if any
        self.editing_part: str | None = None  # set by the UI: which part to read back for the Part editor
        # This tick's sprig textures and which sprigs were drawn (screenshots redraw the ones holding Snap).
        self._part_textures: dict = {}
        self._parts_used: set[str] = set()
        self._collection = collection.model_copy(deep=True)
        self._plan = None  # see _sprig_plan
        self._sources: dict[str, Source] = {}
        self._stopped_unknown: set[str] = set()  # sources whose plugin was removed while they ran
        self._thread = threading.Thread(target=self._run, name="sprigcast-render", daemon=True)
        self._stopping: threading.Thread | None = None  # see stop(wait=False)
        self.mixer = make_mixer(lambda levels: self.events.put(AudioLevels(levels)))  # the engine's, or Python's
        self.monitor = make_monitor(self.mixer)  # plays monitored sources on the speakers, opened only when one is
        self._monitoring = False
        self._taps: dict[str, list[Callable[[Frame], None]]] = {}  # canvas id -> recorders, later streams
        self._taps_lock = threading.Lock()
        self._grabs: list[tuple] = []  # (source id, callback, raw, limit, filters): see grab_source
        self._transitions: dict[str, Running] = {}  # canvas id -> the transition it is in
        self._warm: dict[tuple, object] = {}  # (path, limit, gain) -> a stinger player opened ahead (_warm_stingers)
        self._ending = False  # the render loop is ending: nothing more is opened ahead
        self._warm_lock = threading.Lock()
        self._warming: dict[tuple, threading.Thread] = {}  # stingers being opened ahead, on a thread of their own
        self._warm_wanted: list[tuple] = []
        self._filtered: frozenset[str] = frozenset()  # sources with video filters, see _sync_filters
        # canvas id -> (outgoing scene, give up at, sources still opening): a switch waiting for them
        self._waking: dict[str, tuple] = {}
        # Sources that had a picture when they went to sleep (their texture let go): a switch back to their bloom
        # waits for their first picture, as for a camera, so the first uploads can be spread over a few ticks.
        self._had_picture: set[str] = set()
        self._sprigs_held: frozenset = frozenset()  # canvases whose new bloom's sprigs weren't drawn this tick
        self._woke = 0  # sources woken this tick (the waking-frame log line)
        self._source_ms: dict[str, float] = {}  # source id -> average ms per frame it was asked for
        self._source_every: dict[str, int] = {}  # source id -> asked every Nth tick (over its budget)
        self._budget_logged: set[str] = set()
        self._wipe_maps: dict[str, np.ndarray] = {}  # wipe image path -> its small grey picture
        self._release_later: list[tuple[str, str]] = []  # ("canvas" | "texture", key) to free on the next tick
        self._pruned_at = 0.0  # when idle output targets were last let go (compositor.prune)
        self._state_fades: dict[str, tuple] = {}
        # Build: canvas id -> the bloom being edited there when it isn't on air. Rendered to "next:<canvas>"
        # for the preview only. The UI replaces the whole dict, so the render thread sees a consistent one.
        self.staged: dict[str, str] = {}
        self.peek: frozenset[str] = frozenset()  # blooms pointed at in Live: their sources wake ahead, nothing drawn
        # Which previews the UI is showing ("horizontal", "next:horizontal", "part"); None = all of them.
        # A canvas nobody watches and nothing records is not drawn or copied back at all; a watched one
        # is copied back at most PREVIEW_FPS times a second. The UI replaces the whole set.
        self.previews: frozenset[str] | None = None
        # How big each preview is on screen, in physical pixels: previews are shrunk on the GPU to that,
        # so a 1080p canvas shown at 600x340 copies 0.8 MB per frame instead of 8 MB.
        self.preview_sizes: dict[str, tuple[int, int]] = {}
        self._preview_at: dict[str, float] = {}
        self.preview_fps = PREVIEW_FPS  # 15 with Preview while live: Light (Settings → General → This PC)
        self.readbacks = 0  # canvases copied from the GPU, for Stats
        self.wait_free = True  # copy outputs and small previews back a tick late instead of waiting on the GPU
        # Sources nothing on screen shows rest: "not_cameras" (cameras stay ready, they take a second
        # to open), "all", or "off". Set by the UI from Settings → General.
        self.sleep_policy = "not_cameras"
        self.live = False  # streaming: cameras stay open off screen (set by the window, app/main_window.py)
        self._last_shown: dict[str, float] = {}  # source id -> when something on screen last showed it
        self._camera_wait: dict[str, float] = {}  # camera id -> how long it stays open off screen (see above)
        self._slept_at: dict[str, float] = {}  # camera id -> when it last closed
        self._told_hidden: dict[str, bool] = {}  # source id -> what set_hidden() last said (see _show_hidden)
        self._visible_fresh = False
        self._visible: set[str] = set()  # sources showing in what's drawn, this tick (see _show_hidden)
        # Source record: canvas id -> sources left out of its clean copy ("clean:<canvas id>"), which
        # frame taps on that id get. Replaced whole by the UI (see set_clean).
        self.clean_hidden: dict[str, frozenset[str]] = {}
        # Extra pictures for taps on "view:<name>" (the virtual camera): name -> (canvas id, kind, ref),
        # kind "bloom" (a bloom drawn whether or not it's on air), "source" (one source, fitted) or
        # "scene" (ref is a Scene a plugin made, e.g. Instant replay's game with the camera in a corner).
        # Replaced whole by the UI (see set_view); drawn only while something taps them.
        self.views: dict[str, tuple[str, str, Any]] = {}
        self._view_at: dict[str, float] = {}  # view name -> when a screen-only view was last drawn
        # With the native engine: views it draws in a canvas slot of their own (name -> (canvas, scene)), views
        # of a bloom on air that ride on that canvas's outputs (canvas id -> names), and the views it served
        # this tick (Python's compositor leaves those alone).
        self._engine_views: dict = {}
        self._shot_views: frozenset[str] = frozenset()  # views only a screenshot reads (set_view shot=True)
        self._riders: dict[str, list[str]] = {}
        self._served_views: set[str] = set()
        self._ticks = 0  # ticks drawn (each counts itself up early on): what a hold or a screenshot waits for
        # Freezes. Snap's (see hold): source id -> (token, first tick, until perf_counter), replaced whole under
        # _holds_lock. Freeze filters (the Freeze plugin): source id -> ("filter", run) and its time.time() end
        # (0: none), from _sync_filters. Each tick: _frozen (source id -> which freeze, for the ones frozen now)
        # and _caught (source id -> the freeze whose picture its texture holds: uploads stop once caught).
        self._holds: dict[str, tuple[object, float, float]] = {}
        self._holds_lock = threading.Lock()
        self._freeze_filters: dict[str, tuple[tuple, float]] = {}
        # Items frozen on their own (a Freeze among an item's effects): item key -> (("filter", run), its
        # time.time() end, 0: none), from _sync_filters, and the ones frozen this tick (item key -> which freeze).
        self._item_freezes: dict[str, tuple[tuple, float]] = {}
        self._item_frozen: dict[str, object] = {}
        self._frozen: dict[str, object] = {}
        self._caught: dict[str, object] = {}
        self._missed: dict[str, np.ndarray] = {}  # a frozen source's newest picture, shown when it's let go
        # One source failing (a plugin's, a driver's) is that source's problem only: the rest of the canvas and
        # the sound carry on. source id -> when to ask it for a picture again, and the last problem said.
        self._failing: dict[str, float] = {}
        self._failed_said: dict[str, tuple[str, str]] = {}
        # SPRIGCAST_ENGINE=native: the native engine draws the canvases for the previews (render/native_bridge.py).
        self._native = None
        self._engine_edits = False  # this tick the native engine draws the sprig open in Build (PART_VIEW)
        self._python_drew = True  # last tick Python's compositor drew something (always, without the native engine)
        self._refresh_outputs = False  # refresh_outputs() asked: outputs are made again next tick
        self._refresh_waiting = False  # ...and is waiting for the native engine's stuck frame
        self._held_back: dict = {}  # source id -> (frame, channel order, layout): pictures Python's compositor skipped

    # Called from the UI thread ----------------------------------------------

    def start(self) -> None:
        self.mixer.start()
        self._thread.start()

    def send(self, command) -> None:
        self.commands.put(command)

    def add_frame_tap(self, canvas_id: str, callback: Callable[[Frame], None],
                      nv12_size: tuple[int, int] | None = None) -> None:
        """Hand every finished frame of a canvas to callback, on the render thread. It must not block.

        With nv12_size the frame arrives already scaled and converted to NV12 on the GPU (what encoders
        want); outputs sharing a size share one conversion. Without it, the frame is RGBA at canvas size.
        """
        with self._taps_lock:
            self._taps.setdefault(canvas_id, []).append((callback, nv12_size))

    def grab_source(self, source_id: str | None, callback: Callable[[np.ndarray | None], None],
                    raw: bool = False, limit: int = 1920, filters: list | None = None) -> None:
        """Hand the next picture of one source (with its filters, or without them when raw), or of what's on
        air when source_id is None, to callback as an H x W x 4 RGBA array (None if there's nothing to
        picture), its longest side at most `limit`. With `filters` (copies, not the garden's own) it is drawn with
        those instead of its own: the Filters window's unapplied changes. The callback runs on the render thread
        and must not block."""
        with self._taps_lock:
            self._grabs.append((source_id, callback, raw, limit, filters))

    def _serve_grabs(self, compositor, sizes) -> None:
        with self._taps_lock:
            grabs, self._grabs = self._grabs, []
        canvases = [c for c in self._collection.canvases if c.enabled] or self._collection.canvases
        for source_id, callback, raw, limit, filters in grabs:
            picture = None
            try:
                if source_id is None:
                    canvas = canvases[0] if canvases else None
                    if canvas is not None and canvas.id in compositor.targets:
                        data = compositor.read(canvas.id)
                        picture = np.frombuffer(data, dtype=np.uint8).reshape(canvas.height, canvas.width, 4).copy()
                else:
                    canvas_id = next((c.id for c in canvases if (source_id, c.id) in compositor.parts),
                                     canvases[0].id if canvases else "")
                    fx = None
                    if filters is not None and not raw:
                        from sprigcast.core.filters import video_effects

                        fx = video_effects(filters)
                    picture = compositor.render_source(source_id, sizes, canvas_id, limit, raw=raw, fx=fx)
            except Exception:
                log.exception("grabbing %s failed", source_id or "the stream")
            try:
                callback(picture)
            except Exception:
                log.exception("grab callback for %s failed", source_id or "the stream")

    def hold(self, source_ids, seconds: float, token: object | None = None) -> object:
        """Freeze these sources and sprig placements on the next frame drawn, for seconds (Snap: the screen holds
        still while the polaroid prints). Returns a token for let_go. With the token of a hold already on,
        only its time changes: it keeps the picture it caught. A frame drawn by a tick that started after this
        call already shows the frozen picture (screenshots wait for one, so they match it exactly)."""
        now = time.perf_counter()
        first = self._ticks + 1  # not the tick being drawn now (its sources are uploaded already): the next
        token = object() if token is None else token
        with self._holds_lock:
            holds = {k: v for k, v in self._holds.items() if v[2] > now}  # ended ones go
            for source_id in source_ids:
                old = holds.get(source_id)
                start = old[1] if old is not None and old[0] is token else first
                holds[source_id] = (token, start, now + max(0.0, float(seconds)))
            self._holds = holds
        return token

    def let_go(self, token: object) -> None:
        """End a hold (the sources move again from the next frame)."""
        with self._holds_lock:
            self._holds = {k: v for k, v in self._holds.items() if v[0] is not token}

    def remove_frame_tap(self, canvas_id: str, callback: Callable[[Frame], None]) -> None:
        with self._taps_lock:
            taps = self._taps.get(canvas_id, [])
            taps[:] = [t for t in taps if t[0] != callback]

    def refresh_outputs(self) -> None:
        """An output gets no frames (Recorder.check_flow): next tick the native engine's outputs and Python's
        output copies are let go and made again. Any thread."""
        self._refresh_outputs = True

    def set_clean(self, canvas_id: str, hidden) -> None:
        """Source record: draw a copy of this canvas without these sources (None: stop drawing it)."""
        clean = dict(self.clean_hidden)
        if hidden is None:
            clean.pop(canvas_id, None)
        else:
            clean[canvas_id] = frozenset(hidden)
        self.clean_hidden = clean

    def set_view(self, name: str, canvas_id: str | None = None, kind: str = "bloom", ref=None,
                 shot: bool = False) -> None:
        """Draw a bloom, a single source or a plugin's own Scene on a canvas's size for taps on "view:<name>"
        (None: stop). shot: a bloom pictured once (a screenshot off air): the native engine draws it in a slot of
        its own and native_shot("view:<name>") reads it there, with nothing tapping it. Python's compositor drew
        such a view before, every sprig of the bloom for the first time in one tick: a late frame of 35 to 57 ms
        for each screenshot (PC 2026-11-21)."""
        views = dict(self.views)
        if canvas_id is None:
            views.pop(name, None)
            self._shot_views = self._shot_views - {name}
        else:
            views[name] = (canvas_id, kind, ref)
            self._shot_views = (self._shot_views | {name}) if shot and kind == "bloom" else self._shot_views - {name}
        self.views = views

    def native_view_ready(self, name: str) -> bool | None:
        """Whether the native engine draws this shot view now (native_shot can read it). None: it never will (no
        native engine, or the view isn't one it takes), so a screenshot needn't wait for it."""
        from sprigcast.render.native_bridge import view_key

        native = self._native
        if native is None or native.failed or name not in self._shot_views:
            return None
        return name in self._engine_views and native.draws(view_key(name))

    def _view_scene(self, name: str, sizes: dict | None = None):
        """The scene a view draws, or None when what it names is gone."""
        from sprigcast.core.scene import Scene, SceneItem

        entry = self.views.get(name)  # set_view (any thread) may have just dropped it: PC run 2026-11-18 had
        if entry is None:  # a KeyError here fail a slideshow's start while Instant replay let go of its view
            return None, None
        canvas_id, kind, ref = entry
        collection = self._collection
        canvas = next((c for c in collection.canvases if c.id == canvas_id), None)
        if canvas is None:
            return None, None
        if kind == "bloom":
            return canvas, next((sc for sc in collection.scenes if sc.id == ref), None)
        if kind == "scene":
            return canvas, ref if isinstance(ref, Scene) else None
        if kind == "source" and any(src.id == ref for src in collection.sources):
            w, h = (sizes or {}).get(ref) or (canvas.width, canvas.height)
            k = min(canvas.width / max(1, w), canvas.height / max(1, h))
            item = SceneItem(source=ref, x=(canvas.width - w * k) / 2, y=(canvas.height - h * k) / 2, scale=k)
            return canvas, Scene(id=f"view:{name}", name=name, canvas=canvas_id, items=[item])
        return canvas, None

    def _native_views(self, taps: dict, sizes: dict) -> tuple[dict, dict]:
        """With the native engine: which views it makes. A view only recordings take (NV12: the virtual camera,
        Instant replay keeping a camera) is the engine's; one of the bloom on air rides on its canvas's outputs
        (same picture, mid transition too), any other is drawn in a canvas slot of its own. A view a screen
        watches (a projector, full-size RGBA) stays with Python. Returns (name -> (canvas, scene), canvas id ->
        names riding on it).

        The native heavy run (2026-11-17) still had Python draw and copy back the virtual camera's view and
        Instant replay's camera every frame, with NVENC waiting on it: 375-900 ms window freezes."""
        views: dict = {}
        riders: dict[str, list[str]] = {}
        active = self._collection.active_scenes
        enabled = {c.id for c in self._collection.canvases if c.enabled}
        current = self.views  # one look: set_view (any thread) swaps in a new dict
        for name in sorted(current):
            outputs = taps.get(f"view:{name}", ())
            if not outputs or any(size is None for _cb, size in outputs):
                continue
            canvas_id, kind, ref = current[name]
            if kind == "bloom" and canvas_id in enabled and ref == active.get(canvas_id):
                riders.setdefault(canvas_id, []).append(name)
                continue
            canvas, scene = self._view_scene(name, sizes)
            if canvas is not None and scene is not None:
                views[name] = (canvas, scene)
        for name in sorted(self._shot_views):  # a screenshot's bloom off air: the engine's, with nothing tapping it
            if name in views or name not in current:
                continue
            canvas_id, kind, ref = current[name]
            if kind != "bloom" or (canvas_id in enabled and ref == active.get(canvas_id)):
                continue  # on air: read from its canvas as it is
            canvas, scene = self._view_scene(name, sizes)
            if canvas is not None and scene is not None:
                views[name] = (canvas, scene)
        # Build's own pictures: the bloom being edited when it isn't the one on air, and the sprig that is open.
        # The engine draws them like any other view (Python's compositor drawing them meant it drew every sprig
        # in use a second time and had every page copied through memory: the imported garden's Build sat at 86%
        # of the graphics card, 2026-11-19).
        collection = self._collection
        for canvas in collection.canvases:
            scene_id = self.staged.get(canvas.id)
            if not canvas.enabled or scene_id is None or scene_id == active.get(canvas.id):
                continue
            scene = next((s for s in collection.scenes if s.id == scene_id), None)
            if scene is not None:
                views[STAGED_VIEW + canvas.id] = (canvas, scene)
        editing = self.editing_part
        if editing:
            part = next((p for p in collection.part_scenes() if p.id == editing), None)
            canvas = next((c for c in collection.canvases if part is not None and c.id == part.canvas), None)
            if canvas is not None:
                views[PART_VIEW] = (canvas, part)  # the sprig itself: every item, whatever its Live state
        return views, riders

    def _engine_shows(self, native, name: str) -> bool:
        """Whether the native engine draws this view of Build's (STAGED_VIEW + canvas id, PART_VIEW) this tick:
        it was handed over and got a canvas slot."""
        from sprigcast.render.native_bridge import view_key

        return native is not None and name in self._engine_views and native.draws(view_key(name))

    def _engine_staged(self, native, canvas, now: float) -> bool:
        """Build's edited bloom on this canvas from the native engine: its preview is the engine's picture. False
        when the engine doesn't draw it (Python's compositor does, _render_staged)."""
        from sprigcast.render.native_bridge import view_key

        name = STAGED_VIEW + canvas.id
        key, target = view_key(name), f"next:{canvas.id}"
        if not self._engine_shows(native, name):
            return False
        full = (canvas.width, canvas.height)
        wanted = self.previews is None or target in self.previews
        native.watch(key, (self._preview_size(target, full) or full) if wanted else None, self.preview_fps)
        if self._preview_due(target, now):
            got = native.take(key)
            if got is not None:
                data, w, h = got
                self.frames.setdefault(target, Mailbox()).put(Frame(target, w, h, data, now, canvas_size=full))
        return True

    def _engine_part(self, native, now: float) -> None:
        """The sprig open in Build from the native engine, at full size, as the Part editor takes it."""
        from sprigcast.render.native_bridge import view_key

        if not self._engine_shows(native, PART_VIEW):
            return
        canvas, part = self._engine_views[PART_VIEW]
        key = view_key(PART_VIEW)
        wanted = self.previews is None or "part" in self.previews
        native.watch(key, (canvas.width, canvas.height) if wanted else None, self.preview_fps)
        if self._preview_due("part", now):
            got = native.take(key)
            if got is not None:
                data, w, h = got
                self.part_frames.put(Frame(f"part:{part.id}", w, h, data, now))

    def _rider_outputs(self, canvas_id: str, taps: dict) -> list:
        """The outputs of the views of the bloom on air that ride on this canvas (_native_views)."""
        return [tap for name in self._riders.get(canvas_id, ()) for tap in taps.get(f"view:{name}", ())]

    def _render_views(self, compositor, sizes, now: float, taps: dict, drawn: dict | None = None,
                      native=None) -> None:
        """Draw each view something taps. drawn: canvas id -> its frames, for canvases drawn this tick."""
        from sprigcast.render.native_bridge import view_key

        for name in list(self.views):
            outputs = taps.get(f"view:{name}", ())
            if not outputs or name in self._served_views:
                continue
            if native is not None and name in self._engine_views:
                key, view_canvas = view_key(name), self._engine_views[name][0]
                keys = self._output_keys(native, outputs)
                if native.serves(key, keys):  # the engine drew it in a slot of its own: take its frames
                    native.set_outputs(key, keys, view_canvas.downscale)
                    self._native_outputs(native, SimpleNamespace(id=key), outputs)
                    continue
                native.set_outputs(key, ())
            if all(size is None for _cb, size in outputs):  # only screens watch it (a projector): ~30 fps is plenty
                if now - self._view_at.get(name, -1.0) < 1 / self.preview_fps - 0.002:
                    continue
                self._view_at[name] = now
            canvas, scene = self._view_scene(name, sizes)
            if canvas is None:
                continue
            frames = self._on_air_view(name, canvas, drawn)
            if frames is not None:
                target = canvas  # the bloom on air there, drawn already this tick: no second drawing of it
            else:
                target = canvas.model_copy(update={"id": f"view:{name}"})
                compositor.render(target, scene, sizes)
                frames = {}
            for callback, nv12_size in outputs:
                frame = self._output_frame(compositor, target, callback, nv12_size, now, frames)
                if frame is None:
                    continue
                try:
                    callback(frame)
                except Exception:
                    log.exception("frame tap for view %s failed", name)

    def _on_air_view(self, name: str, canvas, drawn: dict | None) -> dict | None:
        """A view of the bloom on air on a canvas drawn this tick: that canvas's frames this tick, to share.

        The heavy run (2026-11-17) had SprigCast Camera show the vertical bloom on air: the view drew the whole
        bloom a second time every frame, 5-20 ms of it. The canvas's own picture is the same (mid transition
        it shows the transition, as the stream does)."""
        if not drawn or canvas.id not in drawn:
            return None
        entry = self.views.get(name)
        if entry is None:
            return None
        _canvas_id, kind, ref = entry
        if kind != "bloom" or ref != self._collection.active_scenes.get(canvas.id):
            return None
        return drawn[canvas.id]

    def stop(self, timeout: float = 2.0, wait: bool = True) -> None:
        """Stop drawing and sound. wait=False (closing the window): the render thread finishes its tick and lets
        go of the graphics card on a thread of its own, so the window isn't held up (348-392 ms on the PC,
        2026-11-16); finish_stopping() then waits for it, after the window has gone."""
        self.commands.put(Shutdown())
        if wait and self._stopping is not None:  # already stopping on its own thread
            self._stopping.join(timeout + 5.0)
        elif wait:
            self._finish(timeout)
            return
        if self._stopping is None:
            self._stopping = threading.Thread(target=self._finish, args=(timeout,), name="sprigcast-stop")
            self._stopping.start()  # not a daemon: Python waits for it before it exits, so sound closes cleanly

    def finish_stopping(self, timeout: float = 5.0) -> None:
        """Wait for a stop(wait=False) to be done."""
        if self._stopping is not None:
            self._stopping.join(timeout)

    def _finish(self, timeout: float) -> None:
        self._thread.join(timeout)
        self.mixer.stop()
        self.monitor.stop()

    # Render thread -------------------------------------------------------------

    def _run(self) -> None:
        from sprigcast.render.compositor import (
            Compositor,  # GL context must live on this thread
            GraphicsError,
        )

        try:
            compositor = Compositor()
        except GraphicsError as exc:  # no graphics driver, or graphics older than OpenGL 3.3
            self.events.put(EngineError(str(exc), graphics=True))
            return
        except Exception as exc:  # remote desktop without GL, ...
            self.events.put(EngineError(f"Could not start the GPU compositor: {exc}", graphics=True))
            return
        self._warm_stingers(self._collection)

        from sprigcast import sprigcore
        from sprigcast.core import pacing

        if sprigcore.use_native():
            from sprigcast.render.native_bridge import NativeCanvases

            try:
                self._native = NativeCanvases(self._collection)
                from sprigcast.render import gpu_share

                gpu_share.set_native(self._native)  # encoders opened from now on take its frames on the card
            except Exception as exc:  # no native engine: Python's compositor draws everything
                log.warning("native engine: couldn't start (%s); the Python compositor draws everything", exc)
            sprigcore.drain_log()
        if pacing.raise_thread_priority():  # wake on time even while the window or a game keeps the CPU busy
            log.info("render thread priority raised")
        self._sync_sources(compositor)
        self._warm_up(compositor)
        fps = drawing_fps(self._collection.canvases)
        interval = 1.0 / fps
        next_tick = time.perf_counter()
        render_times: deque[float] = deque(maxlen=fps)
        log.info("drawing %d times a second", fps)
        ticks, dropped, stats_at = 0, 0, time.perf_counter()
        last_problem = None
        readbacks_at = 0

        late_logged, late_held = float("-inf"), 0
        failed_run, reset_checked, rebuild_after = 0, 0.0, 0.0
        restarted_at, restarts = float("-inf"), 0  # graphics starts close together (GRAPHICS_RESTART_CALM_S)
        gpu = self._gpu_wait(compositor)
        gpu_waited = 0.0
        skipped_gpu, gpu_logged = 0, float("-inf")
        skipped_run, behind_since = False, float("inf")
        try:
            while True:
                woke = time.perf_counter()
                # The card finishes the last tick before any OpenGL call of this one: if it fell behind, this
                # waits without Python's lock, instead of an OpenGL call waiting with it (render/gpu_wait.py).
                gpu_waited = gpu.wait() if gpu is not None else 0.0
                if gpu is not None and gpu.behind and woke - behind_since < GPU_SKIP_MAX_S:
                    # The card hasn't finished the tick before last: draw nothing new this tick (the window,
                    # sound and encoders carry on; outputs keep their last picture) and look again next tick.
                    if not skipped_run:
                        behind_since = woke
                    skipped_run = True
                    skipped_gpu += 1
                    dropped += 1
                    next_tick += interval
                    now = time.perf_counter()
                    if now < next_tick:
                        time.sleep(next_tick - now)
                    else:
                        next_tick = now
                    if skipped_gpu == 1 or now - gpu_logged >= LATE_LOG_EVERY_S:
                        gpu_logged = now
                        log.info("graphics card busy: waiting for it without drawing (%d ticks so far)",
                                 skipped_gpu)
                    continue
                if not (gpu is not None and gpu.behind):
                    skipped_run, behind_since = False, float("inf")
                if not self._drain_commands(compositor):
                    break
                started = time.perf_counter()
                pacing.busy_begin("render")  # a tick that runs long: the stall watch notes where it was
                try:
                    self._tick(compositor, started)
                    failed_run = 0
                    pacing.frame_ended()  # housekeeping on the window's thread waits for this (pacing.at_frame_end)
                except Exception as exc:  # one bad frame must not end rendering for the whole session
                    failed_run += 1
                    problem = f"{type(exc).__name__}: {exc}"
                    if problem != last_problem:
                        last_problem = problem
                        log.exception("a frame failed to render")
                        self.events.put(EngineError(f"A frame failed to render: {problem}"))
                if started - reset_checked >= 1.0 or failed_run >= GRAPHICS_LOST_TICKS:
                    reset_checked = started
                    if (failed_run >= GRAPHICS_LOST_TICKS or self._graphics_reset(compositor)) \
                            and started >= rebuild_after:
                        fresh = self._restart_graphics(compositor, gpu)
                        if fresh is None:
                            rebuild_after = started + GRAPHICS_RETRY_S
                        else:
                            compositor, gpu = fresh
                            failed_run, last_problem = 0, None
                            restarts = restarts + 1 if started - restarted_at < GRAPHICS_RESTART_CALM_S else 1
                            restarted_at = started
                            rebuild_after = started + min(GRAPHICS_RESTART_MAX_S, GRAPHICS_RETRY_S * 2 ** (restarts - 1))
                            continue
                if gpu is not None:
                    gpu.mark()
                held_at = pacing.busy_end("render")
                render_times.append(time.perf_counter() - started)
                ticks += 1

                next_tick += interval
                now = time.perf_counter()
                if now > next_tick:
                    # Running behind. A little late: render the next tick straight away and stay on the
                    # grid, so recordings get evenly spaced frames. A whole tick or more late: skip the
                    # missed ticks (no burst to catch up) and count them, still keeping the grid's phase.
                    missed = int((now - next_tick) / interval)
                    if missed:
                        dropped += missed
                        late_held += 1
                        # Say where the time went: the first late frame, then at most one every 10 s
                        # (878 lines on 2026-11-15), with how many were let go since.
                        if gpu_waited >= GPU_STALL_S:  # the card stopped for the whole app: see who used it
                            from sprigcast.app.gpu_usage import log_gpu_users_later

                            log_gpu_users_later(1000 * gpu_waited)
                        if now - late_logged >= LATE_LOG_EVERY_S:
                            late_logged = now
                            log.info("late frame (%d missed): woke %.1f ms late, %scommands %.1f ms, frame %.1f ms%s%s%s%s",
                                     missed, 1000 * max(0.0, woke - (next_tick - interval)),
                                     f"graphics card behind {1000 * gpu_waited:.1f} ms, " if gpu_waited >= 0.001
                                     else "",
                                     1000 * (started - woke - gpu_waited), 1000 * (render_times[-1]), self._where(),
                                     pacing.pause_since(next_tick - 2 * interval),
                                     f"; held up at {held_at}" if held_at else "",
                                     f"; {late_held - 1} more late since the last line" if late_held > 1 else "")
                            late_held = 0
                        next_tick += missed * interval
                else:
                    # Python 3.11+ sleeps with a high-resolution timer on Windows: no need to spin.
                    time.sleep(next_tick - now)

                if now - stats_at >= 1.0:
                    wanted = drawing_fps(self._collection.canvases)
                    if wanted != fps:  # a canvas's frame rate changed, or one was turned on or off
                        log.info("drawing %d times a second (was %d)", wanted, fps)
                        fps, interval = wanted, 1.0 / wanted
                        render_times = deque(render_times, maxlen=fps)
                        self._follow_rate(fps)
                    avg_ms = 1000 * sum(render_times) / max(1, len(render_times))
                    self.events.put(Stats(fps=ticks / (now - stats_at), render_ms=avg_ms, dropped_ticks=dropped,
                                           readbacks=(self.readbacks - readbacks_at) / (now - stats_at)))
                    readbacks_at = self.readbacks
                    self.events.put(
                        SourceInfo(
                            sizes={sid: src.size for sid, src in self._sources.items()},
                            statuses={sid: src.status for sid, src in self._sources.items()},
                        )
                    )
                    ticks, stats_at = 0, now
        finally:
            with self._warm_lock:
                self._ending = True
            for canvas_id in list(self._transitions):
                self._finish_transition(canvas_id, compositor)
            with self._warm_lock:
                warm, self._warm = list(self._warm.values()), {}
            for player in warm:
                player.close()
            retire.stop_all(list(self._sources.values()), CLOSE_ALL_S)  # side by side, not one after another
            if gpu is not None:
                gpu.release()
            compositor.release()
            if self._native is not None:
                from sprigcast.render import gpu_share

                gpu_share.set_native(None)
                self._native.close()
                self._native = None

    @staticmethod
    def _graphics_reset(compositor) -> bool:
        """Has the graphics driver reset under this context (OpenGL's own reset status, where it says)?"""
        gl = getattr(compositor, "gl", None)
        status = getattr(gl, "reset_status", None)
        return bool(status and status())

    def _restart_graphics(self, compositor, gpu):
        """The graphics driver reset (a driver update, a game crashing the card, Windows' hang recovery): every
        texture and target on the card is gone. Like OBS rebuilding its device, start the graphics again and
        have every source send its picture afresh, instead of a black canvas until SprigCast restarts.
        Returns (compositor, gpu wait) or None when the card can't start yet (tried again shortly)."""
        from sprigcast.render.compositor import Compositor

        log.warning("graphics driver reset: starting the graphics again")
        for step in (lambda: [self._finish_transition(c, compositor) for c in list(self._transitions)],
                     lambda: gpu.release() if gpu is not None else None,
                     compositor.release,
                     lambda: compositor.ctx.release()):
            try:
                step()
            except Exception:  # noqa: BLE001 (the old context is gone: letting go of it may fail too)
                pass
        try:
            fresh = Compositor()
        except Exception as exc:  # noqa: BLE001
            log.warning("graphics didn't start again yet: %s", exc)
            return None
        for source in list(self._sources.values()):  # pictures that only come once (images, text) come again
            self._retire(source)
        self._sources.clear()
        for held in (self._last_shown, self._failing, self._failed_said, self._camera_wait, self._slept_at,
                     self._caught, self._missed, self._held_back):
            held.clear()  # the held-back pictures too: one that broke the copy must not break the new graphics
        self._part_textures, self._parts_used = {}, set()
        try:
            self._sync_sources(fresh)
            self._warm_up(fresh)
        except Exception:  # noqa: BLE001 (the next ticks try again, and restart again if they keep failing)
            log.exception("graphics: setting the picture up again failed")
        self.events.put(EngineError("The graphics card restarted. SprigCast set the picture up again."))
        return fresh, self._gpu_wait(fresh)

    @staticmethod
    def _gpu_wait(compositor):
        """Fences for this thread's OpenGL context, or None (a test stand-in, or no sync objects)."""
        import moderngl

        if not isinstance(getattr(compositor, "ctx", None), moderngl.Context):
            return None
        from sprigcast.render.gpu_wait import GpuWait

        gpu = GpuWait()
        return gpu if gpu.ok else None

    def _drain_commands(self, compositor) -> bool:
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return True
            if isinstance(command, Shutdown):
                return False
            if isinstance(command, UpdateSourceSettings):
                self._update_settings(command.settings)
                continue
            if isinstance(command, SwitchBlooms):
                self._switch_blooms(command.active_scenes)
                continue
            if isinstance(command, ReplaceCollection):
                # A drag sends a new collection on every mouse move: only the newest one matters, so
                # skip straight to it instead of rebuilding sources for each (that backlog was the lag).
                settings_after: list = []  # settings sent after the collection taken: applied on top of it
                while True:
                    try:
                        newer = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(newer, Shutdown):
                        return False
                    if isinstance(newer, ReplaceCollection):
                        command, settings_after = newer, []
                    elif isinstance(newer, UpdateSourceSettings):
                        settings_after.append(newer.settings)
                    elif isinstance(newer, SwitchBlooms):  # a switch after it: the newest collection, switched
                        command = ReplaceCollection(command.collection.model_copy(
                            update={"active_scenes": dict(newer.active_scenes)}))
                went_on_air = self._went_on_air(self._collection, command.collection)
                self._start_transitions(self._collection, command.collection)
                self._start_state_fades(self._collection, command.collection)
                self._collection = command.collection
                self._warm_stingers(self._collection)
                for canvas in self._collection.canvases:
                    self.frames.setdefault(canvas.id, Mailbox())
                self._sync_sources(compositor)
                self._reload_on_air(went_on_air)
                for settings in settings_after:
                    self._update_settings(settings)

    def _switch_blooms(self, active_scenes: dict[str, str]) -> None:
        """SwitchBlooms: what a collection replace does for a switch, less comparing every source (none changed)."""
        old = self._collection
        new = old.model_copy(update={"active_scenes": dict(active_scenes)})
        went_on_air = self._went_on_air(old, new)
        self._start_transitions(old, new)
        self._start_state_fades(old, new)
        self._collection = new
        self._warm_stingers(new)
        self.mixer.bloom_out = new.sounds_off_here()  # sound per bloom, as blooms switch
        self._reload_on_air(went_on_air)

    def _update_settings(self, settings: dict[str, dict]) -> None:
        """UpdateSourceSettings: these sources' settings in the engine's copy of the collection, and to the sources."""
        configs = {s.id: s for s in self._collection.sources}
        for source_id, new in settings.items():
            config = configs.get(source_id)
            if config is None:
                continue
            config.settings = dict(new)
            source = self._sources.get(source_id)
            if source is not None and source.settings != config.settings:
                self._safely(source_id, source, "update", source.update, config.settings)
        self._plan = None  # a placement's hidden items are settings too (see _sprig_plan)

    @staticmethod
    def _on_air_sources(collection: SceneCollection, canvas_id: str | None = None) -> set[str]:
        """Sources in the blooms on air now (in one canvas, or all), and in the sprigs inside them."""
        stack = [s for s in (collection.active_scene(c.id) for c in collection.canvases
                             if c.enabled and (canvas_id is None or c.id == canvas_id)) if s]
        seen, found = {s.id for s in stack}, set()
        while stack:
            scene = stack.pop()
            found.update(item.source for item in scene.items)
            for inner in collection.inner_shapes(scene):
                if inner.id not in seen:
                    seen.add(inner.id)
                    stack.append(inner)
        return found

    def _went_on_air(self, old: SceneCollection, new: SceneCollection) -> set[str]:
        """Sources that a bloom switch just put on air (not ones that were on air already)."""
        if old.active_scenes == new.active_scenes:
            return set()
        return self._on_air_sources(new) - self._on_air_sources(old)

    def _reload_on_air(self, source_ids: set[str]) -> None:
        """Web pages with "Reload when on air" load again as their bloom goes on air."""
        for source_id in source_ids:
            source = self._sources.get(source_id)
            if source is not None and source.settings.get("reload_on_air") and hasattr(source, "reload"):
                source.reload()

    def _start_transitions(self, old: SceneCollection, new: SceneCollection) -> None:
        """When a canvas's active scene changes, run the transition from the scene it showed until now."""
        started: dict[str, StingerPlayer] = {}  # canvases switching together share one player per clip
        for canvas in new.canvases:
            before, after = old.active_scenes.get(canvas.id), new.active_scenes.get(canvas.id)
            if before == after or before is None:
                continue
            transition = new.transition_for(before, after)
            self._finish_transition(canvas.id)
            outgoing = next((s for s in old.scenes if s.id == before), None)
            incoming = next((s for s in new.scenes if s.id == after), None)
            log.info("on air on %s: %r (was %r), %s", canvas.id, getattr(incoming, "name", after),
                     getattr(outgoing, "name", before), transition.kind)
            self._wait_for_waking(canvas.id, outgoing, new, after)
            if outgoing is None or transition.kind == "cut":
                continue
            now = time.perf_counter()
            if transition.kind == "stinger":
                path = stinger_path(transition, canvas)
                if path:
                    player = started.get(path)
                    if player is None:
                        player = started[path] = self._new_stinger(path, transition, new)
                    self._transitions[canvas.id] = Running(outgoing, None, 0.0, transition, stinger=player)
                    continue
                transition = transition.model_copy(update={"kind": "fade"})  # no clip picked yet
            if transition.duration_ms <= 0:
                continue
            wipe_map = transition.wipe_image if transition.kind == "wipe" and transition.wipe_image else ""
            if wipe_map and self._wipe_map(wipe_map) is None:
                wipe_map = ""
            self._transitions[canvas.id] = Running(outgoing, now, transition.duration_ms / 1000, transition,
                                                   wipe_map=wipe_map)

    def _wait_for_waking(self, canvas_id: str, outgoing, collection: SceneCollection, scene_id: str) -> None:
        """Switching to a bloom with a closed camera (or a slow_wake source, like a PNGtuber loading its pictures
        again, a closed video, or a web page loading again: its first paint, not the clear placeholder, counts
        through `first_picture`): keep showing the old bloom until it has a picture
        (at most WAKE_WAIT_S), so the cut or transition lands on the camera, not on an empty spot."""
        self._waking.pop(canvas_id, None)
        scene = next((s for s in collection.scenes if s.id == scene_id), None)
        if scene is None or outgoing is None:
            return
        stack, seen, ids = [scene], {scene.id}, set()
        while stack:
            shown = stack.pop()
            ids.update(item.source for item in shown.items if item.visible)
            for inner in collection.inner_shapes(shown):
                if inner.id not in seen:
                    seen.add(inner.id)
                    stack.append(inner)
        asleep = {sid for sid in ids if (src := self._sources.get(sid)) is not None and src.asleep
                  and (src.kind == "camera" or getattr(src, "slow_wake", False) or sid in self._had_picture)}
        if asleep:
            self._waking[canvas_id] = (outgoing, time.perf_counter() + WAKE_WAIT_S, asleep)

    def _switch_waits(self, compositor, canvas_id: str, now: float) -> bool:
        """True while a switch on this canvas still waits for a picture (see _still_waking)."""
        waiting = self._waking.get(canvas_id)
        if waiting is None:
            return False
        _outgoing, give_up, ids = waiting
        return now < give_up and not all(self._has_picture(compositor, sid) for sid in ids)

    def _has_picture(self, compositor, source_id: str) -> bool:
        """A source a switch waits for has its picture: in Python's compositor, or (a capture the engine does
        itself) in the engine, which then draws the switch without Python ever getting a copy."""
        source = self._sources.get(source_id)
        if source_id in compositor.textures and getattr(source, "first_picture", True):
            return True
        return self._native is not None and not self._native.failed and (
            hasattr(type(source), "engine_shows") and bool(source.engine_shows))

    def _still_waking(self, compositor, canvas_id: str, now: float):
        """The scene to keep showing while a switch waits for a camera to open, or None to go ahead."""
        waiting = self._waking.get(canvas_id)
        if waiting is None:
            return None
        if canvas_id in self._sprigs_held or self._switch_waits(compositor, canvas_id, now):
            return waiting[0]  # (held when its sprigs were left out this tick: shown the next, sprigs drawn)
        del self._waking[canvas_id]
        running = self._transitions.get(canvas_id)
        if running is not None and running.began is not None:
            running.began = now  # the transition starts now that the camera is there
        return None

    @staticmethod
    def _stinger_key(path: str, transition, collection: SceneCollection) -> tuple:
        """(path, limit, gain): what a stinger player is opened with."""
        width = max(c.width for c in collection.canvases)
        height = max(c.height for c in collection.canvases)
        matte = transition.stinger.matte
        limit = (width * (2 if matte == "right" else 1), height * (2 if matte == "below" else 1))
        sting = transition.stinger
        gain = 0.0 if sting.muted else 10 ** (sting.volume_db / 20)
        return path, limit, gain

    def _new_stinger(self, path: str, transition, collection: SceneCollection) -> StingerPlayer:
        key = self._stinger_key(path, transition, collection)
        with self._warm_lock:
            player = self._warm.pop(key, None)
        if player is not None and player.error is None:
            return player  # opened ahead: its first pictures are waiting
        if player is not None:
            player.close()
        player = open_stinger(*key)
        player.start()
        return player

    def _warm_stingers(self, collection: SceneCollection) -> None:
        """Open the stinger clips this collection can play ahead of time, paused with their first pictures decoded.

        Opened at the switch, the clip's file, decoder and first pictures all came at once with the bloom's cameras
        waking, on a PC already busy (2026-11-21: "the stinger is a bit spotty, a slight slow down"). Opened ahead,
        the switch starts from pictures already waiting; a clip used is opened again for the next switch."""
        if self._ending or not collection.canvases:
            return
        wanted: list[tuple] = []
        for transition in [collection.transition, *(rule.transition for rule in collection.transition_rules)]:
            if transition.kind != "stinger":
                continue
            for canvas in collection.canvases:
                path = stinger_path(transition, canvas) if getattr(canvas, "enabled", True) else ""
                key = self._stinger_key(path, transition, collection) if path else None
                if key is not None and key not in wanted:
                    wanted.append(key)
        wanted = wanted[:WARM_STINGERS]
        with self._warm_lock:
            self._warm_wanted = wanted
            unwanted = [self._warm.pop(k) for k in [k for k in self._warm if k not in wanted]]
            missing = [k for k in wanted if k not in self._warm and k not in self._warming]
            for key in missing:
                # Opened on a thread of its own: opened here, right after a switch took the warm one, the clip's file
                # and decoder held the switch frame (PC live 2026-11-21: "commands 31.4 ms" at a switch).
                thread = self._warming[key] = threading.Thread(target=self._open_warm, args=(key,),
                                                               name="sprigcast-stinger-ahead", daemon=True)
                thread.start()
        for player in unwanted:
            player.close()

    def _open_warm(self, key: tuple) -> None:
        player = None
        try:
            player = open_stinger(*key)
            player.start()
        except Exception as exc:  # noqa: BLE001 (opened at the switch instead)
            log.warning("stinger %s couldn't be opened ahead: %s", key[0], exc)
        with self._warm_lock:
            self._warming.pop(key, None)
            keep = player is not None and not self._ending and key in self._warm_wanted and key not in self._warm
            if keep:
                self._warm[key] = player
        if player is not None and not keep:
            player.close()

    def _wait_warm(self, timeout: float = 5.0) -> None:
        """Until the stingers being opened ahead are open (tests)."""
        with self._warm_lock:
            threads = list(self._warming.values())
        for thread in threads:
            thread.join(timeout)

    def _wipe_map(self, path: str) -> np.ndarray | None:
        """A wipe image as a small grey RGB picture (it is a soft gradient: 960 px wide is plenty)."""
        if path not in self._wipe_maps:
            try:
                from PIL import Image

                with Image.open(path) as image:
                    image = image.convert("L")
                    image.thumbnail((960, 960))
                    self._wipe_maps[path] = np.asarray(image.convert("RGB"))
            except Exception as exc:
                log.warning("wipe image %s could not be read: %s", path, exc)
                return None
        return self._wipe_maps[path]

    def _finish_transition(self, canvas_id: str, compositor=None) -> None:
        running = self._transitions.pop(canvas_id, None)
        if running is None:
            return
        free = [("canvas", canvas_id)]
        player = running.stinger
        if player is not None and not any(r.stinger is player for r in self._transitions.values()):
            self.mixer.remove_extra(player)
            player.close()
            free.append(("texture", player.id))
            self._warm_stingers(self._collection)  # ready for the next switch
        if running.wipe_map and not any(r.wipe_map == running.wipe_map for r in self._transitions.values()):
            free.append(("texture", f"wipe:{running.wipe_map}"))
        if compositor is None:  # called from a command: free them on the next tick, unless reused by then
            self._release_later += free
            return
        for what, key in free:
            if what == "canvas":
                compositor.end_transition(key)
            else:
                compositor.forget(key)

    def _release_unused(self, compositor) -> None:
        later, self._release_later = self._release_later, []
        busy_textures = {r.stinger.id for r in self._transitions.values() if r.stinger is not None}
        busy_textures |= {f"wipe:{r.wipe_map}" for r in self._transitions.values() if r.wipe_map}
        for what, key in later:
            if what == "canvas" and key not in self._transitions:
                compositor.end_transition(key)
            elif what == "texture" and key not in busy_textures:
                compositor.forget(key)

    def _transition_step(self, compositor, canvas_id: str, now: float) -> dict | None:
        """Advance a canvas's transition. Returns the extra render arguments, or None when there is none."""
        running = self._transitions.get(canvas_id)
        if running is None:
            return None
        player = running.stinger
        if player is not None and running.began is None:
            if player.ready.is_set() and player.error is None:
                for other in self._transitions.values():  # canvases sharing the clip start together
                    if other.stinger is player:
                        other.began, other.duration = now, player.duration
                self.mixer.add_extra(player)
            elif player.ready.is_set() or now - running.queued > STINGER_WAIT_S:
                # The clip would not open: fall back to a quick fade so the switch still happens.
                fade = running.settings.model_copy(update={"kind": "fade"})
                running.settings, running.stinger, running.began = fade, None, now
                running.duration = max(0.05, fade.duration_ms / 1000)
                if not any(r.stinger is player for r in self._transitions.values()):
                    player.close()
            else:
                return {"previous": running.outgoing, "progress": 0.0, "transition": running.settings}
        elapsed = now - running.began
        progress = elapsed / running.duration
        if progress >= 1.0:
            self._finish_transition(canvas_id, compositor)
            return None
        extra = {"previous": running.outgoing, "progress": max(0.0, progress), "transition": running.settings}
        if running.settings.kind == "move":
            canvas = next((c for c in self._collection.canvases if c.id == canvas_id), None)
            if canvas is not None:
                extra["move"] = partial(self._move_frame, running, canvas, max(0.0, progress))
        if running.stinger is not None:
            from sprigcast.render.native_bridge import native_transition

            native = self._native
            if native is not None and not native.failed and native_transition(running):
                running.stinger.play()  # the engine draws the clip from its player: no picture comes through here
                extra["engine_stinger"] = True
            else:
                frame = running.stinger.frame_at(elapsed)
                if frame is not None:
                    compositor.upload(running.stinger.id, frame, running.stinger.channel_order)
            extra["overlay"] = running.stinger.id
            extra["cut"] = elapsed >= running.settings.stinger.cut_ms / 1000
        if running.wipe_map:
            key = f"wipe:{running.wipe_map}"
            if key not in compositor.textures:
                picture = self._wipe_map(running.wipe_map)
                if picture is not None:
                    compositor.upload(key, picture, "RGB")
            extra["wipe_map"] = key
        return extra

    def _start_state_fades(self, old: SceneCollection, new: SceneCollection) -> None:
        """When a sprig switches state, fade the items it shows or hides instead of popping them."""
        before = {p.id: p for p in old.parts()}
        for part in new.parts():
            was = before.get(part.id)
            if was is None:
                continue
            old_hidden, new_hidden = old.state_hidden(was), new.state_hidden(part)
            if old_hidden != new_hidden:
                self._state_fades[part.id] = (time.perf_counter(), old_hidden, new_hidden)

    def _state_view(self, collection: SceneCollection, layout):
        """A sprig layout as its Live state shows it right now: (layout to draw, per-item fade opacities)."""
        main = collection.main_part(layout)
        fade = self._state_fades.get(main.id)
        if fade is not None:
            start, old_hidden, new_hidden = fade
            t = (time.perf_counter() - start) / STATE_FADE_S
            if t < 1.0:
                t = max(0.0, t)
                fades = {sid: t for sid in old_hidden - new_hidden}  # appearing
                fades.update({sid: 1.0 - t for sid in new_hidden - old_hidden})  # disappearing
                return collection.hide_items(layout, old_hidden & new_hidden), fades
            del self._state_fades[main.id]
        return collection.hide_items(layout, collection.state_hidden(layout)), None

    def _sync_sources(self, compositor) -> None:
        """Create, update and remove source instances so they match the collection."""
        wanted = {s.id: s for s in self._collection.sources_in_use()}
        for source_id in list(self._sources):
            gone = source_id not in wanted or self._sources[source_id].kind != wanted[source_id].kind
            if gone or source_class(self._sources[source_id].kind) is None:  # its plugin was removed
                if not gone:
                    self._stopped_unknown.add(source_id)
                self._retire(self._sources.pop(source_id))
                self._last_shown.pop(source_id, None)
                self._failing.pop(source_id, None)
                self._failed_said.pop(source_id, None)
                self._camera_wait.pop(source_id, None)
                self._slept_at.pop(source_id, None)
                compositor.forget(source_id)
        for source_id, config in wanted.items():
            existing = self._sources.get(source_id)
            if existing is not None and self._needs_new(existing, config.settings):
                self._retire(self._sources.pop(source_id))  # a new one opens while the old one closes
                existing = None
            if existing is None and source_class(config.kind) is None and source_id in self._stopped_unknown:
                continue  # its plugin was removed: said so once already
            if existing is None:
                try:
                    source = create_source(config.kind, config.id, config.settings)
                    if hasattr(source, "canvas_fps"):  # captures ask Windows for no more pictures than this
                        source.canvas_fps = drawing_fps(self._collection.canvases)
                    if getattr(source, "wants_engine", False):  # plugins that tap frames or sound (Instant replay)
                        source.engine = self
                    if self._may_sleep(source) and source_id not in self._shown_sources():
                        source.asleep = True  # not on screen: don't even start it until it is
                        source.sleep()
                    else:
                        source.start()
                except Exception as exc:
                    log.warning("source %s (%s) failed to start: %s", config.name, config.kind, exc)
                    self.events.put(EngineError(f"Source {config.name!r} failed to start: {exc}"))
                    continue
                self._sources[source_id] = source
            elif existing.settings != config.settings:
                self._safely(source_id, existing, "update", existing.update, config.settings)
        self._sync_filters(compositor, wanted)
        self._sync_detail()
        compositor.downscale = {c.id: c.downscale for c in self._collection.canvases}
        self._sync_parts(compositor)
        self.mixer.set_sources(list(self._sources.values()))
        names = {c.id: c.name for c in self._collection.sources}
        listen.set_sounds([(sid, names.get(sid, sid)) for sid, s in self._sources.items()
                           if hasattr(s, "read_block") and sid in names])  # what plugins can listen to
        self.mixer.bloom_out = self._collection.sounds_off_here()  # sound per bloom, as blooms switch
        followed = {  # sounds a Ducking filter listens to
            str(f.settings.get("sound_id")) for s in self._collection.sources for f in s.filters
            if f.kind == "ducking" and f.enabled and f.settings.get("sound_id")}
        for source_id, source in self._sources.items():  # and sources that move with a sound (a PNGtuber)
            follow = getattr(source, "sound_to_follow", None)
            if follow is None:
                continue
            try:
                sound = follow(self._collection)
            except Exception as exc:
                log.warning("source %s couldn't pick a sound to follow: %s", source_id, exc)
                continue
            if sound:
                followed.add(str(sound))
        for gone in set(SOUND_LEVELS) - followed:  # a sound no longer followed mustn't keep its last level
            SOUND_LEVELS.pop(gone, None)
        self.mixer.followed = frozenset(followed)
        self._sync_audio_options()

    def _sync_detail(self) -> None:
        """Tell each sharp source (text, clock cards) the biggest size it's shown at, so it draws that many
        pixels: crisp when it's made big, light when it's small. Only a step change redraws it."""
        sharp = {sid: s for sid, s in self._sources.items() if getattr(s, "sharp", False)}
        shrinks = {sid: s for sid, s in self._sources.items() if getattr(s, "shrinks", False)}
        if not sharp and not shrinks:
            return
        try:
            scales = shown_detail(self._collection, {sid: s.size for sid, s in {**sharp, **shrinks}.items()})
        except Exception as exc:  # never stop the render loop for this: they keep the detail they have
            log.warning("couldn't work out how big sharp sources are shown: %s", exc)
            return
        for source_id, source in shrinks.items():  # videos shown small are decoded small (sources/media.py)
            try:
                step = shrink_step(scales.get(source_id, 1.0))
                if step != getattr(source, "shrink", 1.0):
                    source.set_shrink(step)
            except Exception as exc:  # a plugin's source
                log.warning("source %s set_shrink failed: %s", source_id, exc)
        for source_id, source in sharp.items():
            try:
                step = detail_step(scales.get(source_id, 1.0), source.size)
                if step != source.detail:
                    source.set_detail(step)
            except Exception as exc:  # a plugin's source
                log.warning("source %s set_detail failed: %s", source_id, exc)

    def set_sound_out(self, device: str | None) -> None:
        """Play the stream mix on a playback device too (None: stop), e.g. a virtual cable that TikTok LIVE
        Studio uses as its mic, next to SprigCast Camera. Opened only while asked for."""
        current = getattr(self, "_sound_out", None)
        if current is not None and current.device == device:
            return
        if current is not None:
            self.mixer.remove_tap(current.write)
            current.stop()
            self._sound_out = None
        if device:
            out = make_monitor(self.mixer, device)
            out.start()
            self.mixer.add_tap(out.write, ("stream",))
            self._sound_out = out

    @property
    def sound_out_status(self) -> str:
        out = getattr(self, "_sound_out", None)
        return out.status if out is not None else "off"

    def push_key(self, down: bool) -> None:
        """The push-to-talk key went down or up (any thread): the mixer opens or closes the mic per block."""
        self.mixer.push_key(down)

    def _sync_audio_options(self) -> None:
        """Ducking settings to the mixer; start monitoring when a source is monitored, stop when none is."""
        audio = self._collection.audio
        self.mixer.set_ducking(Ducking(
            enabled=audio.ducking, amount_db=audio.duck_amount_db, threshold_db=audio.duck_threshold_db,
            attack_ms=audio.duck_attack_ms, release_ms=audio.duck_release_ms, hold_ms=audio.duck_hold_ms,
        ))
        self.mixer.set_push(audio.push_mic(self._collection), audio.push_mode, audio.push_release_ms)
        if audio.windows_ducking_off != getattr(self, "_windows_ducking_off", None):
            from sprigcast import sprigcore

            self._windows_ducking_off = audio.windows_ducking_off
            try:
                sprigcore.set_windows_ducking(audio.windows_ducking_off)
            except Exception as exc:  # noqa: BLE001 (an older engine: Windows keeps its own setting)
                log.info("couldn't set Windows ducking: %s", exc)
        device = audio.monitor_device or None
        if device != getattr(self, "_monitor_device", None):  # "Listen through" changed: a monitor on that device
            self._monitor_device = device
            was = self._monitoring
            if was:
                self.mixer.remove_monitor_tap(self.monitor.write)
                self.monitor.stop()
                self._monitoring = False
            self.monitor = make_monitor(self.mixer, device)
        monitoring = any(
            s.settings.get("monitor") and not s.settings.get("muted") for s in self._collection.sources_in_use()
            if s.kind in ("audio_input", "audio_output", "app_audio", "media")
        )
        if monitoring and not self._monitoring:
            self._monitoring = True
            self.monitor.start()
            self.mixer.add_monitor_tap(self.monitor.write)
        elif not monitoring and self._monitoring:
            self._monitoring = False
            self.mixer.remove_monitor_tap(self.monitor.write)
            self.monitor.stop()

    def _sync_filters(self, compositor, wanted: dict) -> None:
        """Hand each source's video filters to the compositor and keep its audio filter chain current."""
        from sprigcast.core.filters import freeze_of

        effects = {}
        freezes = {}
        for source_id, source in self._sources.items():
            filters = wanted[source_id].filters
            fx = video_effects(filters)
            if fx.active:
                effects[source_id] = fx
            freeze = freeze_of(filters)
            if freeze is not None:
                freezes[source_id] = (("filter", freeze[0]), freeze[1])
            chain = getattr(source, "audio_chain", None)
            if chain is not None and (chain.matches(filters) or chain.update(filters)):
                continue
            new_chain = AudioChain(filters)
            source.audio_chain = new_chain if new_chain.processors else None
        self._filtered = frozenset(effects)  # sources with video filters (a Move draws such a sprig whole)
        item_freezes = {}
        for scene in self._collection.scenes:  # effects on one item only, under its item key
            for item in scene.items:
                if item.effects:
                    own = video_effects(item.effects)
                    if own.active:
                        base = effects.get(item.source) or video_effects(())
                        effects[item_key(scene.id, item.source)] = with_item(base, own)
                    freeze = freeze_of(item.effects)
                    if freeze is not None:
                        item_freezes[item_key(scene.id, item.source)] = (("filter", freeze[0]), freeze[1])
        compositor.effects = effects
        self._freeze_filters = freezes
        self._item_freezes = item_freezes

    def _sync_parts(self, compositor) -> None:
        """Tell each nested-part source the size of the layout it shows, and drop textures of layouts that are gone.

        A placement shows the part's layout for the canvas of the scene it sits in.
        """
        collection = self._collection
        placed_on = {}  # source id -> canvas of the first scene that places it
        for scene in collection.scenes:
            for item in scene.items:
                placed_on.setdefault(item.source, scene.canvas)
        for source_id, source in self._sources.items():
            if source.kind != PART_KIND:
                continue
            part = collection.part_of(next(s for s in collection.sources if s.id == source_id))
            canvas = None
            if part is not None:
                shape = collection.shape_for(part, placed_on.get(source_id, part.canvas))
                canvas = next((c for c in collection.canvases if c.id == shape.canvas), None)
            source.part_size = (canvas.width, canvas.height) if canvas else (0, 0)
        alive = {p.id for p in collection.part_scenes()}
        for source in collection.sources:  # placements with their own tweaks render a copy of their own
            part = collection.part_of(source)
            if part is not None and collection.hidden_here(source):
                alive |= {f"{shape.id}~{source.id}" for shape in collection.shapes(part)}
        for key in [k for k in compositor.targets if k.startswith("part:")]:
            if key[5:] not in alive:
                compositor.forget_part(key[5:])

    def _may_sleep(self, source) -> bool:
        if not source.sleeps or self.sleep_policy == "off":
            return False
        if source.kind == "camera" and self.live:
            # Live, a camera stays open once opened: reopened at each switch it held the switch up to 2 s, came up
            # in another format and lined up with the stinger (PC, 2026-11-21).
            return False
        if self.sleep_policy == "all" or source.kind != "camera":
            return True
        # A camera pauses when its own "Pause when not on screen" is on (the default; it waits CAMERA_SLEEP_AFTER).
        return bool((getattr(source, "settings", None) or {}).get("pause_off_screen", True))

    def _shown_sources(self) -> set[str]:
        """Sources placed in anything drawn right now: on air, fading out, staged, the sprig being edited,
        and every sprig inside those. Hidden items count too, so showing one is instant."""
        collection = self._collection
        stack = self._drawn_scenes(collection)
        if self.editing_part:
            stack += [p for p in collection.part_scenes() if p.id == self.editing_part]
        peek = self.peek
        if peek:  # pointed at in Live: woken (a page loads) before the click, not drawn
            stack += [s for s in collection.scenes if s.id in peek and s not in stack]
        seen = {scene.id for scene in stack}
        shown: set[str] = set()
        while stack:
            scene = stack.pop()
            shown.update(item.source for item in scene.items)
            for inner in collection.inner_shapes(scene):
                if inner.id not in seen:
                    seen.add(inner.id)
                    stack.append(inner)
        return shown

    def _visible_sources(self) -> set[str]:
        """Sources actually showing in what's drawn: like _shown_sources, but items with their eye off (or
        hidden by a sprig's Live state or by one placement of it) don't count, nor anything inside them."""
        collection = self._collection
        configs = {s.id: s for s in collection.sources}
        visible: set[str] = set()
        drawn = self._drawn_scenes(collection)
        if self.editing_part:
            drawn += [p for p in collection.part_scenes() if p.id == self.editing_part]
        for scene in drawn:
            _walk_visible(collection, configs, visible, scene, set(), (scene.id,))
        return visible

    def _on_air_visible(self) -> set[str]:
        """Like _visible_sources, for what's on air only (and a bloom fading out): no staged bloom, no sprig being
        edited."""
        collection = self._collection
        configs = {s.id: s for s in collection.sources}
        visible: set[str] = set()
        for canvas in collection.canvases:
            if not canvas.enabled:
                continue
            scenes = [next((s for s in collection.scenes if s.id == collection.active_scenes.get(canvas.id)), None)]
            running = self._transitions.get(canvas.id)
            if running is not None:
                scenes.append(running.outgoing)
            for scene in scenes:
                if scene is not None:
                    _walk_visible(collection, configs, visible, scene, set(), (scene.id,))
        return visible

    def _show_hidden(self, visible: set[str] | None = None) -> None:
        """Tell each awake source when its eye goes off or on (a video pauses, then starts over or carries on)."""
        if visible is None:  # worked out by _rest_sources this tick, else now
            visible = self._visible if self._visible_fresh else self._visible_sources()
        self._visible_fresh = False
        told = self._told_hidden
        on_air = placed = None  # worked out only if a sound file is about (below)
        for source_id, source in self._sources.items():
            if source.asleep:
                told.pop(source_id, None)  # said again once it wakes
                continue
            hidden = source_id not in visible
            if getattr(source, "follows_air", False):
                # A sound file placed in a bloom plays while that bloom is on air, like a video (an OBS import's
                # Starting Soon music never played, PC 2026-11-20); a staged bloom in Build stays quiet. One only
                # in the Sound list isn't told at all: it plays when you press play.
                if placed is None:
                    on_air, placed = self._on_air_visible(), {i.source for sc in self._collection.scenes
                                                              for i in sc.items}
                if source_id not in placed:
                    continue
                hidden = source_id not in on_air
            if told.get(source_id) != hidden:
                told[source_id] = hidden
                tell = getattr(source, "set_hidden", None)
                began = time.perf_counter()
                try:
                    if tell is not None:
                        tell(hidden)
                except Exception as exc:  # a plugin's source: never stop the render loop for it
                    log.warning("source %s set_hidden failed: %s", source_id, exc)
                # The late-frame line names the slowest: "telling sources shown or hidden" took 1146 ms on the PC
                # (2026-11-21) with nothing saying which source.
                spent = time.perf_counter() - began
                if spent > getattr(self, "_slowest_rest", ("", "", 0.0))[2]:
                    self._slowest_rest = (source_id, "hidden" if hidden else "shown", spent)
        for source_id in [s for s in told if s not in self._sources]:
            del told[source_id]

    def _rest_sources(self, compositor, now: float) -> None:
        """Put sources nothing has shown for a while to sleep, and wake the ones that are shown again.

        A sleeping capture or camera stops and frees its frames, a clock stops redrawing, and every
        sleeper's texture is dropped, so scenes you aren't using cost neither memory nor CPU. The
        short wait keeps flicking between two scenes from stopping and starting things each time.
        """
        shown = self._shown_sources()
        visible = self._visible = self._visible_sources()
        self._visible_fresh = True
        slowest = ("", "", 0.0)  # (source, wake or sleep, seconds): named in the late-frame line
        self._woke = 0
        waking_spent = 0.0
        for source_id, source in self._sources.items():
            began = time.perf_counter()
            if source_id in visible or not self._may_sleep(source):
                self._last_shown[source_id] = now
                if source.asleep:
                    # A bloom's sources woken all in one tick held that frame 17-58 ms, on every canvas (PC soak
                    # 2026-11-19: a vertical-only switch made the streamed horizontal canvas late). Once this
                    # tick's waking has taken WAKE_BUDGET_S, the rest stay asleep one more tick (a switch that
                    # waits for their pictures waits for these too: _wait_for_waking).
                    if waking_spent >= WAKE_BUDGET_S:
                        continue
                    source.asleep = False
                    if source.kind == "camera":
                        self._camera_back(source_id, now)
                    self._failing.pop(source_id, None)  # asked for a picture straight away
                    self._safely(source_id, source, "wake", source.wake)
                    self._woke += 1
                    waking_spent += time.perf_counter() - began
            elif not source.asleep and now - self._last_shown.setdefault(source_id, now) >= (
                    self._camera_wait.get(source_id, CAMERA_SLEEP_AFTER) if source.kind == "camera"
                    else SLEEP_AFTER if source_id in shown else 0.0):
                source.asleep = True
                if source.kind == "camera":
                    self._slept_at[source_id] = now
                self._safely(source_id, source, "sleep", source.sleep)
                if not getattr(source, "keeps_picture", False):
                    if source_id in getattr(compositor, "textures", {}):
                        self._had_picture.add(source_id)
                    else:
                        self._had_picture.discard(source_id)
                    compositor.forget(source_id)
            else:
                continue
            spent = time.perf_counter() - began
            if spent > slowest[2]:
                slowest = (source_id, "sleep" if source.asleep else "wake", spent)
        self._slowest_rest = slowest

    # Lifecycle safety ---------------------------------------------------------------------------------

    @staticmethod
    def _needs_new(source, settings: dict) -> bool:
        """True when a settings change means closing this source and opening a new one (Source.needs_new)."""
        try:
            return bool(source.needs_new(settings))
        except Exception as exc:  # a plugin's source: update it in place as before
            log.warning("source %s needs_new failed: %s", getattr(source, "id", "?"), exc)
            return False

    def _retire(self, source) -> None:
        """Close a source that's gone. Its stop() may wait for a device or a thread, so it runs on the closing
        helper (core/retire.py), never here: the render thread doesn't wait for anything."""
        if getattr(source, "stop_may_wait", True):
            retire.RETIRER.retire(source)
        else:
            self._safely(getattr(source, "id", "?"), source, "stop", source.stop)

    def _safely(self, source_id: str, source, step: str, call, *args) -> bool:
        """call(*args) for one source; if it fails, say so for that source and carry on with the rest."""
        try:
            call(*args)
            return True
        except Exception as exc:
            self._source_failed(source_id, source, step, exc)
            return False

    def _source_failed(self, source_id: str, source, step: str, exc: Exception) -> None:
        problem = f"{type(exc).__name__}: {exc}"
        if self._failed_said.get(source_id) != (step, problem):  # each new problem once, not every frame
            self._failed_said[source_id] = (step, problem)
            log.error("source %s (%s) failed in %s: %s", source_id, getattr(source, "kind", "?"), step, problem,
                      exc_info=exc)
            name = next((c.name for c in self._collection.sources if c.id == source_id), source_id)
            self.events.put(EngineError(f"Source {name!r} has a problem: {problem}"))
        try:
            source.status = f"failed: {problem}"[:160]
        except Exception:  # noqa: BLE001 (a read-only status: the log has it)
            pass

    def _source_works_again(self, source_id: str, source) -> None:
        self._failing.pop(source_id, None)
        said = self._failed_said.pop(source_id, None)
        if said is not None:
            log.info("source %s (%s) works again", source_id, getattr(source, "kind", "?"))
        if str(getattr(source, "status", "")).startswith("failed:"):
            try:
                source.status = "ok"
            except Exception:  # noqa: BLE001
                pass

    def _camera_back(self, source_id: str, now: float) -> None:
        """A closed camera is on screen again: back soon, it stays open longer next time; else 30 s again."""
        slept = self._slept_at.pop(source_id, None)
        if slept is None or now - slept > CAMERA_BACK_SOON_S:
            self._camera_wait.pop(source_id, None)
            return
        wait = min(CAMERA_SLEEP_MAX, 2 * self._camera_wait.get(source_id, CAMERA_SLEEP_AFTER))
        if wait != self._camera_wait.get(source_id):
            log.info("camera %s needed again %.0f s after it closed: now stays open %.0f s off screen",
                     source_id, now - slept, wait)
        self._camera_wait[source_id] = wait

    def _drawn_scenes(self, collection: SceneCollection, held_back: frozenset = frozenset()) -> list:
        """Scenes drawn right now. held_back: canvases whose bloom switched to is left out, for its sprigs: the
        canvas still shows the old one (_wait_for_waking) this tick. Its sources wake, but its sprigs are drawn
        from the tick it shows, not on every tick it waits (both blooms' sprigs each held tick: "sprigs 51.9 ms"
        on the PC, 2026-11-18)."""
        drawn = []
        for canvas in collection.canvases:
            if not canvas.enabled:
                continue
            on_air = collection.active_scenes.get(canvas.id)
            for scene_id in (on_air, self.staged.get(canvas.id)):
                if scene_id == on_air and canvas.id in held_back:
                    continue
                scene = next((s for s in collection.scenes if s.id == scene_id), None)
                if scene is not None and scene not in drawn:
                    drawn.append(scene)
            running = self._transitions.get(canvas.id)
            if running is not None and running.outgoing not in drawn:
                drawn.append(running.outgoing)
            waiting = self._waking.get(canvas.id)
            if waiting is not None and waiting[0] not in drawn:  # still on screen while a camera opens
                drawn.append(waiting[0])
        for name in list(self.views):  # what the virtual camera shows wakes its sources (and sprigs)
            _canvas, scene = self._view_scene(name)
            if scene is not None and scene not in drawn:
                drawn.append(scene)
        return drawn

    # Move transition -----------------------------------------------------------------------------------

    def _move_pieces(self, scene, sizes: dict, hidden: frozenset = frozenset()) -> list:
        """A bloom as the pieces a Move transition moves (render/move.py flatten): sprigs that fill the
        canvas one to one are opened up so what's inside them moves on its own."""
        collection = self._collection
        canvases = {c.id: (c.width, c.height) for c in collection.canvases}
        sources = {s.id: s for s in collection.sources}

        def part_size(source_id: str, canvas_id: str):
            source = sources.get(source_id)
            part = collection.part_of(source) if source is not None else None
            return canvases.get(collection.shape_for(part, canvas_id).canvas) if part is not None else None

        def view(shape, source):
            layout, fades = self._state_view(collection, shape)
            if collection.hidden_here(source):
                layout = collection.placement_view(source, layout)
            return layout, fades

        filtered, frozen = self._filtered, self._frozen  # a frozen sprig is drawn whole too: its held picture
        return move.flatten(scene, collection, sizes, part_size, view,
                            lambda sid: sid not in filtered and sid not in frozen,
                            canvases.get(scene.canvas, (0, 0)), hidden)

    def _move_layout(self, running: Running, canvas, hidden: frozenset = frozenset()) -> tuple[list, list]:
        """A Move's two blooms on a canvas as pieces (render/move.py flatten): the one it leaves, the one it goes
        to. The native engine takes them once, when the Move starts, and works out every frame itself."""
        sizes = {source_id: source.size for source_id, source in self._sources.items()}
        new = self._collection.active_scene(canvas.id)
        old = self._move_pieces(running.outgoing, sizes, hidden)
        now = self._move_pieces(new, sizes, hidden) if new is not None else []
        return old, now

    def _move_frame(self, running: Running, canvas, progress: float, hidden: frozenset = frozenset()) -> list:
        """This frame of a Move transition on a canvas, as pieces to draw (see compositor.render)."""
        old, now = self._move_layout(running, canvas, hidden)
        t = running.settings
        return move.frame(old, now, progress, t.easing, t.unmatched, (canvas.width, canvas.height))

    def _move_light(self, collection: SceneCollection, drawn: list) -> list:
        """What to draw sprigs for: during a Move, the bloom it moves out of needs only the sprigs it draws
        whole, not the ones opened up into their pieces (their sources are drawn straight onto the canvas)."""
        outgoing = {id(r.outgoing): r for r in self._transitions.values()
                    if r.settings.kind == "move" and r.began is not None}
        if not outgoing:
            return drawn
        held = {id(w[0]) for w in self._waking.values()}  # shown as it is while a camera opens
        out = []
        for scene in drawn:
            if id(scene) not in outgoing or id(scene) in held:
                out.append(scene)
                continue
            whole: dict[str, list] = {}  # canvas id -> sprigs drawn whole
            for piece in self._move_pieces(scene, {}):
                if collection.part_of(collection.source(piece.source)) is not None:
                    whole.setdefault(piece.canvas, []).append(SceneItem(source=piece.source))
            for canvas_id, items in whole.items():  # stand-ins placing just those sprigs, so they are drawn
                out.append(Scene(id=f"~move:{scene.id}", name="", canvas=canvas_id, items=items))
        return out

    def _render_parts(self, compositor, sizes: dict[str, tuple[int, int]], now: float) -> None:
        """Render every part layout in use once (inner ones first), then point each placement at its texture."""
        collection = self._collection
        # Only layouts that something drawn this tick shows, however deep: what is on air, the scene a
        # transition fades out of, the bloom being edited in Build, and the Part editor. The other blooms'
        # sprigs cost nothing until they are switched to.
        used: set[str] = set()
        held = self._sprigs_held = frozenset(cid for cid in self._waking if self._switch_waits(compositor, cid, now))
        stack = self._move_light(collection, self._drawn_scenes(collection, held))
        editing = None if self._engine_edits else self.editing_part  # the engine shows the open sprig itself
        if editing:
            stack += [p for p in collection.part_scenes() if p.id == editing]
            used.add(editing)
        while stack:
            for inner in collection.inner_shapes(stack.pop()):
                if inner.id not in used:
                    used.add(inner.id)
                    stack.append(inner)
        textures = {}
        plan = self._sprig_plan(collection)
        for part, size, tweaks in plan.order:
            if part.id not in used:
                continue
            compositor.parts = self._part_sources(textures, compositor)
            if part.id == editing:
                view, fades = part, None  # being edited: show every item, whatever its Live state
            else:
                view, fades = self._sprig_view(plan, collection, part)
            textures[part.id] = compositor.render_part(part.id, size, view, sizes, fades)
            if part.id == editing and self._preview_due("part", now):
                self.readbacks += 1
                data, drawn = self._read_full(compositor, f"part:{part.id}", now)
                if data is not None:
                    self.part_frames.put(Frame(f"part:{part.id}", size[0], size[1], data, drawn))
            # Placements of this layout with their own tweaks (hidden items) get their own copy.
            for source in tweaks:
                key = (source.id, part.id)
                tweaked = plan.tweaked.get(key) if fades is None and view is plan.views.get(part.id) else None
                if tweaked is None:
                    tweaked = collection.placement_view(source, view)
                    if fades is None and view is plan.views.get(part.id):
                        plan.tweaked[key] = tweaked
                textures[key] = compositor.render_part(f"{part.id}~{source.id}", size, tweaked, sizes, fades)
        compositor.parts = self._part_sources(textures, compositor)
        self._part_textures, self._parts_used = textures, used  # for screenshots (see _render_shot)

    def _render_staged(self, compositor, canvas, sizes, now: float) -> None:
        """Build: draw the bloom being edited on this canvas, for its preview only (not for outputs)."""
        scene_id = self.staged.get(canvas.id)
        if scene_id is None or scene_id == self._collection.active_scenes.get(canvas.id):
            return
        if not self._preview_due(f"next:{canvas.id}", now):
            return
        scene = next((s for s in self._collection.scenes if s.id == scene_id), None)
        if scene is None:
            return
        target = canvas.model_copy(update={"id": f"next:{canvas.id}"})
        compositor.render(target, scene, sizes)
        mailbox = self.frames.setdefault(target.id, Mailbox())
        frame = self._preview_frame(compositor, target.id, (canvas.width, canvas.height), now)
        if frame is not None:
            mailbox.put(frame)

    def _render_clean(self, compositor, canvas, sizes, now: float, outputs) -> None:
        """Source record: draw the bloom on air without the left-out sources, for taps on "clean:<id>".

        Only items placed in the bloom itself are left out (a whole sprig can be); it cuts between blooms
        instead of running the transition."""
        hidden = self.clean_hidden.get(canvas.id)
        if hidden is None or not outputs:
            return
        target = canvas.model_copy(update={"id": f"clean:{canvas.id}"})
        scene = self._collection.active_scene(canvas.id)
        compositor.render(target, self._collection.hide_items(scene, set(hidden)) if scene else scene, sizes)
        frames: dict = {}
        for callback, nv12_size in outputs:
            frame = self._output_frame(compositor, target, callback, nv12_size, now, frames)
            if frame is None:
                continue
            try:
                callback(frame)
            except Exception:
                log.exception("frame tap for %s failed", target.id)

    def _upload_held(self, compositor, held_back: dict) -> None:
        """Pictures held back for Python's compositor, now that it draws again. One that fails to copy is let go
        (said once for all of them), never tried again: the native simple run (2026-11-18) had a held-back
        Local time picture fail every tick, restart the graphics once a minute for 5 minutes, and keep
        the stream at 0 frames, because the same picture was kept and copied again after every restart."""
        failed = []
        for source_id, (frame, order, layout) in held_back.items():
            source = self._sources.get(source_id)
            if source is None or getattr(source, "asleep", False):
                continue  # gone, or asleep: it sends a fresh picture when it wakes
            try:
                self._upload(compositor, source_id, frame, order, layout, held=True)
            except Exception:  # noqa: BLE001 - _upload said which and why
                failed.append(source_id)
        if failed:
            log.warning("let go of held-back pictures that failed to copy: %s (their next pictures are copied as "
                        "usual)", ", ".join(failed))

    def _upload(self, compositor, source_id: str, frame, order: str, layout, held: bool = False) -> None:
        """compositor.upload, saying which source's picture it was if the copy fails. The PC heavy run
        (2026-11-17) had an access violation inside the copy to the card, twice, with nothing to say whose."""
        if isinstance(frame, drawn.Drawn):  # a list of pictures (core/drawn.py): Python's compositor gets it painted
            frame = frame.picture()
        try:
            compositor.upload(source_id, frame, order, layout)
        except Exception:
            source = self._sources.get(source_id)
            base = getattr(frame, "base", None)
            log.error("the picture of %s (%s%s) failed to copy to the graphics card: %s %s, strides %s, "
                      "%s, owned by %s", source_id, getattr(source, "kind", "?"), ", held back" if held else "",
                      getattr(frame, "shape", "?"), getattr(frame, "dtype", "?"), getattr(frame, "strides", "?"),
                      "contiguous" if getattr(getattr(frame, "flags", None), "c_contiguous", False)
                      else "not contiguous", type(base).__name__ if base is not None else "itself")
            raise

    def native_shot(self, canvas_id: str) -> tuple[bytes, int, int] | None:
        """A screenshot of a canvas the native engine draws, read from the engine (any thread; waits for its
        next frames): (BGRA premultiplied, width, height). None when Python must draw it: no engine, a canvas
        it doesn't show, or sources that stay out of screenshots. Before, every Control API screenshot on the
        native engine woke Python's compositor to draw the whole canvas on its own (PC heavy run, 2026-11-17:
        573-602 ms held each minute, twice followed by the graphics restarting)."""
        native = self._native
        if native is None or native.failed or not native.draws(canvas_id):
            return None
        try:
            hidden = self._not_in_screenshots()
            if canvas_id.startswith("view:"):  # a shot view (set_view shot=True), drawn in a slot of its own
                if canvas_id[5:] not in self._engine_views or hidden:
                    return None
            elif hidden and hidden & self._on_air_sources(self._collection, canvas_id):
                return None  # Snap's polaroid is up in this canvas: Python draws it without it
            slot = native._slots.get(canvas_id)
            size = native.engine._sizes.get(slot) if slot is not None else None
            if size is None:
                return None
            return native.engine.read_bgra(slot), size[0], size[1]
        except Exception as exc:  # noqa: BLE001 (the engine stopped, or the canvas changed: Python draws it)
            log.info("screenshot of %s from the native engine failed (%s): drawn here instead", canvas_id, exc)
            return None

    def _not_in_screenshots(self) -> frozenset[str]:
        """Sources that are only for show on stream and stay out of screenshots (Snap: its polaroid is the
        screenshot being shown off; a screenshot taken while one is up shouldn't have it in)."""
        return frozenset(source_id for source_id, source in self._sources.items()
                         if not getattr(source, "in_screenshots", True))

    def _render_shot(self, compositor, canvas, held, extra: dict, sizes, now: float, outputs, hidden) -> bool:
        """For taps on "shot:<canvas id>" (screenshots): the canvas as on air, with the hidden sources left out
        however deep in sprigs they sit. Sprigs that hold one are drawn again without it (only for this
        frame, into their own textures); everything else reuses this tick's textures. False if it couldn't
        (the screenshot then gets the canvas as it is)."""
        collection = self._collection
        parts_before = compositor.parts
        try:
            textures = dict(self._part_textures)
            used = self._parts_used
            changed: set[str] = set()
            if used:
                for part in collection.part_order():
                    if part.id not in used:
                        continue
                    if not (any(item.source in hidden for item in part.items)
                            or any(inner.id in changed for inner in collection.inner_shapes(part))):
                        continue
                    canvas_of = next((c for c in collection.canvases if c.id == part.canvas), None)
                    if canvas_of is None:
                        continue
                    changed.add(part.id)
                    compositor.parts = self._part_sources(textures, compositor)
                    size = (canvas_of.width, canvas_of.height)
                    if part.id == self.editing_part:
                        view, fades = part, None
                    else:
                        view, fades = self._state_view(collection, part)
                    view = collection.hide_items(view, set(hidden))
                    textures[part.id] = compositor.render_part(f"shot~{part.id}", size, view, sizes, fades)
                    main = collection.main_part(part)
                    for source in collection.sources:
                        if collection.part_of(source) is main and collection.hidden_here(source):
                            tweaked = collection.hide_items(collection.placement_view(source, view), set(hidden))
                            textures[(source.id, part.id)] = compositor.render_part(
                                f"shot~{part.id}~{source.id}", size, tweaked, sizes, fades)
            compositor.parts = self._part_sources(textures, compositor)
            target = canvas.model_copy(update={"id": f"shot:{canvas.id}"})
            scene = held or collection.active_scene(canvas.id)
            more = dict(extra)
            if more.get("previous") is not None:
                more["previous"] = collection.hide_items(more["previous"], set(hidden))
            if more.get("move") is not None:  # a Move: its pieces without them, however deep
                more["move"] = partial(more["move"], hidden=frozenset(hidden))
            compositor.render(target, collection.hide_items(scene, set(hidden)) if scene else scene, sizes, **more)
            frame = Frame(target.id, canvas.width, canvas.height, compositor.read(target.id), now)  # now, not later
        except Exception:
            log.exception("drawing a screenshot without %s failed", sorted(hidden))
            return False
        finally:
            compositor.parts = parts_before
        for callback, _nv12 in outputs:
            try:
                callback(frame)
            except Exception:
                log.exception("frame tap for %s failed", target.id)
        return True

    def _output_frame(self, compositor, canvas, callback, nv12_size, now: float, made: dict):
        """_frame for one output. An encoder that takes frames on the card (SprigCast's NVENC, see
        render/gpu_share.py) gets a GpuFrame instead, with no read-back; when none can be made this tick it gets
        the read-back frame as before. Outputs of one size share one of each."""
        if nv12_size is not None and getattr(self, "_native", None) is None and _wants_gpu(callback):
            key = ("gpu", nv12_size)
            if key not in made:
                share = getattr(compositor, "share_nv12", None)
                made[key] = share(canvas.id, nv12_size, now) if share is not None else None
            if made[key] is not None:
                return made[key]
        return self._frame(compositor, canvas, nv12_size, now, made)

    def _frame(self, compositor, canvas, nv12_size, now: float, made: dict) -> Frame | None:
        """This canvas for an output (NV12 at its size) or a full-size preview (RGBA). None: nothing yet.

        With wait_free, NV12 comes back a tick late without the CPU waiting on the GPU (see compositor._Pbos).
        """
        if nv12_size in made:
            return made[nv12_size]
        self.readbacks += 1
        if nv12_size is None:
            data, drawn = self._read_full(compositor, canvas.id, now)
            frame = None if data is None else Frame(canvas.id, canvas.width, canvas.height, data, drawn)
        elif self.wait_free:
            data, drawn = compositor.read_nv12_later(canvas.id, nv12_size, now, now)
            frame = None if data is None else Frame(canvas.id, nv12_size[0], nv12_size[1], data, drawn, "nv12")
        else:
            data = compositor.read_nv12(canvas.id, nv12_size)
            frame = Frame(canvas.id, nv12_size[0], nv12_size[1], data, now, "nv12")
        made[nv12_size] = frame
        return frame

    def _read_full(self, compositor, target_id: str, now: float) -> tuple[bytes | None, float]:
        """A whole canvas as RGBA: with wait_free a tick late without waiting on the GPU (it waited 100-140 ms
        about ten times an hour on the PC, 2026-11-16), else now. (None, 0) while nothing is ready yet."""
        later = getattr(compositor, "read_full_later", None) if self.wait_free else None
        if later is None:  # waiting reads, or a compositor without it
            return compositor.read(target_id), now
        data, drawn = later(target_id, now, now)
        return data, (drawn if data is not None else 0.0)

    @staticmethod
    def _output_keys(native, outputs) -> list:
        """Each frame tap as the native engine names its output (native_bridge output_key): its NV12 size and
        whether its encoder takes frames on the card. None for a full-size RGBA tap.

        A recording or stream that can take its frames from the engine itself (link_writer) gets an output of
        its own while the engine has outputs to spare: two of them sharing one output meant only one could be
        handed its frames (on the card, or at all), and the other took them through Python and lost some
        (horizontal.mkv at 57.17 fps beside the stream's encoder on the PC, 2026-11-18). Each extra output is
        one more scale on the graphics card, nothing on the CPU."""
        plain = [(native.output_key(size, size is not None and _wants_gpu(callback)), callback)
                 for callback, size in outputs]
        own = [native.output_key(size, size is not None and _wants_gpu(callback), id(callback.__self__))
               if key is not None and _engine_writer(callback) else key
               for (key, callback), (_cb, size) in zip(plain, outputs)]
        if len(set(own)) <= _engine_outputs(native):
            return own
        return [key for key, _callback in plain]  # too many: alike ones share, as before

    def _native_outputs(self, native, canvas, outputs) -> None:
        """Hand each output every frame the native engine finished for it, oldest first, each with the time
        the engine drew it: NV12 bytes, or the frame on the card for an encoder that takes it there (outputs
        alike share the frames). A recording through the engine's own writer is linked instead: the engine hands
        its frames over itself (native_bridge link_taps)."""
        made: dict = {}
        lost: dict = {}
        keys = self._output_keys(native, outputs)
        link_taps = getattr(native, "link_taps", None)
        # Recordings the engine hands frames to itself (its own writer): nothing for them here.
        linked = link_taps(canvas.id, [(callback, key) for (callback, _size), key in zip(outputs, keys)]) \
            if link_taps is not None else set()
        for (callback, _size), key in zip(outputs, keys):
            if id(getattr(callback, "__self__", None)) in linked:
                continue
            if key not in made:
                made[key] = [frame if key[1] else Frame(canvas.id, key[0][0], key[0][1], frame[0], frame[1], "nv12")
                             for frame in native.take_outputs(canvas.id, key)]
                lost[key] = getattr(native, "lost", 0)
            if lost[key]:  # frames the engine let go of: the recorder or stream counts them as dropped
                count = getattr(getattr(callback, "__self__", None), "frames_lost", None)
                if count is not None:
                    count(lost[key])
            for frame in made[key]:
                try:
                    callback(frame)
                except Exception:
                    log.exception("frame tap for %s failed", canvas.id)

    def _python_draws(self, native, taps: dict) -> bool:
        """With the native engine drawing the previews: does Python's compositor still draw anything this tick
        (an output, a screenshot, a transition, Build's edited bloom, the Part editor, a canvas the native engine
        doesn't show)? If not, it skips the sprigs too."""
        from sprigcast.render.native_bridge import native_soon, native_transition

        # (A switch waiting for a picture isn't a reason: the engine draws the bloom held, and the sources waited
        # for are copied for Python's compositor on their own, see `awaited` in _tick. Counting it as one had
        # Python draw every sprig and copy every source of every canvas for the whole wait.)
        if self._grabs or (self.editing_part and not self._engine_shows(native, PART_VIEW)):
            return True
        canvases = {c.id for c in self._collection.canvases if c.enabled}
        riding = {f"view:{name}" for names in self._riders.values() for name in names}
        for key in {*taps, *self._riders}:
            if key in riding:
                continue  # served with its canvas
            outputs = [*taps.get(key, ()), *self._rider_outputs(key, taps)]
            if not key.startswith("view:") and key not in canvases:
                return True  # a screenshot, source record, or the like
            if not native.serves(key, self._output_keys(native, outputs)):
                return True  # a projector, or an output only Python can make
        if any(not native_transition(r) and not native_soon(r) for r in self._transitions.values()):
            return True  # a stinger or move transition that is Python's
        active = self._collection.active_scenes
        if any(scene_id != active.get(canvas_id) and not self._engine_shows(native, STAGED_VIEW + canvas_id)
               for canvas_id, scene_id in self.staged.items()):
            return True  # Build's edited bloom, when the engine has no canvas slot left for it
        return any(c.enabled and not native.draws(c.id) for c in self._collection.canvases)

    def _preview_size(self, key: str, canvas_size: tuple[int, int]) -> tuple[int, int] | None:
        """The size to copy a preview at, or None for full size (shown big, or size unknown)."""
        want = self.preview_sizes.get(key)
        if not want:
            return None
        cw, ch = canvas_size
        scale = max(want[0] / cw, want[1] / ch)
        if scale >= 0.85:
            return None
        scale = math.ceil(scale * 16) / 16  # steps of 1/16, so resizing the window doesn't churn GPU targets
        return max(16, round(cw * scale)), max(16, round(ch * scale))

    def _preview_frame(self, compositor, target_id: str, canvas_size: tuple[int, int], now: float) -> Frame | None:
        self.readbacks += 1
        size = self._preview_size(target_id, canvas_size)
        if size is None:  # shown big: a full-size copy
            keep = getattr(compositor, "previewed_full", None)  # test stand-ins may not have it
            if keep is not None:
                keep(target_id)
            data, drawn = self._read_full(compositor, target_id, now)
            return None if data is None else Frame(target_id, *canvas_size, data, drawn)
        if self.wait_free:
            data, drawn = compositor.read_scaled_later(target_id, size, now, now, optional=True)
            return None if data is None else Frame(target_id, size[0], size[1], data, drawn, canvas_size=canvas_size)
        return Frame(target_id, size[0], size[1], compositor.read_scaled(target_id, size), now, canvas_size=canvas_size)

    def _preview_due(self, key: str, now: float) -> bool:
        """True when the UI shows this preview and its last copy is at least 1/preview_fps old."""
        wanted = self.previews
        if wanted is not None and key not in wanted:
            return False
        if now - self._preview_at.get(key, -1.0) < 1.0 / self.preview_fps - 0.002:
            return False
        self._preview_at[key] = now
        return True

    def _sprig_plan(self, collection: SceneCollection) -> _SprigPlan:
        """What the sprigs pass needs that only changes with the collection, worked out once per collection.

        The heavy run (2026-11-17, 17 sprigs on two canvases) spent 11-120 ms a frame in this pass. Most of it
        was Python, not drawing: every frame made a deep copy of each sprig as its Live state shows it, and
        looked up every placement of every sprig once per sprig drawn. The engine gets a new collection object
        for every change, so all of that holds until the next one."""
        plan = getattr(self, "_plan", None)
        if plan is not None and plan.collection is collection:
            return plan
        order = []
        for part in collection.part_order():
            if any(collection.contains(inner, part.id) for inner in collection.parts_inside(part)):
                continue  # a part inside itself: never drawn
            canvas = next((c for c in collection.canvases if c.id == part.canvas), None)
            if canvas is None:
                continue
            main = collection.main_part(part)
            tweaks = [s for s in collection.sources if collection.part_of(s) is main and collection.hidden_here(s)]
            order.append((part, (canvas.width, canvas.height), tweaks))
        placements = []  # (placement source id, canvas id, shape id) for every sprig placement
        for source in collection.sources:
            part = collection.part_of(source)
            if part is None:
                continue
            for canvas in collection.canvases:
                placements.append((source.id, canvas.id, collection.shape_for(part, canvas.id).id))
        self._plan = plan = _SprigPlan(collection, order, placements)
        return plan

    def _sprig_view(self, plan: _SprigPlan, collection: SceneCollection, part):
        """_state_view, kept for the collection while no state fade runs (a fade's view changes every frame)."""
        if self._state_fades.get(collection.main_part(part).id) is not None:
            return self._state_view(collection, part)
        view = plan.views.get(part.id)
        if view is None:
            view, fades = self._state_view(collection, part)
            if fades is not None:
                return view, fades
            plan.views[part.id] = view
        return view, None

    def _part_sources(self, textures: dict, compositor=None) -> dict:
        """(placement source id, canvas id) -> texture of the part's layout for that canvas. A frozen placement
        gets the copy made the first time this tick's picture of it was ready (see _freezes)."""
        out = {}
        if not textures:
            return out
        frozen = self._frozen
        holds = frozen and getattr(compositor, "held", None) is not None
        for source_id, canvas_id, shape_id in self._sprig_plan(self._collection).placements:
            texture = textures.get((source_id, shape_id), textures.get(shape_id))
            if texture is None:
                continue
            freeze = frozen.get(source_id) if holds else None
            if freeze is not None:
                held = compositor.held.get((source_id, canvas_id))
                if held is not None and held[0] == freeze:
                    texture = held[1]
                else:
                    texture = compositor.hold_copy((source_id, canvas_id), freeze, texture)
            out[(source_id, canvas_id)] = texture
        return out

    def _item_holds(self, compositor) -> dict[str, object]:
        """Which items are frozen on their own this tick (item key -> which freeze), told to Python's compositor.
        The native engine gets them with its snapshot (NativeCanvases.sync's holds)."""
        freezes = self._item_freezes
        if not freezes and not self._item_frozen and not getattr(compositor, "item_holds", None):
            return self._item_frozen
        wall = time.time()
        frozen = {key: token for key, (token, until) in freezes.items() if not until or wall < until}
        if frozen != self._item_frozen:
            self._item_frozen = frozen
        hold_items = getattr(compositor, "hold_items", None)  # (a compositor made anew is told again)
        if hold_items is not None and compositor.item_holds != self._item_frozen:
            hold_items(self._item_frozen)
        return self._item_frozen

    def _freezes(self, compositor) -> dict[str, object]:
        """Which sources and sprig placements are frozen this tick (source id -> which freeze), and let go of
        what the ones that ended held. A hold counts from the first tick that counts itself after it was asked for."""
        holds, filters = self._holds, self._freeze_filters
        if not holds and not filters:
            if self._frozen or self._caught:
                self._frozen, self._caught = {}, {}
            if getattr(compositor, "held", None):
                compositor.let_go_held({})
            return self._frozen
        frozen: dict[str, object] = {}
        wall, clock, tick = time.time(), time.perf_counter(), self._ticks
        for source_id, (key, until) in filters.items():
            if not until or wall < until:
                frozen[source_id] = key
        ended = False
        for source_id, (token, start, until) in holds.items():
            if clock >= until:
                ended = True
            elif start <= tick:
                frozen[source_id] = token
        if ended:  # forget holds whose time is up
            with self._holds_lock:
                self._holds = {k: v for k, v in self._holds.items() if v[2] > clock}
        self._frozen = frozen
        if self._caught:
            self._caught = {k: v for k, v in self._caught.items() if frozen.get(k) == v}
        if getattr(compositor, "held", None):
            compositor.let_go_held(frozen)
        return frozen

    def _warm_up(self, compositor) -> None:
        """Draw one frame before the clock starts. The graphics driver finishes its shaders and makes the
        sprig and canvas textures on the first draw: in the soak that first frame took a second, counted
        as a late frame and a hitch at start."""
        began = time.perf_counter()
        try:
            self._tick(compositor, began)
        except Exception:
            log.exception("first frame failed")  # the loop reports what keeps failing
        log.info("first frame drawn in %.0f ms", 1000 * (time.perf_counter() - began))

    def _budget_allows(self, source_id: str) -> bool:
        """A source over its time budget is asked for a frame only every few ticks (see _charge)."""
        every = self._source_every.get(source_id, 1)
        return every <= 1 or (self._ticks + hash(source_id)) % every == 0

    def _charge(self, source_id: str, source, spent: float) -> None:
        """Keep each source's average time per frame. One averaging over SOURCE_BUDGET_MS (a plugin that
        paints every frame: Death count took 15-24 ms while animating) is drawn every 2nd-4th tick, so the
        canvas, the encoders and the sound stay on time; its animation just moves in bigger steps."""
        # One frame counts at most CHARGE_CAP_MS: a single long one is a stall it waited out (Python's lock held
        # elsewhere, "everything waited"), not what the source costs. Charged in full, one 237 ms media frame on
        # the PC (2026-11-18) had it drawn every 4th frame for 0.8 s afterwards. A source steadily slow still
        # averages up to the cap and is drawn less often.
        avg = self._source_ms.get(source_id, 0.0) * 0.9 + min(1000 * spent, CHARGE_CAP_MS) * 0.1
        self._source_ms[source_id] = avg
        before = self._source_every.get(source_id, 1)
        every = min(4, max(1, math.ceil(avg / SOURCE_BUDGET_MS))) if avg > SOURCE_BUDGET_MS else 1
        if every < before and avg > (every * SOURCE_BUDGET_MS) * BUDGET_MARGIN:
            every = before  # only back to drawing more often once well under: Instant replay at 5.1-5.2 ms
            # switched 6 times in 6 s in the 2026-11-17 heavy run
        if every != before:
            self._source_every[source_id] = every
            if every > 1 and source_id not in self._budget_logged:
                self._budget_logged.add(source_id)
                log.info("source %s (%s) takes %.1f ms a frame: drawing it every %d frames to keep the canvas "
                         "on time", source_id, source.kind, avg, every)
            elif every == 1 and source_id in self._budget_logged:
                self._budget_logged.discard(source_id)
                log.info("source %s (%s) back to every frame (%.1f ms a frame)", source_id, source.kind, avg)

    def _mark(self, name: str) -> None:
        now = time.perf_counter()
        marks = self._marks
        if marks and now - marks[-1][1] >= SLOW_STEP_S and not getattr(self, "_window_at", ""):
            # Settings opening made "sprigs" take 14 ms on the PC (2026-11-17) with nothing slow in the step
            # itself: it waited for Python's lock. Where the window's thread is right after says who held it.
            self._window_at = pacing.window_at()
        marks.append((name, now))

    def _follow_rate(self, fps: int) -> None:
        """The native engine and captures follow the drawing rate when it changes."""
        native = self._native
        if native is not None and not native.failed and getattr(native, "fps", fps) != fps:
            try:
                native.engine.set_rate(fps, 1)
                native.fps = fps
            except Exception as exc:  # noqa: BLE001 (it keeps its old rate; drawing still follows)
                log.warning("native engine: couldn't change its rate to %d fps: %s", fps, exc)
        for source in list(self._sources.values()):
            if hasattr(source, "canvas_fps"):
                source.canvas_fps = fps

    def _where(self) -> str:
        """The last frame's slowest steps, for the late-frame log line."""
        marks = getattr(self, "_marks", None)
        if not marks:
            return ""
        steps = sorted(((b[1] - a[1], b[0]) for a, b in zip(marks, marks[1:])), reverse=True)[:3]
        text = ", ".join(f"{name} {1000 * spent:.1f}" for spent, name in steps if spent >= 0.0005)
        slow = getattr(self, "_slowest_source", None)
        if slow and slow[1] >= 0.002:
            text += f"; slowest source {slow[0]} {1000 * slow[1]:.1f}"
        rest = getattr(self, "_slowest_rest", None)
        if rest and rest[2] >= 0.002:
            text += f"; {rest[1]} {rest[0]} {1000 * rest[2]:.1f}"
        pruned = getattr(self, "_pruned", 0)
        if pruned and any(name == "letting go of idle copies" for name, _t in marks):
            text += f"; let go of {pruned} idle copies"
        window = getattr(self, "_window_at", "")
        if window:  # a step held up by Python's lock: what the window's thread was doing then
            text += f"; window at {window}"
        return f" ({text})" if text else ""

    def _tick(self, compositor, now: float) -> None:
        self._marks = [("start", time.perf_counter())]
        self._window_at = ""
        compositor.time = now
        if self._release_later:
            self._release_unused(compositor)
            self._mark("letting go of textures")
        if self._refresh_outputs:
            self._refresh_outputs = False
            if self._native is not None and not self._native.failed and self._native.reset_outputs() is False:
                self._refresh_outputs = True  # the engine's frame is stuck: once it's going again
                if not self._refresh_waiting:
                    log.warning("outputs are made again once the native engine's frame is unstuck")
                self._refresh_waiting = True
            else:
                self._refresh_waiting = False
                prune = getattr(compositor, "prune", None)
                self._pruned = prune(0.0) if prune is not None else 0
                log.warning("outputs made again: %d of Python's output copies let go", self._pruned)
            self._mark("making outputs again")
        if getattr(compositor, "_to_free", None):  # spares past their time: a few each frame (Compositor.free_some)
            compositor.free_some()
        if now - self._pruned_at > 1.0:  # a recording or source record that ended leaves its targets idle
            self._pruned_at = now
            prune = getattr(compositor, "prune", None)  # test stand-ins may not have it
            if prune is not None:
                self._pruned = prune()
                self._mark("letting go of idle copies")
        self._rest_sources(compositor, now)
        self._mark("waking and pausing")
        self._show_hidden()
        self._mark("telling sources shown or hidden")
        slowest = ("", 0.0)
        self._ticks = getattr(self, "_ticks", 0) + 1
        frozen = self._freezes(compositor)
        self._item_holds(compositor)
        textures = getattr(compositor, "textures", {})
        woke = bool(self._woke or self._waking)  # a bloom is coming back: first pictures are spread out
        # Sources a switch is waiting on: only these are copied for Python's compositor while it waits (their
        # textures there are what ends the wait). Every other source stays with the engine: copying them all
        # made a switch on one canvas cost the other canvas a read-back of each video (media 10.8 ms a frame).
        awaited = set().union(*(ids for _o, _t, ids in self._waking.values())) if self._waking else ()
        first_bytes, waking = 0, []
        native = self._native if self._native is not None and not self._native.failed else None
        for source_id, source in self._sources.items():
            freeze = frozen.get(source_id) if frozen else None
            # A video the engine's own player decodes: the native engine takes its pictures itself, frame by frame
            # (not while it is frozen or asleep: then it keeps the picture it has).
            player = None if source.asleep else getattr(source, "engine_player", None)
            # Frozen, a player is let go (it keeps its picture); a browser page's block or a capture stays, held, so
            # the page isn't left waiting for the engine to take its pictures, and a capture needn't start over.
            if freeze is not None and not (isinstance(player, str) or getattr(player, "holds", False)):
                player = None
            direct = native is not None and native.attach_media(source_id, player, held=freeze is not None)
            if direct and hasattr(type(source), "engine_layout"):
                # A player shared with other sources may play bigger than this one is laid out (one camera, two
                # modes): the engine draws it at this source's size, where its border is.
                native.media_size(source_id, source.engine_layout)
            if source.asleep or not self._budget_allows(source_id) or (
                    self._failing and now < self._failing.get(source_id, 0.0)):
                if freeze is not None and source_id in textures:
                    self._caught.setdefault(source_id, freeze)  # its picture now is the frozen one, as on screen
                elif source.asleep and self._missed:
                    self._missed.pop(source_id, None)
                continue
            began = time.perf_counter()
            missed = self._missed.pop(source_id, None) if self._missed and freeze is None else None
            try:
                # (A capture the engine does itself isn't copied for a waiting switch either: the engine says
                # when it has a picture, see _has_picture. Its copy is a read-back from the card.)
                if direct and not self._python_drew and (
                        source_id not in awaited or hasattr(type(source), "engine_shows")):
                    source.engine_tick()  # nothing of Python's shows it this tick: no picture is copied at all
                    frame = None
                else:
                    frame = source.video_frame()
            except Exception as exc:  # this source's problem only: the canvas keeps its last picture of it
                self._failing[source_id] = now + FAIL_RETRY_S
                self._source_failed(source_id, source, "video_frame", exc)
                continue
            if self._failed_said and (source_id in self._failing or self._failed_said.get(
                    source_id, ("",))[0] in ("video_frame", "wake")):
                self._source_works_again(source_id, source)
            if freeze is not None and self._caught.get(source_id) == freeze and source_id in textures:
                if frame is not None:  # frozen: it keeps the picture it had (the source runs on, unseen)...
                    self._missed[source_id] = frame  # ...and shows its newest once it's let go: a text or
                frame = None  # Death count only sends a picture when it changes
            elif freeze is not None:
                self._caught[source_id] = freeze  # this frame (or the one it has, if none came) is the frozen one
            if frame is None and missed is not None:
                frame = missed
            if frame is not None and woke and source_id not in textures:
                # A first picture after a wake: at most FIRST_UPLOAD_BYTES of them a tick, the rest the next tick
                # (the switch waits for them, see _wait_for_waking). All at once they held the PC's switch frame
                # 130-220 ms (2026-11-18): every GIF, video and card of the bloom copied to the card in one tick.
                if first_bytes >= FIRST_UPLOAD_BYTES:
                    self._missed[source_id] = frame
                    frame = None
                else:
                    first_bytes += getattr(frame, "nbytes", 0)
            if frame is not None:
                layout = source.size if getattr(source, "sharp", False) or getattr(source, "shrinks", False) else None
                if native is not None and not self._python_drew and source_id not in awaited and freeze is None:
                    # Python's compositor drew nothing last tick: only the engine gets it. Python's copy waits
                    # here (the newest only) for a tick it draws again. The native simple run (2026-11-17) had
                    # every camera picture sent to both, ~3x the default engine's CPU.
                    self._held_back[source_id] = (frame, source.channel_order, layout)
                else:
                    self._held_back.pop(source_id, None)
                    self._upload(compositor, source_id, frame, source.channel_order, layout)
                if native is not None and not direct:
                    if isinstance(frame, drawn.Drawn):
                        native.draw_list(source_id, frame)
                    else:
                        # (One color is a few pixels stretched over its size: the engine has to be told the size,
                        # or it draws a 2 x 2 dot. Python's compositor knows it from the layout.)
                        stretched = source.size if getattr(source, "stretched", False) else None
                        native.upload(source_id, frame, source.channel_order, layout or stretched)
            spent = time.perf_counter() - began
            self._charge(source_id, source, spent)
            if spent > slowest[1]:
                slowest = (source_id, spent)
            if self._woke and spent >= WAKE_LOG_S:
                waking.append(f"{source_id} {1000 * spent:.1f}{'' if frame is not None else ' (no picture)'}")
        self._slowest_source = slowest
        if waking:  # every slow source of a frame that woke some: "(no picture)" means it waited, not drew
            log.info("waking frame: %s", ", ".join(waking))
        self._mark("sources")
        with self._taps_lock:
            taps = {canvas_id: list(callbacks) for canvas_id, callbacks in self._taps.items() if callbacks}
        sizes = {source_id: source.size for source_id, source in self._sources.items()}
        self._served_views = set()
        if native is not None:
            self._engine_views, self._riders = self._native_views(taps, sizes)
            from sprigcast.render.native_bridge import native_soon, native_transition

            stingers = {r.stinger.id: r.stinger.engine_player for r in self._transitions.values()
                        if r.stinger is not None and native_transition(r)}
            for sting_id, player in stingers.items():  # a stinger's clip: drawn by the engine from its player
                native.attach_media(sting_id, player)
            native.forget_gone({*self._sources, *stingers})
            # A stinger waiting for its clip: the engine goes on showing the bloom it changes from (native_soon).
            kept = {cid: r.outgoing for cid, r in self._transitions.items() if native_soon(r)}
            kept.update({cid: w[0] for cid, w in self._waking.items()})
            native.sync(self._collection, getattr(compositor, "effects", {}), self._state_view, bool(self._state_fades),
                        {cid: r for cid, r in self._transitions.items() if cid not in self._waking}, self._wipe_map,
                        views=self._engine_views, held=kept, move_pieces=self._move_layout,
                        holds=self._item_frozen)
            native.check(now)
            sprigcore_drain()
            self._mark("native engine")
        else:
            self._engine_views, self._riders = {}, {}
        self._engine_edits = self._engine_shows(native, PART_VIEW)
        self._python_drew = native is None or self._python_draws(native, taps)
        if self._held_back:
            if self._python_drew or native is None:  # it draws again: it gets the newest pictures it skipped
                held_back, self._held_back = self._held_back, {}
                self._upload_held(compositor, held_back)
                self._mark("pictures held back")
            else:  # only sources still drawn keep one waiting (a bloom gone to sleep lets go of its pictures)
                self._held_back = {sid: held for sid, held in self._held_back.items()
                                   if sid in self._sources and not getattr(self._sources[sid], "asleep", False)}
        self._sprigs_held = frozenset()
        if not self._python_drew:
            self._part_textures, self._parts_used = {}, set()  # nothing Python draws this tick shows a sprig
        elif self._collection.parts():
            self._render_parts(compositor, sizes, now)
        else:
            self._part_textures, self._parts_used = {}, set()
            if compositor.parts:
                compositor.parts = {}
        self._mark("sprigs")
        drawn_now: dict[str, dict] = {}  # canvas id -> its frames this tick, for canvases drawn this tick
        for canvas in self._collection.canvases:
            if not canvas.enabled:
                continue
            held = self._still_waking(compositor, canvas.id, now)
            extra = {} if held is not None else self._transition_step(compositor, canvas.id, now) or {}
            outputs = taps.get(canvas.id, ())
            shots = taps.get(f"shot:{canvas.id}", ())
            if shots:  # a screenshot: drawn without what's only for show (Snap's polaroid), if anything is
                hidden = self._not_in_screenshots()
                if not (hidden and self._render_shot(compositor, canvas, held, extra, sizes, now, shots, hidden)):
                    outputs = (*outputs, *shots)  # the canvas as it is: shares its copy
            preview = self._preview_due(canvas.id, now)
            if native is not None:
                # While a switch waits for a camera the engine draws the bloom held (sync's held), so its outputs
                # go on: dropping them each switch made new texture pools every time, and the writer on the card
                # keeps every texture it was ever handed (NVENC knows them by pointer): ~10 MB of graphics memory a
                # minute with a switch every 15 s (PC soak 2026-11-18).
                waits = held is None and native_soon(self._transitions.get(canvas.id))  # its clip isn't there yet
                native_shows = (not extra or waits or native.handles(extra)) and native.draws(canvas.id)
                wanted = self.previews is None or canvas.id in self.previews
                size = self._preview_size(canvas.id, (canvas.width, canvas.height)) or (canvas.width, canvas.height)
                native.watch(canvas.id, size if native_shows and wanted else None, self.preview_fps)
                if native_shows and preview:
                    preview = False  # the native engine drew it: Python's compositor needn't
                    got = native.take(canvas.id)
                    if got is not None:
                        data, w, h = got
                        self.frames[canvas.id].put(Frame(canvas.id, w, h, data, now,
                                                         canvas_size=(canvas.width, canvas.height)))
                ridden = [*outputs, *self._rider_outputs(canvas.id, taps)]  # the virtual camera showing it, too
                keys = self._output_keys(native, ridden)
                native_out = native_shows and native.serves(canvas.id, keys)
                pause = getattr(native, "pause_outputs", None)
                if not native_out and pause is not None and extra and not native.handles(extra) \
                        and native.draws(canvas.id) and native.serves(canvas.id, keys):
                    pause(canvas.id, keys, canvas.downscale)  # a transition only Python draws: kept, not remade
                else:
                    native.set_outputs(canvas.id, keys if native_out else (), canvas.downscale)
                if native_out:  # recordings and streams: the frames the native engine finished since last tick
                    self._native_outputs(native, canvas, ridden)
                    self._served_views.update(self._riders.get(canvas.id, ()))
                    self._mark(f"{canvas.id} streams and recordings")  # the engine's finished frames, handed on
                    outputs = ()
            if not self._engine_staged(native, canvas, now):
                self._render_staged(compositor, canvas, sizes, now)
            self._render_clean(compositor, canvas, sizes, now, taps.get(f"clean:{canvas.id}", ()))
            if not outputs and not preview:
                continue  # nobody is watching or recording this canvas right now
            self._mark("canvases")  # the late-frame line splits the canvases into these steps
            compositor.render(canvas, held or self._collection.active_scene(canvas.id), sizes, **extra)
            self._mark(f"{canvas.id} drawn")
            frames: dict = {}  # nv12 size (None = RGBA) -> Frame, so outputs of one size share one readback
            drawn_now[canvas.id] = frames  # a view of this same picture (the virtual camera) reads it from here
            if preview:
                size = self._preview_size(canvas.id, (canvas.width, canvas.height))
                if size is None:  # full size: shares the RGBA copy with any output that wants one
                    shown = self._frame(compositor, canvas, None, now, frames)
                else:
                    shown = self._preview_frame(compositor, canvas.id, (canvas.width, canvas.height), now)
                if shown is not None:
                    self.frames[canvas.id].put(shown)
                self._mark(f"{canvas.id} preview copy")
            for callback, nv12_size in outputs:
                frame = self._output_frame(compositor, canvas, callback, nv12_size, now, frames)
                self._mark(f"{canvas.id} read back")
                if frame is None:
                    continue  # the first tick of a wait-free copy: its frame arrives next tick
                if native is not None and nv12_size is not None:
                    native.served(canvas.id, nv12_size, frame.time)
                try:
                    callback(frame)
                except Exception:
                    log.exception("frame tap for %s failed", canvas.id)
                self._mark(f"{canvas.id} {getattr(callback, '__qualname__', 'output')}")
        self._mark("canvases")
        self._engine_part(native, now)
        self._render_views(compositor, sizes, now, taps, drawn_now, native)
        self._mark("virtual camera")
        if self._grabs:
            self._serve_grabs(compositor, sizes)
            self._mark("snapshots")


class _SprigPlan:
    """Engine._sprig_plan: the sprigs pass's lookups for one collection object."""

    __slots__ = ("collection", "order", "placements", "views", "tweaked")

    def __init__(self, collection, order, placements):
        self.collection = collection
        self.order = order  # (layout, canvas size, placements with their own hidden items), inner ones first
        self.placements = placements
        self.views: dict = {}  # layout id -> the layout as its Live state shows it (no fade running)
        self.tweaked: dict = {}  # (placement source id, layout id) -> that view with the placement's hidden items


MOST_DETAIL_PIXELS = 4096  # a sharp source's picture is never drawn wider or taller than this
DETAIL_RANGE = (0.125, 4.0)


SHRINK_STEPS = (1.0, 0.5, 0.25)  # a video is decoded at its size, half or a quarter (never smaller than shown)


def sprigcore_drain() -> None:
    """The native engine's log lines into SprigCast's log."""
    from sprigcast import sprigcore

    sprigcore.drain_log()


def shrink_step(scale: float) -> float:
    """The smallest of SHRINK_STEPS that still has a pixel for every canvas pixel it covers at `scale`. Only
    whole halvings, so a video dragged bigger or smaller changes size a couple of times, not every frame."""
    for step in reversed(SHRINK_STEPS):
        if scale <= step + 1e-6:
            return step
    return 1.0


def detail_step(scale: float, size: tuple[int, int]) -> float:
    """The detail a sharp source shown at `scale` draws at: rounded UP to a step of about 19% (2 ** 0.25), so
    dragging its corner redraws it a few times, not every frame, and it's never drawn smaller than shown."""
    most = min(DETAIL_RANGE[1], MOST_DETAIL_PIXELS / max(1, size[0]), MOST_DETAIL_PIXELS / max(1, size[1]))
    scale = min(max(scale, DETAIL_RANGE[0]), most)
    step = 2.0 ** (math.ceil(math.log2(scale) * 4 - 1e-6) / 4)
    return round(min(step, most), 4)


def shown_detail(collection: SceneCollection, sizes: dict[str, tuple[int, int]]) -> dict[str, float]:
    """How many canvas pixels each source in `sizes` covers per pixel of its own, at the biggest it's placed
    anywhere in the garden: through every sprig it sits in (a sprig made half size halves what's inside it).
    Each bloom and each sprig (as Build shows it, at canvas size) is a starting point."""
    configs = {s.id: s for s in collection.sources}
    canvases = {c.id: (c.width, c.height) for c in collection.canvases}
    best: dict[str, float] = {}
    for scene in collection.scenes:
        _walk_detail(collection, configs, canvases, sizes, best, scene, 1.0, (scene.id,))
    return best


def _walk_detail(collection, configs, canvases, sizes, best, scene, factor: float, path: tuple[str, ...]) -> None:
    """shown_detail's walk (not a closure, like _walk_visible)."""
    for item in scene.items:
        config = configs.get(item.source)
        part = collection.part_of(config) if config is not None else None
        if part is not None:
            if part.id in path or len(path) >= 16:
                continue
            shape = collection.shape_for(part, scene.canvas)
            size = canvases.get(shape.canvas)
        else:
            size = sizes.get(item.source)
            shape = None
        if not size or size[0] <= 0 or size[1] <= 0:
            continue
        sx, sy = item.picture_scale(size)
        scale = factor * max(abs(sx), abs(sy))
        if shape is not None:
            _walk_detail(collection, configs, canvases, sizes, best, shape, scale, (*path, part.id))
        elif scale > best.get(item.source, 0.0):
            best[item.source] = scale


def _walk_visible(collection, configs, visible: set[str], scene, hidden: set[str], path: tuple[str, ...]) -> None:
    """Engine._visible_sources' walk. Not a closure: a nested function that calls itself is a reference
    cycle, so every frame left two for Python's cleanup to find (with the frames' sets and lists)."""
    if scene.kind == "part":
        hidden = hidden | collection.state_hidden(scene)
    for item in scene.items:
        if not item.visible or item.source in hidden:
            continue
        visible.add(item.source)
        config = configs.get(item.source)
        part = collection.part_of(config) if config is not None else None
        if part is not None and part.id not in path and len(path) < 16:
            _walk_visible(collection, configs, visible, collection.shape_for(part, scene.canvas),
                          collection.hidden_here(config), (*path, part.id))
