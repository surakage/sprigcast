"""The native engine drawing SprigCast's canvases for the screens (docs/native-engine.md, phase 2).

With ``SPRIGCAST_ENGINE=native`` the render thread hands every source's new picture to the native engine as well,
and a frozen snapshot of what is on air whenever that changes. The native engine draws the canvases on its own
thread and copies small pictures back for the previews without waiting on the graphics card, so the Python render
thread no longer draws a canvas just to show it.

Transitions (fade, slide, swipe, wipe, leaf and bloom) run in the native engine on its own clock; the outgoing
bloom is in the snapshot while one runs. Recordings and streams take NV12 frames the native engine makes on its
own thread (up to ENGINE_OUTPUTS sizes a canvas), timed by when it drew them. An output whose encoder is
SprigCast's own NVENC, opened on the native engine's device (encoder_device), gets its frames as NV12 textures
on the card (CardFrame): nothing is copied back at all. Python's compositor still draws
whatever the native engine can't do yet: stinger and move transitions, Build's edited bloom, screenshots, source
record and the virtual camera, and the outputs of a canvas while it draws one of those. If the native engine faults for
good, this steps aside and Python's compositor draws the previews again, as before.
"""

from __future__ import annotations

import logging
import os
import time

from sprigcast import sprigcore
from sprigcast.core.scene import Scene, SceneCollection
from sprigcast.render import native_snapshot
from sprigcast.render.native_snapshot import SceneDraw

log = logging.getLogger(__name__)

STILL_AFTER_S = 1.0  # a source that sent nothing for this long is treated as a still picture (kept for rebuilds)
STATS_EVERY_S = 1.0


FAULT_KINDS = {1: "a C++ exception", 2: "an access violation", 3: "a lost graphics card", 4: "a frame stuck for 3 s"}


def planned_faults(text: str | None) -> list[tuple[float, int]]:
    """SPRIGCAST_ENGINE_FAULT, for testing the crash gates on a real PC (never set in normal use):
    "kind@seconds" items separated by commas, each optionally "xN" to repeat it N times a second apart. Kinds
    as sprigcore.Engine.debug_fault: 1 exception, 2 access violation, 3 lost card, 4 stuck frame. "3@60" loses
    the card a minute in (the engine rebuilds); "1@60x7" faults seven times in a row (more than five a minute:
    the engine stops and Python's compositor takes over). Returns (seconds after start, kind), soonest first;
    anything that doesn't read as that is left out."""
    plan = []
    for item in (text or "").split(","):
        try:
            kind, _, rest = item.strip().partition("@")
            when, _, times = rest.partition("x")
            kind, when, times = int(kind), float(when), int(times or 1)
        except ValueError:
            continue
        if kind in FAULT_KINDS and when >= 0:
            plan.extend((when + n, kind) for n in range(max(1, min(times, 20))))
    return sorted(plan)


def canvas_key(canvas_id: str) -> str:
    return f"canvas:{canvas_id}"


def part_key(shape_id: str) -> str:
    return f"part:{shape_id}"


def from_key(canvas_id: str) -> str:
    return f"from:{canvas_id}"


def native_transition(running) -> bool:
    """Whether the native engine draws this running transition (render/engine.py Running)."""
    return running is not None and running.began is not None and running.stinger is None and \
        running.settings.kind in sprigcore.TRANSITION_MODES


def snapshot_draws(collection: SceneCollection, state_view, outgoing: dict[str, Scene] | None = None
                   ) -> tuple[list[SceneDraw], dict, dict[str, str | None]]:
    """What the native engine draws: each enabled canvas's bloom on air and every sprig layout (the engine draws
    only those something on air places). state_view(collection, layout) gives a sprig as its Live state shows
    it: (layout, fades). Returns the scenes, the sprig placements ((source id, scene key) -> sprig key) and
    canvas id -> the scene key it shows (None: nothing on air). outgoing: canvas id -> the bloom a transition there
    changes from (drawn as from_key(canvas id))."""
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
        old = (outgoing or {}).get(canvas.id)
        if old is not None:
            draws.append(SceneDraw(from_key(canvas.id), old, sizes[canvas.id]))
            place(old, from_key(canvas.id), canvas.id)
    for layout in collection.part_scenes():
        size = sizes.get(layout.canvas)
        if size is None:
            continue
        view, fades = state_view(collection, layout)
        key = part_key(layout.id)
        draws.append(SceneDraw(key, view, size, fades))
        place(view, key, layout.canvas)
    return draws, parts, shows


class CardPool:
    """The engine's texture pool a CardFrame's texture belongs to. An encoder registers each of its textures
    once and lets go of them all when a frame of another pool arrives (output/nvenc_d3d11.py encode_texture)."""

    def __init__(self, pool_id: int):
        self.id = pool_id


class CardFrame:
    """A finished canvas as an NV12 texture on the native engine's device (fmt "gpu"), for SprigCast's own
    NVENC encoder: shaped like render/gpu_share.py GpuFrame. Its texture goes back to the engine when the last
    reference to the frame goes (encoded, dropped, replaced)."""

    __slots__ = ("canvas_id", "width", "height", "time", "fmt", "canvas_size", "pool", "texture", "entry",
                 "_give_back", "__weakref__")

    data = None  # no bytes: see texture

    def __init__(self, canvas_id: str, size: tuple[int, int], drawn: float, pool: CardPool, texture: int,
                 entry: int, give_back):
        self.canvas_id, self.width, self.height, self.time = canvas_id, size[0], size[1], drawn
        self.fmt = "gpu"
        self.canvas_size = size
        self.pool, self.texture, self.entry = pool, texture, entry
        self._give_back = give_back

    @property
    def key(self) -> tuple:
        """The same for the same texture for as long as its pool lives (encoders register each once)."""
        return "native", self.pool.id if self.pool is not None else 0, self.entry

    def __del__(self):
        pool, self.pool = self.pool, None
        if pool is not None:
            try:
                self._give_back(pool.id, self.entry, self.texture)
            except Exception:  # noqa: BLE001 (never from a destructor)
                pass


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
        self._sent_transitions: dict[int, int | None] = {}  # slot -> id() of the Running handed over
        # canvas slot -> output key ((width, height), on the card) -> output slot
        self._outputs: dict[int, dict[tuple, int]] = {}
        self.can_share = True  # until the engine says its device can't draw NV12 textures
        self._pools: dict[int, CardPool] = {}  # the engine's pool id -> its CardPool (the newest few)
        self._output_scale: dict[int, str] = {}
        self._last_out: dict[tuple[str, tuple[int, int]], float] = {}  # newest frame time handed on, per output
        self._dropped: dict[tuple[int, int], int] = {}  # (canvas slot, output slot) -> frames the engine dropped
        self.output_drops = 0
        self._wipe_maps: set[str] = set()
        self._signature = None
        self._last_upload: dict[str, float] = {}
        self._stats_at = 0.0
        self._rebuilds = 0
        self._faults = planned_faults(os.environ.get("SPRIGCAST_ENGINE_FAULT"))  # crash gate test on a PC
        self._began: float | None = None
        if self._faults:
            log.warning("native engine: SPRIGCAST_ENGINE_FAULT is set: %d deliberate faults planned", len(self._faults))
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

    def sync(self, collection: SceneCollection, effects: dict, state_view, fading: bool,
             transitions: dict | None = None, wipe_picture=None) -> None:
        """Hand over a new snapshot when what's on air, the filters, a sprig's state or a transition changed
        (every frame while a sprig's state fades). transitions: canvas id -> render/engine.py Running;
        wipe_picture(path) gives a wipe image as a grey RGB array."""
        if self.failed:
            return
        running = {cid: r for cid, r in (transitions or {}).items() if native_transition(r)}
        signature = (id(collection), id(effects),
                     tuple((c.id, c.enabled, c.width, c.height, collection.active_scenes.get(c.id))
                           for c in collection.canvases),
                     tuple(sorted((cid, id(r)) for cid, r in running.items())))
        if signature == self._signature and not fading:
            return
        self._signature = signature
        draws, parts, shows = snapshot_draws(collection, state_view,
                                             {cid: r.outgoing for cid, r in running.items()})
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
            self._hand_transition(slot, canvas.id, running.get(canvas.id), wipe_picture)

    def _hand_transition(self, slot: int, canvas_id: str, running, wipe_picture) -> None:
        """Start (or stop) the canvas's transition in the engine when Python's changed."""
        sent = id(running) if running is not None else None
        if self._sent_transitions.get(slot) == sent:
            return
        self._sent_transitions[slot] = sent
        if running is None:
            self._guard("transition", self.engine.transition, slot, None, None)
            return
        wipe = running.wipe_map or None
        if wipe and wipe not in self._wipe_maps and wipe_picture is not None:
            picture = wipe_picture(wipe)
            if picture is not None:
                import numpy as np

                rgba = np.concatenate([picture, np.full(picture.shape[:2] + (1,), 255, np.uint8)], axis=-1)
                self._guard("wipe image", self.engine.set_mask, f"wipe:{wipe}", rgba, True)
                self._wipe_maps.add(wipe)
        spec = sprigcore.transition_spec(running.settings, f"wipe:{wipe}" if wipe in self._wipe_maps else None)
        spec.seconds = max(0.001, float(running.duration))
        self._guard("transition", self.engine.transition, slot, from_key(canvas_id), spec)

    def _slot(self, canvas_id: str) -> int | None:
        slot = self._slots.get(canvas_id)
        if slot is None and len(self._slots) < sprigcore.ENGINE_CANVASES:
            slot = self._slots[canvas_id] = len(self._slots)
        return slot

    # ---- what comes out ----------------------------------------------------------------------------------------

    def handles(self, extra: dict) -> bool:
        """Whether this frame's transition arguments (render/engine.py _transition_step) are ones the native
        engine draws itself."""
        transition = extra.get("transition")
        return transition is not None and transition.kind in sprigcore.TRANSITION_MODES and \
            "overlay" not in extra and "move" not in extra

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

    # ---- recordings and streams ---------------------------------------------------------------------------------

    def output_key(self, size: tuple[int, int] | None, on_card: bool) -> tuple | None:
        """What names an output the native engine makes: its NV12 size and whether its frames stay on the
        card (asked for and possible). None: a full-size RGBA tap, which only Python's compositor makes."""
        return None if size is None else (tuple(size), bool(on_card and self.can_share))

    def serves(self, canvas_id: str, keys) -> bool:
        """Whether the native engine can make every one of these outputs (output_key): NV12 only, at most
        ENGINE_OUTPUTS different ones, on a canvas it draws."""
        keys = set(keys)
        return bool(keys) and None not in keys and len(keys) <= sprigcore.ENGINE_OUTPUTS and self.draws(canvas_id)

    def set_outputs(self, canvas_id: str, keys, scale: str = "bilinear") -> None:
        """The outputs (output_key) the native engine makes for this canvas from now on (empty: none). One
        kept keeps its slot, so its frames go on without a gap."""
        slot = self._slots.get(canvas_id)
        if slot is None or self.failed:
            return
        want = sorted(set(keys))[:sprigcore.ENGINE_OUTPUTS]
        current = self._outputs.setdefault(slot, {})
        if want and self._output_scale.get(slot, scale) != scale:  # every output is scaled again: start over
            self.set_outputs(canvas_id, ())
            current = self._outputs.setdefault(slot, {})
        if want:
            self._output_scale[slot] = scale
        for key in [k for k in current if k not in want]:
            out = current.pop(key)
            self._dropped.pop((slot, out), None)
            if not any(k[0] == key[0] for k in current):
                self._last_out.pop((canvas_id, key[0]), None)
            self._guard("output", self.engine.set_output, slot, out, 0, 0)
        free = [o for o in range(sprigcore.ENGINE_OUTPUTS) if o not in current.values()]
        for key in want:
            if key not in current and free:
                out = current[key] = free.pop(0)
                self._guard("output", self.engine.set_output, slot, out, key[0][0], key[0][1], scale, key[1])

    def take_outputs(self, canvas_id: str, key: tuple) -> list:
        """Every frame of this output (output_key) the native engine finished since the last call, oldest
        first: (NV12 data, when it was drawn in time.perf_counter() seconds), or CardFrames for an output on the
        card. Never a frame older than one handed on."""
        slot = self._slots.get(canvas_id)
        out = self._outputs.get(slot, {}).get(key) if slot is not None else None
        if out is None or self.failed:
            return []
        size, on_card = key
        frames = []
        last = self._last_out.get((canvas_id, size), 0.0)
        for _ in range(sprigcore.ENGINE_OUTPUTS * 2):  # the queue holds a few; never loop for ever
            if on_card:
                got = self._guard("output frame", self.engine.take_texture, slot, out)
                if got is None:
                    break
                state, taken, dropped = got
                if state < 0 and self.can_share:
                    self.can_share = False  # outputs are named again next tick: this one goes through memory
                    log.warning("native engine: this graphics card can't keep frames on it for the encoder; "
                                "they go through memory")
                if taken is None:
                    self._count_drops(slot, out, dropped)
                    break
                texture, pool_id, entry, drawn = taken
                frame = CardFrame(canvas_id, size, drawn, self._pool(pool_id), texture, entry,
                                  lambda p, e, t, slot=slot, out=out: self._give_back(slot, out, p, e, t))
            else:
                got = self._guard("output frame", self.engine.take_output, slot, out, size[0], size[1])
                if got is None:
                    break
                data, drawn, dropped = got
                frame = (data, drawn)
            self._count_drops(slot, out, dropped)
            if drawn > last:
                frames.append(frame)
                last = drawn
        self._last_out[(canvas_id, size)] = last
        return frames

    def _count_drops(self, slot: int, out: int, dropped: int) -> None:
        gone = dropped - self._dropped.get((slot, out), 0)
        if gone > 0:
            self._dropped[(slot, out)] = dropped
            self.output_drops += gone

    def _pool(self, pool_id: int) -> CardPool:
        pool = self._pools.get(pool_id)
        if pool is None:
            pool = self._pools[pool_id] = CardPool(pool_id)
            for old in sorted(self._pools)[:-8]:  # frames and encoders keep the ones they still use alive
                del self._pools[old]
        return pool

    def _give_back(self, slot: int, out: int, pool_id: int, entry: int, texture: int) -> None:
        """A CardFrame was let go (on any thread): its texture is the engine's to draw into again."""
        try:
            self.engine.give_back(slot, out, pool_id, entry)
        finally:
            sprigcore.com_release(texture)

    def encoder_device(self) -> int | None:
        """The native engine's Direct3D 11 device for an encoder that takes its frames on the card (an
        ID3D11Device pointer with a reference of the caller's), or None: it can't share frames."""
        if self.failed or not self.can_share:
            return None
        try:
            return self.engine.device()[0] or None
        except Exception:  # noqa: BLE001 (no device right now, mid-rebuild: this encoder takes frames from memory)
            return None

    def served(self, canvas_id: str, size: tuple[int, int], drawn: float) -> None:
        """Python's compositor handed on a frame of this output drawn at `drawn`: a native frame from before
        then would go backwards, so it's let go."""
        key = (canvas_id, size)
        self._last_out[key] = max(self._last_out.get(key, 0.0), drawn)

    def check(self, now: float) -> None:
        """About once a second: the engine's health. A faulted engine turns this off."""
        if self.failed or now - self._stats_at < STATS_EVERY_S:
            return
        self._stats_at = now
        if self._faults:
            self._began = now if self._began is None else self._began
            if now - self._began >= self._faults[0][0]:  # one a check, so repeats land about a second apart
                _when, kind = self._faults.pop(0)
                log.warning("native engine: deliberate fault now (%s), as SPRIGCAST_ENGINE_FAULT asks",
                            FAULT_KINDS[kind])
                self._guard("deliberate fault", self.engine.debug_fault, kind)
        stats = self._guard("stats", self.engine.stats)
        if stats is None:
            return
        if stats["rebuilds"] != self._rebuilds:
            self._rebuilds = stats["rebuilds"]
            log.warning("native engine: rebuilt its device (%d so far)", self._rebuilds)
        if self.output_drops:
            log.warning("native engine: %d recording or stream frames weren't taken in time", self.output_drops)
            self.output_drops = 0
        if stats["faulted"]:
            self.fail("it stopped after repeated faults")
        elif stats["hung"]:
            log.warning("native engine: a frame is stuck")

    def close(self) -> None:
        try:
            self.engine.close()
        except Exception:
            log.exception("native engine: closing failed")
