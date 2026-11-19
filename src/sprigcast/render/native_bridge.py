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
import sys
import time
import weakref

from sprigcast import sprigcore
from sprigcast.core import drawn, painted
from sprigcast.core.scene import Scene, SceneCollection
from sprigcast.render import native_snapshot
from sprigcast.render.native_snapshot import SceneDraw

log = logging.getLogger(__name__)

STILL_AFTER_S = 1.0  # a source that sent nothing for this long is treated as a still picture (kept for rebuilds)
STATS_EVERY_S = 1.0
HUNG_GIVE_UP_S = 3.0  # a frame stuck this long (after the engine's own 2 s) and Python draws everything again


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


def view_key(name: str) -> str:
    """A view's scene in the snapshot, and its canvas slot: the same name the render thread taps it by."""
    return f"view:{name}"


def engine_kind(kind: str) -> bool:
    """Whether the native engine draws transitions of this kind (core/scene.py Transition.kind)."""
    return kind in sprigcore.TRANSITION_MODES or (kind == "move" and sprigcore.use_native_move()) or \
        (kind == "stinger" and sprigcore.use_native_stinger())


def native_transition(running) -> bool:
    """Whether the native engine draws this running transition (render/engine.py Running): a stinger only when its
    clip plays in the engine's own player (render/stinger.py NativeStinger), which the engine takes it from."""
    if running is None or running.began is None or not engine_kind(running.settings.kind):
        return False
    if running.settings.kind == "stinger":
        return running.stinger is not None and getattr(running.stinger, "engine_player", None) is not None
    return running.stinger is None


def snapshot_draws(collection: SceneCollection, state_view, outgoing: dict[str, Scene] | None = None,
                   views: dict | None = None, held: dict[str, Scene] | None = None
                   ) -> tuple[list[SceneDraw], dict, dict[str, str | None]]:
    """What the native engine draws: each enabled canvas's bloom on air and every sprig layout (the engine draws
    only those something on air places). state_view(collection, layout) gives a sprig as its Live state shows
    it: (layout, fades). Returns the scenes, the sprig placements ((source id, scene key) -> sprig key) and
    canvas id -> the scene key it shows (None: nothing on air). outgoing: canvas id -> the bloom a transition there
    changes from (drawn as from_key(canvas id)). views: name -> (canvas, scene) for each view something taps (the
    virtual camera showing a bloom, Instant replay keeping a camera): drawn as view_key(name), shown in
    shows[view_key(name)]. held: canvas id -> the bloom it keeps showing while a switch waits for a camera
    (render/engine.py _still_waking), drawn in place of the one on air."""
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
        scene = (held or {}).get(canvas.id) or collection.active_scene(canvas.id)
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
    for name, (canvas, scene) in sorted((views or {}).items()):
        key = view_key(name)
        draws.append(SceneDraw(key, scene, (canvas.width, canvas.height)))
        place(scene, key, canvas.id)
        shows[key] = key
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
    held = 0  # frames not given back yet, all outputs together (said when an output's feed stops)

    def __init__(self, canvas_id: str, size: tuple[int, int], drawn: float, pool: CardPool, texture: int,
                 entry: int, give_back):
        CardFrame.held += 1
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
            CardFrame.held -= 1
            try:
                self._give_back(pool.id, self.entry, self.texture)
            except Exception:  # noqa: BLE001 (never from a destructor)
                pass


def _wants_card(owner) -> bool:
    """Whether a recording still takes its frames on the card (Recorder.wants_gpu_frames)."""
    wants = getattr(owner, "wants_gpu_frames", None)
    try:
        return bool(wants()) if wants is not None else False
    except Exception:  # asked again next tick
        log.exception("native engine: asking a recording about frames on the card failed")
        return False


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
        self._last_out: dict[tuple[str, tuple], float] = {}  # newest frame time handed on, per (canvas, output_key)
        self._served: dict[tuple[str, tuple[int, int]], float] = {}  # newest Python handed on, per (canvas, size)
        self._dropped: dict[tuple[int, int], int] = {}  # (canvas slot, output slot) -> frames the engine dropped
        # (canvas slot, output slot) -> (recording, its writer, (start, fps, keep)): outputs the engine hands to a
        # writer itself (link_taps)
        self._links: dict[tuple[int, int], tuple] = {}
        self._paused: dict[tuple[int, int], tuple] = {}  # (owner, writer) unlinked for a transition Python draws
        self._link_refused = weakref.WeakSet()  # recordings whose writer an output wouldn't link to
        self.output_drops = 0
        self.lost = 0  # frames of the output take_outputs last looked at that the engine let go of
        self._wipe_maps: set[str] = set()
        self._signature = None
        self._last_upload: dict[str, float] = {}
        self._media: dict[str, object] = {}  # source id -> the engine's player its pictures come straight from
        self._held: set[str] = set()  # browser pages whose pictures the engine takes but doesn't show (a freeze)
        # source id -> art key -> (its engine id, cacheKey, bytes, the list it was last used in)
        self._arts: dict[str, dict[str, tuple[str, int, int, int]]] = {}
        self._lists = 0
        self._stats_at = 0.0
        self._rebuilds = 0
        self._hung_since: float | None = None  # when a stuck frame was first seen (no frame finished since)
        self._hung_ticks = -1
        self._faults = planned_faults(os.environ.get("SPRIGCAST_ENGINE_FAULT"))  # crash gate test on a PC
        self._began: float | None = None
        if self._faults:
            log.warning("native engine: SPRIGCAST_ENGINE_FAULT is set: %d deliberate faults planned", len(self._faults))
        log.info("native engine: drawing the previews (%s)", "software" if warp else "graphics card")
        self._card_told = False
        self._tell_card()
        drawn.ENGINE = True  # store plugins may hand over lists of pictures (core/drawn.py)

    def _tell_card(self) -> None:
        """Browser pages are drawn on the engine's card (sources/browser.use_card), once it has a device."""
        if self._card_told or self.failed or sys.platform != "win32":
            return
        luid_of = getattr(self.engine, "luid", None)
        if luid_of is None:  # a stand-in engine (tests)
            self._card_told = True
            return
        try:
            luid = luid_of()
        except Exception as exc:  # noqa: BLE001 - pages stay in memory
            log.info("native engine: its card is unknown (%s): browser pages stay in memory", exc)
            self._card_told = True
            return
        if luid is not None:
            self._card_told = True
            from sprigcast.sources import browser

            browser.use_card(luid)

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
            drawn.ENGINE = False
            log.error("native engine: %s; the Python compositor draws the previews again", why)
            self._unlink_all()

    def upload(self, source_id: str, frame, channel_order: str, size) -> None:
        """A source's new picture. The first picture after a quiet spell is kept by the engine (a still picture
        sends once, and must come back by itself if the engine rebuilds); a stream of pictures isn't."""
        now = time.perf_counter()
        last = self._last_upload.get(source_id)
        self._last_upload[source_id] = now
        keep = last is None or now - last > STILL_AFTER_S
        if source_id in self._arts:  # a picture again (the show is over): its list's art can go
            self._forget_arts(source_id)
        self._guard("upload", self.engine.upload, source_id, frame, channel_order, size, keep=keep)

    def attach_media(self, source_id: str, player, held: bool = False) -> bool:
        """``player`` (a sprigcore.MediaPlayer, the name of a browser page's shared memory block, a
        sprigcore.CaptureTarget, or None) is where the engine takes this source's pictures from, each frame, by
        itself. True while it does: the render thread then sends it none. ``held``: a page's block or a capture
        frozen (Snap's freeze): the engine keeps its picture but still takes the new ones, which would otherwise
        wait for it (and make a page's texture again every few seconds: PC, 2026-11-18; a capture would have to
        start over)."""
        if self.failed:
            return False
        capture = isinstance(player, sprigcore.CaptureTarget)
        held = bool(held) and (isinstance(player, str) or capture)
        if held != (source_id in self._held):
            if held:
                self._held.add(source_id)
            else:
                self._held.discard(source_id)
            if capture:
                self._guard("hold", self.engine.hold_capture, source_id, held)
            elif isinstance(player, str) or not held:
                self._guard("hold", self.engine.hold_shared, source_id, held)
        had = self._media.get(source_id)
        if had is player or ((isinstance(player, str) or capture) and had == player):
            if capture and not player.linked:
                player.link(self.engine, source_id)
            return player is not None
        if isinstance(had, sprigcore.CaptureTarget):
            had.link(None)
            if not capture:  # a capture let go (a new one replaces it in the engine)
                self._guard("capture", self.engine.attach_capture, source_id, None)
        elif isinstance(had, str):
            if not isinstance(player, str):  # a block let go (a new one would replace it)
                self._guard("shared", self.engine.attach_shared, source_id, None)
        elif had is not None and (player is None or isinstance(player, str) or capture):  # a player let go
            self._guard("media", self.engine.attach_media, source_id, None)
        if player is None:
            self._media.pop(source_id, None)
            return False
        if isinstance(player, str):
            self._tell_card()
        self._media[source_id] = player
        if capture:
            self._guard("capture", self.engine.attach_capture, source_id, player)
            if not self.failed:
                player.link(self.engine, source_id)
        elif isinstance(player, str):
            self._guard("shared", self.engine.attach_shared, source_id, player)
        else:
            self._guard("media", self.engine.attach_media, source_id, player)
        return not self.failed

    SPARE_ARTS = 16  # pictures a drawn source keeps on the card beyond those its newest list uses...
    SPARE_BYTES = 48 << 20  # ...and at most this much of them (the oldest used go first)

    def draw_list(self, source_id: str, picture: drawn.Drawn) -> None:
        """A source's picture as a list of pictures (core/drawn.py): new art goes to the card once (kept, so it
        comes back after a rebuild), then only where each picture goes."""
        if self.failed:
            return
        self._last_upload.pop(source_id, None)
        self._lists += 1
        turn = self._lists
        arts = self._arts.setdefault(source_id, {})
        items, used = [], set()
        for art_key, image, x, y, w, h, rotation, crop, opacity in picture.items():
            known = arts.get(art_key)
            stamp = image.cacheKey()
            if known is None or known[1] != stamp:
                art_id = known[0] if known is not None else f"{source_id}/art/{art_key}"
                frame = painted.Frame()(image)
                self._guard("art", self.engine.upload, art_id, frame, "pBGRA", None, keep=True)
                if self.failed:
                    return
                known = (art_id, stamp, frame.nbytes, turn)
            arts[art_key] = (*known[:3], turn)
            used.add(art_key)
            items.append((known[0], x, y, w, h, rotation, crop, opacity, image.width(), image.height()))
        spare = sorted((k for k in arts if k not in used), key=lambda k: arts[k][3], reverse=True)  # newest first
        kept, held = 0, 0
        for art_key in spare:
            kept, held = kept + 1, held + arts[art_key][2]
            if kept > self.SPARE_ARTS or held > self.SPARE_BYTES:
                self._guard("art", self.engine.forget, arts.pop(art_key)[0])
        self._guard("draws", self.engine.draws, source_id, picture.width, picture.height, items)

    def _forget_arts(self, source_id: str) -> None:
        for art_id, *_rest in self._arts.pop(source_id, {}).values():
            self._guard("forget", self.engine.forget, art_id)

    def forget_gone(self, source_ids) -> None:
        for gone in (set(self._last_upload) | set(self._media) | set(self._arts)) - set(source_ids):
            self._last_upload.pop(gone, None)
            had = self._media.pop(gone, None)
            if isinstance(had, sprigcore.CaptureTarget):
                had.link(None)
            self._held.discard(gone)
            self._forget_arts(gone)
            self._guard("forget", self.engine.forget, gone)

    def sync(self, collection: SceneCollection, effects: dict, state_view, fading: bool,
             transitions: dict | None = None, wipe_picture=None, views: dict | None = None,
             held: dict | None = None, move_pieces=None) -> None:
        """Hand over a new snapshot when what's on air, the filters, a sprig's state or a transition changed
        (every frame while a sprig's state fades). transitions: canvas id -> render/engine.py Running;
        wipe_picture(path) gives a wipe image as a grey RGB array. held: canvas id -> the bloom it keeps showing
        while a switch waits (see snapshot_draws): the engine keeps drawing it, so its outputs go on.
        move_pieces(running, canvas): a Move's two blooms as render/move.py pieces (old, new), when one starts."""
        if self.failed:
            return
        running = {cid: r for cid, r in (transitions or {}).items() if native_transition(r)}
        for canvas in collection.canvases:  # canvases get their slots before any view does
            self._slot(canvas.id)
        views = {name: view for name, view in (views or {}).items() if self._slot(view_key(name)) is not None}
        for gone in [key for key in self._slots if key.startswith("view:") and key[5:] not in views]:
            self.set_outputs(gone, ())
            slot = self._slots.pop(gone)  # its slot is free for another view from now on
            self._guard("show", self.engine.show, slot, None)
            self._guard("canvas", self.engine.set_canvas, slot, 0, 0)
            for table in (self._canvas_sizes, self._shown, self._watching, self._sent_transitions,
                          self._outputs, self._output_scale):
                table.pop(slot, None)
        signature = (id(collection), id(effects),
                     tuple(sorted((name, id(scene), canvas.width, canvas.height)
                                  for name, (canvas, scene) in views.items())),
                     tuple((c.id, c.enabled, c.width, c.height, collection.active_scenes.get(c.id))
                           for c in collection.canvases),
                     tuple(sorted((cid, id(r)) for cid, r in running.items())),
                     tuple(sorted((cid, id(scene)) for cid, scene in (held or {}).items())))
        if signature == self._signature and not fading:
            return
        self._signature = signature
        draws, parts, shows = snapshot_draws(collection, state_view,
                                             {cid: r.outgoing for cid, r in running.items()}, views, held)
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
            self._hand_transition(slot, canvas, running.get(canvas.id), wipe_picture,
                                  lambda r, c=canvas: self._move_spec(collection, effects, move_pieces, r, c))
        for name, (canvas, _scene) in views.items():
            key = view_key(name)
            slot = self._slots[key]
            size = (canvas.width, canvas.height)
            if self._canvas_sizes.get(slot) != size:
                self._canvas_sizes[slot] = size
                self._guard("canvas", self.engine.set_canvas, slot, *size)
            if self._shown.get(slot, "") != key:
                self._shown[slot] = key
                self._guard("show", self.engine.show, slot, key)

    def _hand_transition(self, slot: int, canvas, running, wipe_picture, move_spec=None) -> None:
        """Start (or stop) the canvas's transition in the engine when Python's changed."""
        canvas_id = canvas.id
        sent = id(running) if running is not None else None
        if self._sent_transitions.get(slot) == sent:
            return
        self._sent_transitions[slot] = sent
        if running is None:
            self._guard("transition", self.engine.transition, slot, None, None)
            return
        if running.settings.kind == "move":
            spec = move_spec(running) if move_spec is not None else None
            if spec is None:  # nothing to lay out: the engine cuts to the new bloom
                self._guard("transition", self.engine.transition, slot, None, None)
                return
            self._guard("move", self.engine.move, slot, from_key(canvas_id), float(running.duration), spec)
            return
        wipe = running.wipe_map or None
        if wipe and wipe not in self._wipe_maps and wipe_picture is not None:
            picture = wipe_picture(wipe)
            if picture is not None:
                import numpy as np

                rgba = np.concatenate([picture, np.full(picture.shape[:2] + (1,), 255, np.uint8)], axis=-1)
                self._guard("wipe image", self.engine.set_mask, f"wipe:{wipe}", rgba, True)
                self._wipe_maps.add(wipe)
        player = running.stinger
        spec = sprigcore.transition_spec(running.settings, f"wipe:{wipe}" if wipe in self._wipe_maps else None,
                                         player.id if player is not None else None)
        spec.seconds = max(0.001, float(running.duration))
        self._guard("transition", self.engine.transition, slot, from_key(canvas_id), spec)

    def _slot(self, canvas_id: str) -> int | None:
        """The engine's canvas slot for a canvas or a view (view_key), given the first free one if it has none.
        None when all are taken: Python's compositor draws it."""
        slot = self._slots.get(canvas_id)
        if slot is None:
            free = sorted(set(range(sprigcore.ENGINE_CANVASES)) - set(self._slots.values()))
            if free:
                slot = self._slots[canvas_id] = free[0]
        return slot

    @staticmethod
    def _move_spec(collection: SceneCollection, effects: dict, move_pieces, running, canvas):
        """A Move's pieces as the engine takes them (sprigcore.move_spec): sprigs drawn whole by their sprig's
        drawing in the snapshot (part_key), every source with its filters. None without a way to lay it out."""
        if move_pieces is None:
            return None
        from sprigcast.render import move

        old, new = move_pieces(running, canvas)
        sources = {s.id: s for s in collection.sources}
        made: dict[int, object] = {}

        def piece(p):
            source = sources.get(p.source)
            part = collection.part_of(source) if source is not None else None
            part_id = part_key(collection.shape_for(part, p.canvas).id) if part is not None else None
            fx = effects.get(p.source)
            fx_struct = None
            if fx is not None and fx != native_snapshot.NO_EFFECTS:
                fx_struct = made.get(id(fx))
                if fx_struct is None:
                    fx_struct = made[id(fx)] = native_snapshot.effects_struct(fx)
            return p.source, part_id, p.quad, p.opacity, fx_struct

        t = running.settings
        return sprigcore.move_spec([piece(p) for p in old], [piece(p) for p in new], move.pair(old, new),
                                   t.easing, t.unmatched)

    # ---- what comes out ----------------------------------------------------------------------------------------

    def handles(self, extra: dict) -> bool:
        """Whether this frame's transition arguments (render/engine.py _transition_step) are ones the native
        engine draws itself."""
        transition = extra.get("transition")
        if transition is None or not engine_kind(transition.kind):
            return False
        if transition.kind == "stinger":
            return bool(extra.get("engine_stinger"))  # its clip plays in the engine's own player
        return "overlay" not in extra

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

    def output_key(self, size: tuple[int, int] | None, on_card: bool, owner: int | None = None) -> tuple | None:
        """What names an output the native engine makes: its NV12 size and whether its frames stay on the
        card (asked for and possible), and for a recording or stream through the engine's own writer, id() of it
        (owner): it gets an output of its own, so the engine hands it every frame itself, on the card too, even
        when another one takes the same size (render/engine.py _output_keys). None: a full-size RGBA tap, which
        only Python's compositor makes."""
        if size is None:
            return None
        key = (tuple(size), bool(on_card and self.can_share))
        return key if owner is None else (*key, owner)

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
            self._unlink(slot, out)
            self._dropped.pop((slot, out), None)
            self._last_out.pop((canvas_id, key), None)
            self._guard("output", self.engine.set_output, slot, out, 0, 0)
        free = [o for o in range(sprigcore.ENGINE_OUTPUTS) if o not in current.values()]
        for key in want:
            if key not in current and free:
                out = current[key] = free.pop(0)
                self._guard("output", self.engine.set_output, slot, out, key[0][0], key[0][1], scale, key[1])

    def pause_outputs(self, canvas_id: str, keys, scale: str = "bilinear") -> None:
        """Python draws this canvas's outputs for a moment (a Move or stinger transition): the engine's outputs
        stay, with their texture pools, but their recordings are unlinked (they take Python's frames meanwhile)
        and the engine's frames are let go. Dropped and made again for each transition, every transition made
        new pools, and a writer on the card keeps each texture it was handed (PC soak 2026-11-18). Once the
        engine draws the canvas again, set_outputs with the same keys changes nothing and the links come back
        on the same textures."""
        self.set_outputs(canvas_id, keys, scale)
        slot = self._slots.get(canvas_id)
        if slot is None or self.failed:
            return
        for out in list(self._outputs.get(slot, {}).values()):
            entry = self._links.get((slot, out))
            if entry is not None:
                self._paused[(slot, out)] = entry[:2]
                self._unlink(slot, out)
        for key in list(self._outputs.get(slot, {})):
            self.take_outputs(canvas_id, key)  # let go of (CardFrames give their textures back when dropped)
        self.lost = 0

    def reset_outputs(self) -> bool:
        """Let go of every output; the render thread names them again next tick (render/engine.py
        refresh_outputs): a feed that stopped starts over (new texture pools: frames still held elsewhere
        no longer hold it up). False, and nothing done, while the engine's frame is stuck: it would come back
        to outputs changed under it (crash run 2026-11-17); the caller asks again next tick."""
        stats = self._guard("stats", self.engine.stats)
        if stats is not None and stats.get("hung"):
            return False
        now = time.perf_counter()
        made = []  # what each output had made, to tell a feed that stopped from one whose frames weren't taken
        for canvas_id, slot in self._slots.items():
            for key, out in self._outputs.get(slot, {}).items():
                size, on_card = key[0], key[1]
                last = self._last_out.get((canvas_id, key))
                made.append(f"{canvas_id} {size[0]}x{size[1]}{' on the card' if on_card else ''}: last frame "
                            f"{f'{now - last:.1f} s ago' if last else 'never'}, "
                            f"{self._dropped.get((slot, out), 0)} missed by the engine")
        log.warning("native engine: making every output again (%d frames on the card not given back yet, "
                    "%d not taken in time so far; %s)", CardFrame.held, self.output_drops,
                    "; ".join(made) or "no outputs")
        for canvas_id in list(self._slots):
            self.set_outputs(canvas_id, ())
        self._last_out.clear()
        self._served.clear()
        return True

    def take_outputs(self, canvas_id: str, key: tuple) -> list:
        """Every frame of this output (output_key) the native engine finished since the last call, oldest
        first: (NV12 data, when it was drawn in time.perf_counter() seconds), or CardFrames for an output on the
        card. Never a frame older than one handed on."""
        slot = self._slots.get(canvas_id)
        out = self._outputs.get(slot, {}).get(key) if slot is not None else None
        if out is None or self.failed:
            return []
        size, on_card = key[0], key[1]
        frames = []
        before = self.output_drops
        last = max(self._last_out.get((canvas_id, key), 0.0), self._served.get((canvas_id, size), 0.0))
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
        self._last_out[(canvas_id, key)] = last
        self.lost = self.output_drops - before  # what the engine let go of since the last call, for the outputs' counts
        return frames

    # ---- outputs the engine hands to a writer itself ----------------------------------------------------------------

    def link_taps(self, canvas_id: str, taps) -> set[int]:
        """Link each plain output of this canvas to a recording that can take its frames straight from the engine
        (Recorder.link_writer: the engine's own writer), so they never pass through Python. taps: (callback,
        output_key) pairs. Returns id() of each recording linked now: its frames aren't handed to it here. Others
        sharing a linked output still take its frames (the link keeps them queued)."""
        slot = self._slots.get(canvas_id)
        if slot is None or self.failed or os.environ.get("SPRIGCAST_NO_LINK"):
            return set()
        current = self._outputs.get(slot, {})
        owners_of: dict[tuple, list] = {}
        for callback, key in taps:
            owners_of.setdefault(key, []).append(getattr(callback, "__self__", None))
        linked: set[int] = set()
        for key, owners in owners_of.items():
            out = current.get(key)
            if out is None:
                continue
            entry = self._links.get((slot, out))
            on_card = bool(key[1])
            if on_card and entry is not None and entry[2][3] != self._rebuilds:
                # The engine made its device again: its textures can't go to an encoder opened on the old one.
                # The recording goes on with pictures from memory, which its writer copies up itself.
                self._unlink(slot, out)
                entry = None
                for o in owners:
                    lost = getattr(o, "card_lost", None)
                    if lost is not None:
                        lost("the native engine made its graphics device again")
            # Frames on the card go only to a writer that takes them there (its encoder on the engine's device),
            # and only when nothing else takes that output's frames (a stream of the same size, on SprigCast's
            # own NVENC): a writer sharing them takes pictures from memory instead, linked as those.
            if on_card and len(owners) > 1:
                for o in owners:
                    lost = getattr(o, "card_lost", None) if getattr(o, "link_writer", lambda: None)() else None
                    if lost is not None:
                        lost("another output takes the same frames on the card")
            wanting = [] if on_card and len(owners) > 1 else \
                [(o, w) for o in owners if o is not None and not self._refused(o)
                 for w in [getattr(o, "link_writer", lambda: None)()]
                 if w is not None and (not on_card or (getattr(w, "on_card", False) and _wants_card(o)))]
            chosen = next(((o, w) for o, w in wanting if entry is not None and o is entry[0]), None) or \
                next(iter(wanting), None)
            camera = None if chosen is not None or on_card else self._camera_for(owners)
            changed = entry is not None and (chosen is None or chosen[0] is not entry[0] or chosen[1] is not entry[1])
            if changed and (camera is None or camera[0] is not entry[0] or camera[1] != entry[1]):
                self._unlink(slot, out)
                entry = None
            if camera is not None:
                if self._link_camera(canvas_id, slot, out, entry, camera, len(owners) > 1):
                    linked.add(id(camera[0]))
                continue
            if chosen is None:
                continue
            owner, writer = chosen
            start, fps, last, fill = owner.link_numbers()
            settings = (start, fps, len(owners) > 1, self._rebuilds if on_card else None)
            if entry is None or entry[2] != settings:
                try:
                    self.engine.link_output(slot, out, writer, start, fps, last_pts=last, keep=settings[2],
                                            fill_seconds=fill)
                except Exception as exc:  # noqa: BLE001 - this recording takes its frames through Python
                    lost = getattr(owner, "card_lost", None) if on_card else None
                    if lost is not None:  # its frames come from memory from the next tick, linked as those
                        lost(str(exc))
                        continue
                    self._refuse(owner)
                    log.warning("native engine: %s can't take frames from the engine itself (%s); they go through "
                                "Python", getattr(getattr(owner, "path", None), "stem", "a recording"), exc)
                    continue
                again = self._paused.pop((slot, out), None) == (owner, writer)  # back after a transition
                if entry is None:
                    log.log(logging.DEBUG if again else logging.INFO,
                            "native engine: %s %dx%d frames go straight to %s's writer%s", canvas_id, key[0][0],
                            key[0][1], getattr(getattr(owner, "path", None), "stem", "a recording"),
                            " on the card" if on_card else "")
                owner.linked = True
                self._links[(slot, out)] = (owner, writer, settings)
            linked.add(id(owner))
        return linked

    def _camera_for(self, owners):
        """(owner, (block name, width, height)) of the virtual camera among an output's takers, if it can be fed
        by the engine itself (VirtualCamera.link_camera)."""
        for o in owners:
            ask = getattr(o, "link_camera", None)
            if o is None or ask is None or self._refused(o):
                continue
            try:
                where = ask()
            except Exception:  # asked again next tick
                log.exception("native engine: asking the virtual camera where to write failed")
                continue
            if where:
                return o, tuple(where)
        return None

    def _link_camera(self, canvas_id: str, slot: int, out: int, entry, camera, keep: bool) -> bool:
        """The engine writes this output's frames into the virtual camera's block itself (no Python per frame)."""
        owner, where = camera
        settings = ("camera", keep)
        if entry is not None and entry[2] == settings:
            return True
        try:
            self.engine.link_camera(slot, out, where[0], where[1], where[2], keep=keep)
        except Exception as exc:  # noqa: BLE001 - the camera takes its frames through Python
            self._refuse(owner)
            log.warning("native engine: the virtual camera can't take frames from the engine itself (%s); they go "
                        "through Python", exc)
            return False
        again = self._paused.pop((slot, out), None) == (owner, where)  # back after a transition
        if entry is None:
            log.log(logging.DEBUG if again else logging.INFO,
                    "native engine: %s frames go straight to the virtual camera (%dx%d)", canvas_id, where[1],
                    where[2])
        owner.linked = True
        self._links[(slot, out)] = (owner, where, settings)
        return True

    def camera_frames(self, owner) -> int:
        """Frames the engine wrote into the virtual camera's block for this owner's links."""
        total = 0
        for (slot, out), (o, _where, settings) in list(self._links.items()):
            if o is owner and settings and settings[0] == "camera":
                try:
                    total += self.engine.camera_frames(slot, out)
                except Exception as exc:  # noqa: BLE001 - only a count
                    log.debug("native engine: the camera's frame count: %s", exc)
        return total

    def _refused(self, owner) -> bool:
        try:
            return owner in self._link_refused
        except TypeError:  # can't be weakly referenced: never linked
            return True

    def _refuse(self, owner) -> None:
        try:
            self._link_refused.add(owner)
        except TypeError:
            pass

    def _unlink(self, slot: int, out: int) -> None:
        """End an output's link; the recording goes on with Python's frames after the last one its writer took."""
        entry = self._links.pop((slot, out), None)
        if entry is None:
            return
        owner, writer, _settings = entry
        last = -1
        try:
            last, _linked = self.engine.unlink_output(slot, out)
        except Exception:  # the writer still knows
            log.exception("native engine: unlinking an output failed")
        if hasattr(writer, "stats"):  # not the virtual camera's block
            try:
                last = max(last, int(writer.stats().last_pts))
            except Exception as exc:  # noqa: BLE001 - closed: nothing more is written to it anyway
                log.debug("native engine: the unlinked writer's numbers are gone (%s)", exc)
        try:
            owner.unlinked(last)
        except Exception:
            log.exception("native engine: a recording's unlink failed")

    def _unlink_all(self) -> None:
        for slot, out in list(self._links):
            self._unlink(slot, out)
        for had in self._media.values():  # captures read their pictures from the engine no more
            if isinstance(had, sprigcore.CaptureTarget):
                had.link(None)

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
        key = (canvas_id, tuple(size))
        self._served[key] = max(self._served.get(key, 0.0), drawn)

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
        elif stats["hung"] and stats.get("ticks", 0) == self._hung_ticks:
            self._hung_since = now if self._hung_since is None else self._hung_since
            if now - self._hung_since >= HUNG_GIVE_UP_S:
                # A frame that never ends (a call into Windows that never came back: PC test 2026-11-18). The
                # engine can't be stopped from here; Python draws everything again and the engine is left alone.
                self.fail(f"a frame has been stuck for {now - self._hung_since:.0f} s")
            else:
                log.warning("native engine: a frame is stuck")
        else:
            self._hung_since = None
        self._hung_ticks = stats.get("ticks", 0)

    def close(self) -> None:
        drawn.ENGINE = False
        self._unlink_all()
        try:
            self.engine.close()
        except Exception:
            log.exception("native engine: closing failed")
