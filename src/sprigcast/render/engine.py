"""The render thread: paces ticks, applies UI commands, draws every enabled canvas."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable

from sprigcast.audio.mixer import Mixer
from sprigcast.core.events import AudioLevels, EngineError, ReplaceCollection, Shutdown, SourceInfo, Stats
from sprigcast.core.filters import AudioChain, video_effects
from sprigcast.core.mailbox import Mailbox
from sprigcast.core.scene import PART_KIND, SceneCollection
from sprigcast.core.source import Source, create_source

log = logging.getLogger(__name__)


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
        self._taps: dict[str, list[Callable[[Frame], None]]] = {}  # canvas id -> recorders, later streams
        self._taps_lock = threading.Lock()
        self._transitions: dict[str, tuple] = {}  # canvas id -> (outgoing scene, start time, duration s)

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

    # Render thread -------------------------------------------------------------

    def _run(self) -> None:
        from sprigcast.render.compositor import Compositor  # GL context must live on this thread

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
                    time.sleep(max(0.0, next_tick - now - 0.001))
                    while time.perf_counter() < next_tick:  # spin the last millisecond for accurate pacing
                        pass

                if now - stats_at >= 1.0:
                    avg_ms = 1000 * sum(render_times) / max(1, len(render_times))
                    self.events.put(Stats(fps=ticks / (now - stats_at), render_ms=avg_ms, dropped_ticks=dropped))
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
            textures[part.id] = compositor.render_part(part.id, (canvas.width, canvas.height), part, sizes)
            if part.id == self.editing_part:
                data = compositor.read(f"part:{part.id}")
                self.part_frames.put(Frame(f"part:{part.id}", canvas.width, canvas.height, data, now))
        compositor.parts = self._part_sources(textures)

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
                if shape.id in textures:
                    out[(source.id, canvas.id)] = textures[shape.id]
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
            compositor.render(canvas, self._collection.active_scene(canvas.id), sizes, previous, progress)
            data = compositor.read(canvas.id)
            frame = Frame(canvas.id, canvas.width, canvas.height, data, now)
            self.frames[canvas.id].put(frame)
            for callback in taps.get(canvas.id, ()):
                try:
                    callback(frame)
                except Exception:
                    log.exception("frame tap for %s failed", canvas.id)
