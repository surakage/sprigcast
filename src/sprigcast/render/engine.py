"""The render thread: paces ticks, applies UI commands, draws every enabled canvas."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable

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
from sprigcast.core.filters import AudioChain, video_effects
from sprigcast.core.mailbox import Mailbox
from sprigcast.core.scene import PART_KIND, STATE_FADE_S, SceneCollection
from sprigcast.core.source import Source, create_source

log = logging.getLogger(__name__)


PREVIEW_FPS = 30  # the UI draws previews about 30 times a second; copying more is wasted


class Frame:
    """One finished canvas image: RGBA bytes, top row first."""

    __slots__ = ("canvas_id", "width", "height", "data", "time")

    def __init__(self, canvas_id: str, width: int, height: int, data: bytes, time: float = 0.0):
        self.canvas_id, self.width, self.height, self.data = canvas_id, width, height, data
        self.time = time  # perf_counter() at the start of the tick that drew it


class Engine:
    def __init__(self, collection: SceneCollection):
        self.commands: queue.Queue = queue.Queue()  # UI -> engine
        self.events: queue.Queue = queue.Queue()  # engine -> UI
        self.frames: dict[str, Mailbox[Frame]] = {c.id: Mailbox() for c in collection.canvases}
        self.part_frames: Mailbox[Frame] = Mailbox()  # the part open in the Part editor, if any
        self.editing_part: str | None = None  # set by the UI: which part to read back for the Part editor
        self._collection = collection.model_copy(deep=True)
        self._sources: dict[str, Source] = {}
        self._thread = threading.Thread(target=self._run, name="sprigcast-render", daemon=True)
        self.mixer = Mixer(report=lambda levels: self.events.put(AudioLevels(levels)))
        self.monitor = Monitor()  # plays monitored sources on the speakers, opened only when one is
        self._monitoring = False
        self._taps: dict[str, list[Callable[[Frame], None]]] = {}  # canvas id -> recorders, later streams
        self._taps_lock = threading.Lock()
        self._transitions: dict[str, tuple] = {}  # canvas id -> (outgoing scene, start time, duration s)
        self._state_fades: dict[str, tuple] = {}
        # Studio mode: canvas id -> the scene lined up to go on air next. Rendered to "next:<canvas>".
        # The UI replaces the whole dict, so the render thread always sees a consistent one.
        self.staged: dict[str, str] = {}  # sprig id -> (start time, hidden before, hidden after)
        # Which previews the UI is showing ("horizontal", "next:horizontal", "part"); None = all of them.
        # A canvas nobody watches and nothing records is not drawn or copied back at all; a watched one
        # is copied back at most PREVIEW_FPS times a second. The UI replaces the whole set.
        self.previews: frozenset[str] | None = None
        self._preview_at: dict[str, float] = {}
        self.readbacks = 0  # canvases copied from the GPU, for Stats

    # Called from the UI thread ----------------------------------------------

    def start(self) -> None:
        self.mixer.start()
        self._thread.start()

    def send(self, command) -> None:
        self.commands.put(command)

    def add_frame_tap(self, canvas_id: str, callback: Callable[[Frame], None]) -> None:
        """Hand every finished frame of a canvas to callback, on the render thread. It must not block."""
        with self._taps_lock:
            self._taps.setdefault(canvas_id, []).append(callback)

    def remove_frame_tap(self, canvas_id: str, callback: Callable[[Frame], None]) -> None:
        with self._taps_lock:
            taps = self._taps.get(canvas_id, [])
            if callback in taps:
                taps.remove(callback)

    def stop(self, timeout: float = 2.0) -> None:
        self.commands.put(Shutdown())
        self._thread.join(timeout)
        self.mixer.stop()
        self.monitor.stop()

    # Render thread -------------------------------------------------------------

    def _run(self) -> None:
        from sprigcast.render.compositor import (
            Compositor,  # GL context must live on this thread
        )

        try:
            compositor = Compositor()
        except Exception as exc:  # no GPU driver, remote desktop without GL, ...
            self.events.put(EngineError(f"Could not start the GPU compositor: {exc}"))
            return

        self._sync_sources(compositor)
        fps = max(c.fps for c in self._collection.canvases)
        interval = 1.0 / fps
        next_tick = time.perf_counter()
        render_times: deque[float] = deque(maxlen=fps)
        ticks, dropped, stats_at = 0, 0, time.perf_counter()
        readbacks_at = 0

        try:
            while True:
                if not self._drain_commands(compositor):
                    break
                started = time.perf_counter()
                self._tick(compositor, started)
                render_times.append(time.perf_counter() - started)
                ticks += 1

                next_tick += interval
                now = time.perf_counter()
                if now > next_tick:
                    # Running behind: count it and resynchronise instead of rendering a burst to catch up.
                    dropped += 1
                    next_tick = now
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
            for source in self._sources.values():
                source.stop()
            compositor.release()

    def _drain_commands(self, compositor) -> bool:
        while True:
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                return True
            if isinstance(command, Shutdown):
                return False
            if isinstance(command, ReplaceCollection):
                self._start_transitions(self._collection, command.collection)
                self._start_state_fades(self._collection, command.collection)
                self._collection = command.collection
                for canvas in self._collection.canvases:
                    self.frames.setdefault(canvas.id, Mailbox())
                self._sync_sources(compositor)

    def _start_transitions(self, old: SceneCollection, new: SceneCollection) -> None:
        """When a canvas's active scene changes, fade from the scene it showed until now."""
        transition = new.transition
        for canvas in new.canvases:
            before, after = old.active_scenes.get(canvas.id), new.active_scenes.get(canvas.id)
            if before == after or before is None:
                continue
            if transition.kind != "fade" or transition.duration_ms <= 0:
                self._transitions.pop(canvas.id, None)
                continue
            outgoing = next((s for s in old.scenes if s.id == before), None)
            if outgoing is not None:
                self._transitions[canvas.id] = (outgoing, time.perf_counter(), transition.duration_ms / 1000)

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
        wanted = {s.id: s for s in self._collection.sources}
        for source_id in list(self._sources):
            if source_id not in wanted or self._sources[source_id].kind != wanted[source_id].kind:
                self._sources.pop(source_id).stop()
                compositor.forget(source_id)
        for source_id, config in wanted.items():
            existing = self._sources.get(source_id)
            if existing is None:
                try:
                    source = create_source(config.kind, config.id, config.settings)
                    source.start()
                except Exception as exc:
                    log.warning("source %s (%s) failed to start: %s", config.name, config.kind, exc)
                    self.events.put(EngineError(f"Source {config.name!r} failed to start: {exc}"))
                    continue
                self._sources[source_id] = source
            elif existing.settings != config.settings:
                existing.update(config.settings)
        self._sync_filters(compositor, wanted)
        self._sync_parts(compositor)
        self.mixer.set_sources(list(self._sources.values()))
        self._sync_audio_options()

    def _sync_audio_options(self) -> None:
        """Ducking settings to the mixer; start monitoring when a source is monitored, stop when none is."""
        audio = self._collection.audio
        self.mixer.set_ducking(Ducking(
            enabled=audio.ducking, amount_db=audio.duck_amount_db, threshold_db=audio.duck_threshold_db,
            attack_ms=audio.duck_attack_ms, release_ms=audio.duck_release_ms, hold_ms=audio.duck_hold_ms,
        ))
        monitoring = any(
            s.settings.get("monitor") and not s.settings.get("muted") for s in self._collection.sources
            if s.kind in ("audio_input", "audio_output")
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
        effects = {}
        for source_id, source in self._sources.items():
            filters = wanted[source_id].filters
            fx = video_effects(filters)
            if fx.active:
                effects[source_id] = fx
            chain = getattr(source, "audio_chain", None)
            if chain is not None and (chain.matches(filters) or chain.update(filters)):
                continue
            new_chain = AudioChain(filters)
            source.audio_chain = new_chain if new_chain.processors else None
        compositor.effects = effects

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

    def _render_parts(self, compositor, sizes: dict[str, tuple[int, int]], now: float) -> None:
        """Render every part layout in use once (inner ones first), then point each placement at its texture."""
        collection = self._collection
        # Only layouts that something on screen (or the Part editor) shows, however deep.
        used: set[str] = set()
        stack = [s for s in collection.scenes if s.kind == "show"]
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
            compositor.parts = self._part_sources(textures)
            size = (canvas.width, canvas.height)
            if part.id == self.editing_part:
                view, fades = part, None  # being edited: show every item, whatever its Live state
            else:
                view, fades = self._state_view(collection, part)
            textures[part.id] = compositor.render_part(part.id, size, view, sizes, fades)
            if part.id == self.editing_part and self._preview_due("part", now):
                self.readbacks += 1
                data = compositor.read(f"part:{part.id}")
                self.part_frames.put(Frame(f"part:{part.id}", canvas.width, canvas.height, data, now))
            # Placements of this layout with their own tweaks (hidden items) get their own copy.
            main = collection.main_part(part)
            for source in collection.sources:
                if collection.part_of(source) is main and collection.hidden_here(source):
                    tweaked = collection.placement_view(source, view)
                    textures[(source.id, part.id)] = compositor.render_part(
                        f"{part.id}~{source.id}", size, tweaked, sizes, fades)
        compositor.parts = self._part_sources(textures)

    def _render_staged(self, compositor, canvas, sizes, now: float) -> None:
        """Studio mode: draw the scene lined up next on this canvas, for the Next preview (not for outputs)."""
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
        mailbox.put(Frame(target.id, canvas.width, canvas.height, compositor.read(target.id), now))
        self.readbacks += 1

    def _preview_due(self, key: str, now: float) -> bool:
        """True when the UI shows this preview and its last copy is at least 1/PREVIEW_FPS old."""
        wanted = self.previews
        if wanted is not None and key not in wanted:
            return False
        if now - self._preview_at.get(key, -1.0) < 1.0 / PREVIEW_FPS - 0.002:
            return False
        self._preview_at[key] = now
        return True

    def _part_sources(self, textures: dict) -> dict:
        """(placement source id, canvas id) -> texture of the part's layout for that canvas."""
        collection = self._collection
        out = {}
        for source in collection.sources:
            part = collection.part_of(source)
            if part is None:
                continue
            for canvas in collection.canvases:
                shape = collection.shape_for(part, canvas.id)
                texture = textures.get((source.id, shape.id), textures.get(shape.id))
                if texture is not None:
                    out[(source.id, canvas.id)] = texture
        return out

    def _tick(self, compositor, now: float) -> None:
        compositor.time = now
        for source_id, source in self._sources.items():
            frame = source.video_frame()
            if frame is not None:
                compositor.upload(source_id, frame, source.channel_order)
        sizes = {source_id: source.size for source_id, source in self._sources.items()}
        if self._collection.parts():
            self._render_parts(compositor, sizes, now)
        elif compositor.parts:
            compositor.parts = {}
        with self._taps_lock:
            taps = {canvas_id: list(callbacks) for canvas_id, callbacks in self._taps.items() if callbacks}
        for canvas in self._collection.canvases:
            if not canvas.enabled:
                continue
            previous, progress = None, 1.0
            transition = self._transitions.get(canvas.id)
            if transition is not None:
                outgoing, began, duration = transition
                progress = (now - began) / duration
                if progress >= 1.0:
                    del self._transitions[canvas.id]
                else:
                    previous, progress = outgoing, max(0.0, progress)
            outputs = taps.get(canvas.id, ())
            preview = self._preview_due(canvas.id, now)
            self._render_staged(compositor, canvas, sizes, now)
            if not outputs and not preview:
                continue  # nobody is watching or recording this canvas right now
            compositor.render(canvas, self._collection.active_scene(canvas.id), sizes, previous, progress)
            data = compositor.read(canvas.id)
            self.readbacks += 1
            frame = Frame(canvas.id, canvas.width, canvas.height, data, now)
            if preview:
                self.frames[canvas.id].put(frame)
            for callback in outputs:
                try:
                    callback(frame)
                except Exception:
                    log.exception("frame tap for %s failed", canvas.id)
