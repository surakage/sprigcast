"""The native engine drawing SprigCast's canvases for the screens (docs/native-engine.md, phase 2).

With ``SPRIGCAST_ENGINE=native`` the render thread hands every source's new picture to the native engine as well,
and a frozen snapshot of what is on air whenever that changes. The native engine draws the canvases on its own
thread and copies small pictures back for the previews without waiting on the graphics card, so the Python render
thread no longer draws a canvas just to show it.

Python's compositor still draws whatever the native engine can't do yet: recordings and streams (until encoders
move in), transitions, Build's edited bloom, screenshots and the virtual camera. If the native engine faults for
good, this steps aside and Python's compositor draws the previews again, as before.
"""

from __future__ import annotations

import logging
import time

from sprigcast import sprigcore
from sprigcast.core.scene import Scene, SceneCollection
from sprigcast.render import native_snapshot
from sprigcast.render.native_snapshot import SceneDraw

log = logging.getLogger(__name__)

STILL_AFTER_S = 1.0  # a source that sent nothing for this long is treated as a still picture (kept for rebuilds)
STATS_EVERY_S = 1.0


def canvas_key(canvas_id: str) -> str:
    return f"canvas:{canvas_id}"


def part_key(shape_id: str) -> str:
    return f"part:{shape_id}"


def snapshot_draws(collection: SceneCollection, state_view) -> tuple[list[SceneDraw], dict, dict[str, str | None]]:
    """What the native engine draws: each enabled canvas's bloom on air and every sprig layout (the engine draws
    only those something on air places). state_view(collection, layout) gives a sprig as its Live state shows
    it: (layout, fades). Returns the scenes, the sprig placements ((source id, scene key) -> sprig key) and
    canvas id -> the scene key it shows (None: nothing on air)."""
    sources = {s.id: s for s in collection.sources}
    draws: list[SceneDraw] = []
    parts: dict[tuple[str, str], str] = {}
    shows: dict[str, str | None] = {}

    def place(scene: Scene, key: str, canvas_id: str) -> None:
        for item in scene.items:
            source = sources.get(item.source)
            part = collection.part_of(source) if source is not None else None
            if part is not None:
                parts[(item.source, key)] = part_key(collection.shape_for(part, canvas_id).id)

    sizes = {c.id: (c.width, c.height) for c in collection.canvases}
    for canvas in collection.canvases:
        if not canvas.enabled:
            continue
        scene = collection.active_scene(canvas.id)
        if scene is None:
            shows[canvas.id] = None
            continue
        key = canvas_key(canvas.id)
        draws.append(SceneDraw(key, scene, sizes[canvas.id]))
        place(scene, key, canvas.id)
        shows[canvas.id] = key
    for layout in collection.part_scenes():
        size = sizes.get(layout.canvas)
        if size is None:
            continue
        view, fades = state_view(collection, layout)
        key = part_key(layout.id)
        draws.append(SceneDraw(key, view, size, fades))
        place(view, key, layout.canvas)
    return draws, parts, shows


class NativeCanvases:
    """The native engine as the render thread uses it. Every call is safe: a failure turns it off (``failed``)
    and the Python compositor carries on alone."""

    def __init__(self, collection: SceneCollection, warp: bool = False):
        fps = max((c.fps for c in collection.canvases), default=60)
        self.engine = sprigcore.Engine(warp=warp, fps=(int(fps), 1))
        self.resources = native_snapshot.Resources()
        self.failed = False
        self._slots: dict[str, int] = {}
        self._canvas_sizes: dict[int, tuple[int, int]] = {}
        self._shown: dict[int, str | None] = {}
        self._watching: dict[int, tuple[int, int, int]] = {}
        self._signature = None
        self._last_upload: dict[str, float] = {}
        self._stats_at = 0.0
        self._rebuilds = 0
        log.info("native engine: drawing the previews (%s)", "software" if warp else "graphics card")

    # ---- what goes in ------------------------------------------------------------------------------------------

    def _guard(self, what: str, call, *args, **kwargs):
        if self.failed:
            return None
        try:
            return call(*args, **kwargs)
        except sprigcore.NativeError as exc:
            self.fail(f"{what}: {exc}")
        except Exception:  # anything here only turns the native engine off
            log.exception("native engine: %s failed", what)
            self.fail(f"{what} failed")
        return None

    def fail(self, why: str) -> None:
        if not self.failed:
            self.failed = True
            log.error("native engine: %s; the Python compositor draws the previews again", why)

    def upload(self, source_id: str, frame, channel_order: str, size) -> None:
        """A source's new picture. The first picture after a quiet spell is kept by the engine (a still picture
        sends once, and must come back by itself if the engine rebuilds); a stream of pictures isn't."""
        now = time.perf_counter()
        last = self._last_upload.get(source_id)
        self._last_upload[source_id] = now
        keep = last is None or now - last > STILL_AFTER_S
        self._guard("upload", self.engine.upload, source_id, frame, channel_order, size, keep=keep)

    def forget_gone(self, source_ids) -> None:
        for gone in set(self._last_upload) - set(source_ids):
            del self._last_upload[gone]
            self._guard("forget", self.engine.forget, gone)

    def sync(self, collection: SceneCollection, effects: dict, state_view, fading: bool) -> None:
        """Hand over a new snapshot when what's on air, the filters or a sprig's state changed (every frame
        while a sprig's state fades)."""
        if self.failed:
            return
        signature = (id(collection), id(effects),
                     tuple((c.id, c.enabled, c.width, c.height, collection.active_scenes.get(c.id))
                           for c in collection.canvases))
        if signature == self._signature and not fading:
            return
        self._signature = signature
        draws, parts, shows = snapshot_draws(collection, state_view)
        built = native_snapshot.build(draws, effects, parts)
        self._guard("snapshot", native_snapshot.hand_over, self.engine, built, self.resources)
        for canvas in collection.canvases:
            slot = self._slot(canvas.id)
            if slot is None:
                continue
            size = (canvas.width, canvas.height) if canvas.enabled else (0, 0)
            if self._canvas_sizes.get(slot) != size:
                self._canvas_sizes[slot] = size
                self._guard("canvas", self.engine.set_canvas, slot, *size)
            key = shows.get(canvas.id)
            if self._shown.get(slot, "") != key:
                self._shown[slot] = key
                self._guard("show", self.engine.show, slot, key)

    def _slot(self, canvas_id: str) -> int | None:
        slot = self._slots.get(canvas_id)
        if slot is None and len(self._slots) < sprigcore.ENGINE_CANVASES:
            slot = self._slots[canvas_id] = len(self._slots)
        return slot

    # ---- what comes out ----------------------------------------------------------------------------------------

    def draws(self, canvas_id: str) -> bool:
        """True when this canvas's preview comes from the native engine."""
        return not self.failed and self._slots.get(canvas_id) is not None and \
            self._shown.get(self._slots[canvas_id]) is not None

    def watch(self, canvas_id: str, size: tuple[int, int] | None, fps: int) -> None:
        """Copy this canvas back at `size` (None: stop) at most `fps` times a second."""
        slot = self._slots.get(canvas_id)
        if slot is None or self.failed:
            return
        want = (size[0], size[1], fps) if size else (0, 0, 0)
        if self._watching.get(slot, (0, 0, 0)) != want:
            self._watching[slot] = want
            self._guard("watch", self.engine.watch, slot, *want)

    def take(self, canvas_id: str) -> tuple[bytes, int, int] | None:
        """The newest picture of a watched canvas: (RGBA premultiplied, width, height), or None."""
        slot = self._slots.get(canvas_id)
        if slot is None or self.failed:
            return None
        w, h, _fps = self._watching.get(slot, (0, 0, 0))
        if not w:
            return None
        got = self._guard("take", self.engine.take, slot, w, h)
        return (got[0], w, h) if got else None

    def check(self, now: float) -> None:
        """About once a second: the engine's health. A faulted engine turns this off."""
        if self.failed or now - self._stats_at < STATS_EVERY_S:
            return
        self._stats_at = now
        stats = self._guard("stats", self.engine.stats)
        if stats is None:
            return
        if stats["rebuilds"] != self._rebuilds:
            self._rebuilds = stats["rebuilds"]
            log.warning("native engine: rebuilt its device (%d so far)", self._rebuilds)
        if stats["faulted"]:
            self.fail("it stopped after repeated faults")
        elif stats["hung"]:
            log.warning("native engine: a frame is stuck")

    def close(self) -> None:
        try:
            self.engine.close()
        except Exception:
            log.exception("native engine: closing failed")
