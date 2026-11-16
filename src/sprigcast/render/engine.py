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
from typing import Any

import numpy as np

from sprigcast.audio import listen
from sprigcast.audio.mixer import Ducking, Mixer
from sprigcast.audio.monitor import Monitor
from sprigcast.core.events import (
    AudioLevels,
    EngineError,
    ReplaceCollection,
    Shutdown,
    SourceInfo,
    Stats,
)
from sprigcast.core.filters import SOUND_LEVELS, AudioChain, video_effects
from sprigcast.core.mailbox import Mailbox
from sprigcast.core.scene import PART_KIND, STATE_FADE_S, Scene, SceneCollection, SceneItem
from sprigcast.core.source import Source, create_source, source_class
from sprigcast.render import move
from sprigcast.render.stinger import StingerPlayer

log = logging.getLogger(__name__)


SLEEP_AFTER = 5.0  # seconds a source stays ready after the last scene showing it left the screen
# Cameras and capture cards take a second or two to open, so they wait longer before closing; closing
# them lets the camera's own app (Elgato Camera Hub: 11% CPU, 22% GPU on the user's PC) go idle too.
CAMERA_SLEEP_AFTER = 30.0
# A camera that's needed again soon after it closed waits longer next time: twice as long each time it comes
# back within CAMERA_BACK_SOON_S, up to CAMERA_SLEEP_MAX. On the PC (2026-11-16) one closed and opened 14 times
# an hour as blooms switched, each time starting Camera Hub again. Left closed that long, it's back to 30 s.
CAMERA_SLEEP_MAX = 300.0
CAMERA_BACK_SOON_S = 600.0
WATCHER_PARK_S = 60.0  # a frame watcher (background remover) nothing fed for this long lets go of everything
LATE_LOG_EVERY_S = 10.0  # late-frame log lines at most this often (the rest are counted in the next one)
GPU_SKIP_MAX_S = 2.0  # the card stuck longer than this (a lost device?): draw anyway, waiting as before
SOURCE_BUDGET_MS = 5.0  # a source averaging more than this per frame is drawn less often (see _charge)
WAKE_WAIT_S = 2.0  # a switch to a bloom whose camera is closed shows the old bloom until it opens, at most this
PREVIEW_FPS = 30  # the UI draws previews about 30 times a second; copying more is wasted


class Frame:
    """One finished canvas image, top row first: RGBA bytes, or NV12 (Y plane then CbCr) at an output's size."""

    __slots__ = ("canvas_id", "width", "height", "data", "time", "fmt", "canvas_size")

    def __init__(self, canvas_id: str, width: int, height: int, data: bytes, time: float = 0.0, fmt: str = "rgba",
                 canvas_size: tuple[int, int] | None = None):
        self.canvas_id, self.width, self.height, self.data = canvas_id, width, height, data
        self.time = time  # perf_counter() at the start of the tick that drew it
        self.fmt = fmt
        self.canvas_size = canvas_size or (width, height)  # the canvas it shows (a preview may be smaller)


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
        self._sources: dict[str, Source] = {}
        self._stopped_unknown: set[str] = set()  # sources whose plugin was removed while they ran
        self._thread = threading.Thread(target=self._run, name="sprigcast-render", daemon=True)
        self._stopping: threading.Thread | None = None  # see stop(wait=False)
        self.mixer = Mixer(report=lambda levels: self.events.put(AudioLevels(levels)))
        self.monitor = Monitor()  # plays monitored sources on the speakers, opened only when one is
        self._monitoring = False
        self._taps: dict[str, list[Callable[[Frame], None]]] = {}  # canvas id -> recorders, later streams
        self._taps_lock = threading.Lock()
        self._grabs: list[tuple[str | None, Callable[[np.ndarray | None], None]]] = []  # see grab_source
        self._transitions: dict[str, Running] = {}  # canvas id -> the transition it is in
        self._filtered: frozenset[str] = frozenset()  # sources with video filters, see _sync_filters
        # canvas id -> (outgoing scene, give up at, sources still opening): a switch waiting for them
        self._waking: dict[str, tuple] = {}
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
        # Which previews the UI is showing ("horizontal", "next:horizontal", "part"); None = all of them.
        # A canvas nobody watches and nothing records is not drawn or copied back at all; a watched one
        # is copied back at most PREVIEW_FPS times a second. The UI replaces the whole set.
        self.previews: frozenset[str] | None = None
        # How big each preview is on screen, in physical pixels: previews are shrunk on the GPU to that,
        # so a 1080p canvas shown at 600x340 copies 0.8 MB per frame instead of 8 MB.
        self.preview_sizes: dict[str, tuple[int, int]] = {}
        self._preview_at: dict[str, float] = {}
        self.readbacks = 0  # canvases copied from the GPU, for Stats
        self.wait_free = True  # copy outputs and small previews back a tick late instead of waiting on the GPU
        # Sources nothing on screen shows rest: "not_cameras" (cameras stay ready, they take a second
        # to open), "all", or "off". Set by the UI from Settings → General.
        self.sleep_policy = "not_cameras"
        self._last_shown: dict[str, float] = {}  # source id -> when something on screen last showed it
        self._camera_wait: dict[str, float] = {}  # camera id -> how long it stays open off screen (see above)
        self._slept_at: dict[str, float] = {}  # camera id -> when it last closed
        self._told_hidden: dict[str, bool] = {}  # source id -> what set_hidden() last said (see _show_hidden)
        self._visible: set[str] = set()  # sources showing in what's drawn, this tick (see _show_hidden)
        # Plugins' frame watchers (plugin_hooks.register_frame_watcher): source id -> (filter kind, its settings)
        # for sources that want one, and source id -> [watcher, its kind, when it last got a frame].
        self._watched: dict[str, tuple[str, dict]] = {}
        self._watchers: dict[str, list] = {}
        # Source record: canvas id -> sources left out of its clean copy ("clean:<canvas id>"), which
        # frame taps on that id get. Replaced whole by the UI (see set_clean).
        self.clean_hidden: dict[str, frozenset[str]] = {}
        # Extra pictures for taps on "view:<name>" (the virtual camera): name -> (canvas id, kind, ref),
        # kind "bloom" (a bloom drawn whether or not it's on air), "source" (one source, fitted) or
        # "scene" (ref is a Scene a plugin made, e.g. Instant replay's game with the camera in a corner).
        # Replaced whole by the UI (see set_view); drawn only while something taps them.
        self.views: dict[str, tuple[str, str, Any]] = {}
        self._view_at: dict[str, float] = {}  # view name -> when a screen-only view was last drawn
        self._ticks = 0  # ticks drawn (each counts itself up early on): what a hold or a screenshot waits for
        # Freezes. Snap's (see hold): source id -> (token, first tick, until perf_counter), replaced whole under
        # _holds_lock. Freeze filters (the Freeze plugin): source id -> ("filter", run) and its time.time() end
        # (0: none), from _sync_filters. Each tick: _frozen (source id -> which freeze, for the ones frozen now)
        # and _caught (source id -> the freeze whose picture its texture holds: uploads stop once caught).
        self._holds: dict[str, tuple[object, float, float]] = {}
        self._holds_lock = threading.Lock()
        self._freeze_filters: dict[str, tuple[tuple, float]] = {}
        self._frozen: dict[str, object] = {}
        self._caught: dict[str, object] = {}
        self._missed: dict[str, np.ndarray] = {}  # a frozen source's newest picture, shown when it's let go

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

    def grab_source(self, source_id: str | None, callback: Callable[[np.ndarray | None], None]) -> None:
        """Hand the next picture of one source (with its filters), or of what's on air when source_id is
        None, to callback as an H x W x 4 RGBA array (None if there's nothing to picture). The callback
        runs on the render thread and must not block."""
        with self._taps_lock:
            self._grabs.append((source_id, callback))

    def _serve_grabs(self, compositor, sizes) -> None:
        with self._taps_lock:
            grabs, self._grabs = self._grabs, []
        canvases = [c for c in self._collection.canvases if c.enabled] or self._collection.canvases
        for source_id, callback in grabs:
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
                    picture = compositor.render_source(source_id, sizes, canvas_id)
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

    def set_clean(self, canvas_id: str, hidden) -> None:
        """Source record: draw a copy of this canvas without these sources (None: stop drawing it)."""
        clean = dict(self.clean_hidden)
        if hidden is None:
            clean.pop(canvas_id, None)
        else:
            clean[canvas_id] = frozenset(hidden)
        self.clean_hidden = clean

    def set_view(self, name: str, canvas_id: str | None = None, kind: str = "bloom", ref=None) -> None:
        """Draw a bloom, a single source or a plugin's own Scene on a canvas's size for taps on "view:<name>"
        (None: stop)."""
        views = dict(self.views)
        if canvas_id is None:
            views.pop(name, None)
        else:
            views[name] = (canvas_id, kind, ref)
        self.views = views

    def _view_scene(self, name: str, sizes: dict | None = None):
        """The scene a view draws, or None when what it names is gone."""
        from sprigcast.core.scene import Scene, SceneItem

        canvas_id, kind, ref = self.views[name]
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

    def _render_views(self, compositor, sizes, now: float, taps: dict) -> None:
        for name in list(self.views):
            outputs = taps.get(f"view:{name}", ())
            if not outputs:
                continue
            if all(size is None for _cb, size in outputs):  # only screens watch it (a projector): ~30 fps is plenty
                if now - self._view_at.get(name, -1.0) < 1 / PREVIEW_FPS - 0.002:
                    continue
                self._view_at[name] = now
            canvas, scene = self._view_scene(name, sizes)
            if canvas is None:
                continue
            target = canvas.model_copy(update={"id": f"view:{name}"})
            compositor.render(target, scene, sizes)
            frames: dict = {}
            for callback, nv12_size in outputs:
                frame = self._frame(compositor, target, nv12_size, now, frames)
                if frame is None:
                    continue
                try:
                    callback(frame)
                except Exception:
                    log.exception("frame tap for view %s failed", name)

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

        from sprigcast.core import pacing

        if pacing.raise_thread_priority():  # wake on time even while the window or a game keeps the CPU busy
            log.info("render thread priority raised")
        self._sync_sources(compositor)
        self._warm_up(compositor)
        fps = max(c.fps for c in self._collection.canvases)
        interval = 1.0 / fps
        next_tick = time.perf_counter()
        render_times: deque[float] = deque(maxlen=fps)
        ticks, dropped, stats_at = 0, 0, time.perf_counter()
        last_problem = None
        readbacks_at = 0

        late_logged, late_held = float("-inf"), 0
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
                except Exception as exc:  # one bad frame must not end rendering for the whole session
                    problem = f"{type(exc).__name__}: {exc}"
                    if problem != last_problem:
                        last_problem = problem
                        log.exception("a frame failed to render")
                        self.events.put(EngineError(f"A frame failed to render: {problem}"))
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
            for canvas_id in list(self._transitions):
                self._finish_transition(canvas_id, compositor)
            for source in self._sources.values():
                source.stop()
            for source_id in list(self._watchers):
                self._close_watcher(compositor, source_id)
            if gpu is not None:
                gpu.release()
            compositor.release()

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
            if isinstance(command, ReplaceCollection):
                # A drag sends a new collection on every mouse move: only the newest one matters, so
                # skip straight to it instead of rebuilding sources for each (that backlog was the lag).
                while True:
                    try:
                        newer = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(newer, Shutdown):
                        return False
                    if isinstance(newer, ReplaceCollection):
                        command = newer
                went_on_air = self._went_on_air(self._collection, command.collection)
                self._start_transitions(self._collection, command.collection)
                self._start_state_fades(self._collection, command.collection)
                self._collection = command.collection
                for canvas in self._collection.canvases:
                    self.frames.setdefault(canvas.id, Mailbox())
                self._sync_sources(compositor)
                self._reload_on_air(went_on_air)

    @staticmethod
    def _on_air_sources(collection: SceneCollection) -> set[str]:
        """Sources in the blooms on air now, and in the sprigs inside them."""
        stack = [s for s in (collection.active_scene(c.id) for c in collection.canvases if c.enabled) if s]
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
        again): keep showing the old bloom until it has a picture
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
                  and (src.kind == "camera" or getattr(src, "slow_wake", False))}
        if asleep:
            self._waking[canvas_id] = (outgoing, time.perf_counter() + WAKE_WAIT_S, asleep)

    def _still_waking(self, compositor, canvas_id: str, now: float):
        """The scene to keep showing while a switch waits for a camera to open, or None to go ahead."""
        waiting = self._waking.get(canvas_id)
        if waiting is None:
            return None
        outgoing, give_up, ids = waiting
        if now < give_up and not all(sid in compositor.textures for sid in ids):
            return outgoing
        del self._waking[canvas_id]
        running = self._transitions.get(canvas_id)
        if running is not None and running.began is not None:
            running.began = now  # the transition starts now that the camera is there
        return None

    def _new_stinger(self, path: str, transition, collection: SceneCollection) -> StingerPlayer:
        width = max(c.width for c in collection.canvases)
        height = max(c.height for c in collection.canvases)
        matte = transition.stinger.matte
        limit = (width * (2 if matte == "right" else 1), height * (2 if matte == "below" else 1))
        sting = transition.stinger
        gain = 0.0 if sting.muted else 10 ** (sting.volume_db / 20)
        player = StingerPlayer(path, limit, gain)
        player.start()
        return player

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
            frame = running.stinger.frame_at(elapsed)
            if frame is not None:
                compositor.upload(running.stinger.id, frame)
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
                self._sources.pop(source_id).stop()
                self._last_shown.pop(source_id, None)
                self._camera_wait.pop(source_id, None)
                self._slept_at.pop(source_id, None)
                compositor.forget(source_id)
        for source_id, config in wanted.items():
            existing = self._sources.get(source_id)
            if existing is None and source_class(config.kind) is None and source_id in self._stopped_unknown:
                continue  # its plugin was removed: said so once already
            if existing is None:
                try:
                    source = create_source(config.kind, config.id, config.settings)
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
                existing.update(config.settings)
        self._sync_filters(compositor, wanted)
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
            out = Monitor(device)
            out.start()
            self.mixer.add_tap(out.write, ("stream",))
            self._sound_out = out

    @property
    def sound_out_status(self) -> str:
        out = getattr(self, "_sound_out", None)
        return out.status if out is not None else "off"

    def _sync_audio_options(self) -> None:
        """Ducking settings to the mixer; start monitoring when a source is monitored, stop when none is."""
        audio = self._collection.audio
        self.mixer.set_ducking(Ducking(
            enabled=audio.ducking, amount_db=audio.duck_amount_db, threshold_db=audio.duck_threshold_db,
            attack_ms=audio.duck_attack_ms, release_ms=audio.duck_release_ms, hold_ms=audio.duck_hold_ms,
        ))
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
        from sprigcast.core.filters import freeze_of, settings_for
        from sprigcast.plugin_hooks import frame_watchers

        effects = {}
        freezes = {}
        kinds = frame_watchers()
        watched = {}
        for source_id, source in self._sources.items():
            filters = wanted[source_id].filters
            flt = next((f for f in filters if f.enabled and f.kind in kinds), None) if kinds else None
            if flt is not None:
                watched[source_id] = (flt.kind, settings_for(flt.kind, flt.settings))
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
        compositor.effects = effects
        self._freeze_filters = freezes
        self._filtered = frozenset(effects)  # sources with video filters (a Move draws such a sprig whole)
        self._watched = watched
        for source_id in [s for s, w in self._watchers.items() if watched.get(s, ("",))[0] != w[1]]:
            self._close_watcher(compositor, source_id)  # its filter went (or the plugin was removed)

    def _feed_watcher(self, source_id: str, frame, channel_order: str, now: float) -> None:
        """Show a frame to the plugin watching this source (made on the first frame)."""
        kind, settings = self._watched[source_id]
        held = self._watchers.get(source_id)
        if held is None:
            from sprigcast.plugin_hooks import frame_watchers

            factory = frame_watchers().get(kind)
            if factory is None:
                return
            try:
                held = self._watchers[source_id] = [factory(source_id), kind, now]
            except Exception:
                log.exception("a plugin's frame watcher for %s failed to start", source_id)
                self._watched.pop(source_id, None)
                return
        held[2] = now
        try:
            held[0].offer(frame, channel_order, settings)
        except Exception:
            log.exception("a plugin's frame watcher for %s failed", source_id)
            self._close_watcher(None, source_id)
            self._watched.pop(source_id, None)

    def _serve_watchers(self, compositor, now: float) -> None:
        """Upload what the watchers finished, and close the ones nothing has fed for WATCHER_PARK_S."""
        for source_id, held in list(self._watchers.items()):
            if now - held[2] > WATCHER_PARK_S:
                self._close_watcher(compositor, source_id)
                continue
            try:
                result = held[0].take()
                if result is not None and result.get("mask") is not None:
                    compositor.upload_cutout(source_id, result["mask"], result.get("backdrop"))
            except Exception:
                log.exception("a plugin's frame watcher for %s failed", source_id)
                self._close_watcher(compositor, source_id)
                self._watched.pop(source_id, None)

    def _close_watcher(self, compositor, source_id: str) -> None:
        held = self._watchers.pop(source_id, None)
        if held is not None:
            try:
                held[0].close()
            except Exception:
                log.exception("a plugin's frame watcher for %s didn't close", source_id)
        forget = getattr(compositor, "forget_cutout", None)
        if forget is not None:
            forget(source_id)

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

    def _show_hidden(self) -> None:
        """Tell each awake source when its eye goes off or on (a video pauses, then starts over or carries on)."""
        visible = self._visible = self._visible_sources()
        told = self._told_hidden
        for source_id, source in self._sources.items():
            if source.asleep:
                told.pop(source_id, None)  # said again once it wakes
                continue
            hidden = source_id not in visible
            if told.get(source_id) != hidden:
                told[source_id] = hidden
                tell = getattr(source, "set_hidden", None)
                try:
                    if tell is not None:
                        tell(hidden)
                except Exception as exc:  # a plugin's source: never stop the render loop for it
                    log.warning("source %s set_hidden failed: %s", source_id, exc)
        for source_id in [s for s in told if s not in self._sources]:
            del told[source_id]

    def _rest_sources(self, compositor, now: float) -> None:
        """Put sources nothing has shown for a while to sleep, and wake the ones that are shown again.

        A sleeping capture or camera stops and frees its frames, a clock stops redrawing, and every
        sleeper's texture is dropped, so scenes you aren't using cost neither memory nor CPU. The
        short wait keeps flicking between two scenes from stopping and starting things each time.
        """
        shown = self._shown_sources()
        for source_id, source in self._sources.items():
            if source_id in shown or not self._may_sleep(source):
                self._last_shown[source_id] = now
                if source.asleep:
                    source.asleep = False
                    if source.kind == "camera":
                        self._camera_back(source_id, now)
                    source.wake()
            elif not source.asleep and now - self._last_shown.setdefault(source_id, now) >= (
                    self._camera_wait.get(source_id, CAMERA_SLEEP_AFTER) if source.kind == "camera" else SLEEP_AFTER):
                source.asleep = True
                if source.kind == "camera":
                    self._slept_at[source_id] = now
                source.sleep()
                compositor.forget(source_id)

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

    def _drawn_scenes(self, collection: SceneCollection) -> list:
        drawn = []
        for canvas in collection.canvases:
            if not canvas.enabled:
                continue
            for scene_id in (collection.active_scenes.get(canvas.id), self.staged.get(canvas.id)):
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

    def _move_frame(self, running: Running, canvas, progress: float, hidden: frozenset = frozenset()) -> list:
        """This frame of a Move transition on a canvas, as pieces to draw (see compositor.render)."""
        sizes = {source_id: source.size for source_id, source in self._sources.items()}
        new = self._collection.active_scene(canvas.id)
        old = self._move_pieces(running.outgoing, sizes, hidden)
        now = self._move_pieces(new, sizes, hidden) if new is not None else []
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
        stack = self._move_light(collection, self._drawn_scenes(collection))
        if self.editing_part:
            stack += [p for p in collection.part_scenes() if p.id == self.editing_part]
            used.add(self.editing_part)
        while stack:
            for inner in collection.inner_shapes(stack.pop()):
                if inner.id not in used:
                    used.add(inner.id)
                    stack.append(inner)
        textures = {}
        for part in collection.part_order():
            if part.id not in used:
                continue
            if any(collection.contains(inner, part.id) for inner in collection.parts_inside(part)):
                continue  # a part inside itself: never drawn
            canvas = next((c for c in collection.canvases if c.id == part.canvas), None)
            if canvas is None:
                continue
            compositor.parts = self._part_sources(textures, compositor)
            size = (canvas.width, canvas.height)
            if part.id == self.editing_part:
                view, fades = part, None  # being edited: show every item, whatever its Live state
            else:
                view, fades = self._state_view(collection, part)
            textures[part.id] = compositor.render_part(part.id, size, view, sizes, fades)
            if part.id == self.editing_part and self._preview_due("part", now):
                self.readbacks += 1
                data, drawn = self._read_full(compositor, f"part:{part.id}", now)
                if data is not None:
                    self.part_frames.put(Frame(f"part:{part.id}", canvas.width, canvas.height, data, drawn))
            # Placements of this layout with their own tweaks (hidden items) get their own copy.
            main = collection.main_part(part)
            for source in collection.sources:
                if collection.part_of(source) is main and collection.hidden_here(source):
                    tweaked = collection.placement_view(source, view)
                    textures[(source.id, part.id)] = compositor.render_part(
                        f"{part.id}~{source.id}", size, tweaked, sizes, fades)
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
            frame = self._frame(compositor, target, nv12_size, now, frames)
            if frame is None:
                continue
            try:
                callback(frame)
            except Exception:
                log.exception("frame tap for %s failed", target.id)

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
            data, drawn = self._read_full(compositor, target_id, now)
            return None if data is None else Frame(target_id, *canvas_size, data, drawn)
        if self.wait_free:
            data, drawn = compositor.read_scaled_later(target_id, size, now, now, optional=True)
            return None if data is None else Frame(target_id, size[0], size[1], data, drawn, canvas_size=canvas_size)
        return Frame(target_id, size[0], size[1], compositor.read_scaled(target_id, size), now, canvas_size=canvas_size)

    def _preview_due(self, key: str, now: float) -> bool:
        """True when the UI shows this preview and its last copy is at least 1/PREVIEW_FPS old."""
        wanted = self.previews
        if wanted is not None and key not in wanted:
            return False
        if now - self._preview_at.get(key, -1.0) < 1.0 / PREVIEW_FPS - 0.002:
            return False
        self._preview_at[key] = now
        return True

    def _part_sources(self, textures: dict, compositor=None) -> dict:
        """(placement source id, canvas id) -> texture of the part's layout for that canvas. A frozen placement
        gets the copy made the first time this tick's picture of it was ready (see _freezes)."""
        collection = self._collection
        out = {}
        frozen = self._frozen
        for source in collection.sources:
            part = collection.part_of(source)
            if part is None:
                continue
            freeze = frozen.get(source.id) if frozen and getattr(compositor, "held", None) is not None else None
            for canvas in collection.canvases:
                shape = collection.shape_for(part, canvas.id)
                texture = textures.get((source.id, shape.id), textures.get(shape.id))
                if texture is None:
                    continue
                if freeze is not None:
                    held = compositor.held.get((source.id, canvas.id))
                    if held is not None and held[0] == freeze:
                        texture = held[1]
                    else:
                        texture = compositor.hold_copy((source.id, canvas.id), freeze, texture)
                out[(source.id, canvas.id)] = texture
        return out

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
        avg = self._source_ms.get(source_id, 0.0) * 0.9 + 1000 * spent * 0.1
        self._source_ms[source_id] = avg
        every = min(4, max(1, math.ceil(avg / SOURCE_BUDGET_MS))) if avg > SOURCE_BUDGET_MS else 1
        before = self._source_every.get(source_id, 1)
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
        self._marks.append((name, time.perf_counter()))

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
        return f" ({text})" if text else ""

    def _tick(self, compositor, now: float) -> None:
        self._marks = [("start", time.perf_counter())]
        compositor.time = now
        if self._release_later:
            self._release_unused(compositor)
        if now - self._pruned_at > 1.0:  # a recording or source record that ended leaves its targets idle
            self._pruned_at = now
            prune = getattr(compositor, "prune", None)  # test stand-ins may not have it
            if prune is not None:
                prune()
        self._rest_sources(compositor, now)
        self._show_hidden()
        self._mark("waking and pausing")
        slowest = ("", 0.0)
        self._ticks = getattr(self, "_ticks", 0) + 1
        frozen = self._freezes(compositor)
        textures = getattr(compositor, "textures", {})
        for source_id, source in self._sources.items():
            freeze = frozen.get(source_id) if frozen else None
            if source.asleep or not self._budget_allows(source_id):
                if freeze is not None and source_id in textures:
                    self._caught.setdefault(source_id, freeze)  # its picture now is the frozen one, as on screen
                elif source.asleep and self._missed:
                    self._missed.pop(source_id, None)
                continue
            began = time.perf_counter()
            missed = self._missed.pop(source_id, None) if self._missed and freeze is None else None
            frame = source.video_frame()
            if freeze is not None and self._caught.get(source_id) == freeze and source_id in textures:
                if frame is not None:  # frozen: it keeps the picture it had (the source runs on, unseen)...
                    self._missed[source_id] = frame  # ...and shows its newest once it's let go: a text or
                frame = None  # Death count only sends a picture when it changes
            elif freeze is not None:
                self._caught[source_id] = freeze  # this frame (or the one it has, if none came) is the frozen one
            if frame is None and missed is not None:
                frame = missed
            if frame is not None:
                compositor.upload(source_id, frame, source.channel_order)
                if source_id in self._watched and source_id in self._visible:
                    self._feed_watcher(source_id, frame, source.channel_order, now)
            spent = time.perf_counter() - began
            self._charge(source_id, source, spent)
            if spent > slowest[1]:
                slowest = (source_id, spent)
        self._slowest_source = slowest
        if self._watchers:
            self._serve_watchers(compositor, now)
        self._mark("sources")
        sizes = {source_id: source.size for source_id, source in self._sources.items()}
        if self._collection.parts():
            self._render_parts(compositor, sizes, now)
        else:
            self._part_textures, self._parts_used = {}, set()
            if compositor.parts:
                compositor.parts = {}
        self._mark("sprigs")
        with self._taps_lock:
            taps = {canvas_id: list(callbacks) for canvas_id, callbacks in self._taps.items() if callbacks}
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
            self._render_staged(compositor, canvas, sizes, now)
            self._render_clean(compositor, canvas, sizes, now, taps.get(f"clean:{canvas.id}", ()))
            if not outputs and not preview:
                continue  # nobody is watching or recording this canvas right now
            self._mark("canvases")  # the late-frame line splits the canvases into these steps
            compositor.render(canvas, held or self._collection.active_scene(canvas.id), sizes, **extra)
            self._mark(f"{canvas.id} drawn")
            frames: dict = {}  # nv12 size (None = RGBA) -> Frame, so outputs of one size share one readback
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
                frame = self._frame(compositor, canvas, nv12_size, now, frames)
                self._mark(f"{canvas.id} read back")
                if frame is None:
                    continue  # the first tick of a wait-free copy: its frame arrives next tick
                try:
                    callback(frame)
                except Exception:
                    log.exception("frame tap for %s failed", canvas.id)
                self._mark(f"{canvas.id} {getattr(callback, '__qualname__', 'output')}")
        self._mark("canvases")
        self._render_views(compositor, sizes, now, taps)
        self._mark("virtual camera")
        if self._grabs:
            self._serve_grabs(compositor, sizes)
            self._mark("snapshots")


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
