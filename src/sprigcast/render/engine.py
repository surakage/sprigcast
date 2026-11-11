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
from sprigcast.core.mailbox import Mailbox
from sprigcast.core.scene import SceneCollection
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
        self.mixer.set_sources(list(self._sources.values()))

    def _tick(self, compositor, now: float) -> None:
        for source_id, source in self._sources.items():
            frame = source.video_frame()
            if frame is not None:
                compositor.upload(source_id, frame, source.channel_order)
        sizes = {source_id: source.size for source_id, source in self._sources.items()}
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
