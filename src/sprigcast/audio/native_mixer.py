"""The engine's sound mixer driven like audio/mixer.py's Mixer (docs/native-engine.md "Sound").

With the native engine (SPRIGCAST_ENGINE=native) the mix itself is made in C++ (sprigcore.SoundMixer) on a thread of
the engine's own, paced by the clock, so a busy Python (the GIL) never makes the sound stutter: per sound mono,
balance, delay, the fade of a sound per bloom, volume, ducking, every named track, the clean track and the monitor
mix. Sounds the engine plays itself (video and sound files) go straight from their player into the mix; the others
are still made in Python and pushed in by the feeder thread here, one block ahead, with their filters applied. The
feeder also hands the finished blocks to each output and the levels to the meters.

SPRIGCAST_NO_NATIVE_MIXER=1 keeps Python's mixer.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

import numpy as np

from sprigcast import sprigcore
from sprigcast.audio import listen
from sprigcast.audio.mixer import (
    BLOCK,
    FLOOR_DB,
    LEVELS_EVERY,
    SAY_AGAIN_S,
    Ducking,
    is_monitor_only,
    is_monitored,
    is_voice,
    source_tracks,
    to_db,
)
from sprigcast.core.filters import SOUND_LEVELS

log = logging.getLogger(__name__)

CATCH_UP = 4  # blocks pushed at most in one go after the feeder was held up (more would only add delay)
WAIT_MS = 200


def wanted() -> bool:
    """Whether to mix in the engine: the native engine asked for, its library there, not switched off."""
    if os.environ.get("SPRIGCAST_NO_NATIVE_MIXER"):
        return False
    try:
        return sprigcore.engine_choice() == "native" and sprigcore._media_lib() is not None
    except Exception:  # noqa: BLE001 (no engine: Python's mixer)
        return False


def make_mixer(report: Callable[[dict[str, tuple[float, float]]], None]):
    """The mixer render/engine.py uses: the engine's when it can, else Python's."""
    from sprigcast.audio.mixer import Mixer

    if wanted():
        try:
            return NativeMixer(report)
        except Exception as exc:  # noqa: BLE001 (Python's mixer instead)
            log.warning("sound: the engine's mixer isn't available (%s); Python mixes", exc)
    return Mixer(report)


class _Sound:
    """One sound as the engine knows it."""

    __slots__ = ("asleep", "id", "media", "obj", "settings")

    def __init__(self, obj, native_id: int):
        self.obj = obj
        self.id = native_id
        self.settings: tuple | None = None
        self.media = None  # the engine player it reads straight from, if any
        self.asleep: bool | None = None  # what the player was last told


class NativeMixer:
    """Same calls as audio/mixer.py's Mixer."""

    native = True

    def __init__(self, report: Callable[[dict[str, tuple[float, float]]], None], clocked: bool = True):
        self.report = report
        self.ducking = Ducking()
        self.clean_out: frozenset[str] = frozenset()
        self.bloom_out: frozenset[str] = frozenset()
        self.followed: frozenset[str] = frozenset()
        self._engine = sprigcore.SoundMixer(clocked=clocked)
        self._clocked = clocked
        self._lock = threading.Lock()
        self._sources: list = []
        self._extras: list = []
        self._taps: list[tuple[Callable, tuple[str, ...]]] = []
        self._monitor_taps: list[Callable[[np.ndarray], None]] = []
        self._known: dict[int, _Sound] = {}  # id(source object) -> its sound in the engine
        self._native_taps: dict[tuple, int] = {}  # (callback, tracks) -> the engine's tap
        self._listen_taps: dict[str, int] = {}  # a sound (or listen.MIX) heard by a plugin -> the engine's tap
        self._ducking_sent: Ducking | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sprigcast-audio", daemon=True)
        self._said: dict[str, tuple[str, float]] = {}
        self._meters: dict[str, tuple[float, float]] = {}
        self._fed = -1  # the last block number pushed for
        self._sounds: list = []  # (source, _Sound) as last fed
        self.ticks = 0

    # Same calls as Mixer ------------------------------------------------------------------------------

    @property
    def duck_gain(self) -> float:
        try:
            return float(self._engine.levels(sprigcore.MIXER_MASTER)[2])
        except Exception:  # noqa: BLE001 (closed)
            return 1.0

    def set_sources(self, sources: list) -> None:
        with self._lock:
            self._sources = [s for s in sources if hasattr(s, "read_block")]

    def add_extra(self, player) -> None:
        with self._lock:
            if player not in self._extras:
                self._extras = [*self._extras, player]

    def remove_extra(self, player) -> None:
        with self._lock:
            self._extras = [p for p in self._extras if p is not player]

    def same_mix(self, a: str, b: str) -> bool:
        with self._lock:
            sources = self._sources + self._extras
        return all((a in source_tracks(s)) == (b in source_tracks(s)) for s in sources)

    def add_tap(self, callback: Callable, tracks: tuple[str, ...] = ("stream",)) -> None:
        with self._lock:
            self._taps = [*self._taps, (callback, tuple(tracks))]

    def remove_tap(self, callback: Callable) -> None:
        with self._lock:
            self._taps = [t for t in self._taps if t[0] != callback]

    def set_ducking(self, ducking: Ducking) -> None:
        self.ducking = ducking

    def add_monitor_tap(self, callback: Callable[[np.ndarray], None]) -> None:
        with self._lock:
            self._monitor_taps = [*self._monitor_taps, callback]

    def remove_monitor_tap(self, callback: Callable[[np.ndarray], None]) -> None:
        with self._lock:
            self._monitor_taps = [t for t in self._monitor_taps if t != callback]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._let_go_of_players()
        self._engine.close()

    # The feeder ------------------------------------------------------------------------------------------

    def _problem(self, what: str, exc: Exception) -> None:
        problem = f"{type(exc).__name__}: {exc}"
        now = time.monotonic()
        said = self._said.get(what)
        if said is None or said[0] != problem or now - said[1] >= SAY_AGAIN_S:
            self._said[what] = (problem, now)
            log.error("sound: %s failed: %s", what, problem, exc_info=exc)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                now = self._engine.wait(self._fed, WAIT_MS)
            except Exception as exc:  # noqa: BLE001 (closed under it: the loop ends)
                self._problem("waiting for the mixer", exc)
                if self._stop.wait(0.1):
                    break
                continue
            if now <= self._fed:
                continue
            try:
                self.step(min(CATCH_UP, now - self._fed) if self._fed >= 0 else 1)
            except Exception as exc:  # noqa: BLE001 - never end the sound thread: the next block tries again
                self._problem("feeding the mixer", exc)
            self._fed = now

    def step(self, blocks: int = 1) -> None:
        """After the engine mixed a block: hand the finished blocks to the outputs (and now and then the levels to
        the meters), then push the next one of each Python sound."""
        self._drain()
        self._feed(blocks)

    def _feed(self, blocks: int) -> None:
        with self._lock:
            sources = self._sources + self._extras
        heard = listen.LISTENERS
        self._sounds = sounds = self._reconcile(sources, heard)
        self._send_ducking()
        self._sync_taps()
        for _ in range(blocks):
            for source, sound in sounds:
                if sound.media is None:
                    self._push(source, sound, heard)
                else:
                    kept = getattr(source, "sound_kept", None)
                    if kept is not None:
                        kept()

    def _drain(self) -> None:
        self._hand_out(listen.LISTENERS)
        self.ticks += 1
        if self.ticks % LEVELS_EVERY == 0:
            self._levels(self._sounds)

    def tick(self, measure: bool = True) -> None:
        """Unclocked (checks): mix one block now from what the sounds make now, as Mixer.tick does."""
        self._feed(1)
        self._engine.tick(1)
        self._fed += 1
        self._drain()

    def _reconcile(self, sources: list, heard: dict) -> list:
        """Every sound known to the engine with its settings now; gone ones let go of."""
        out = []
        alive = set()
        for source in sources:
            key = id(source)
            alive.add(key)
            sound = self._known.get(key)
            settings = self._settings(source)
            if sound is None:
                sound = self._known[key] = _Sound(source, self._engine.add_source(**settings))
                sound.settings = tuple(settings.items())
            elif sound.settings != tuple(settings.items()):
                self._engine.set_source(sound.id, **settings)
                sound.settings = tuple(settings.items())
            self._link(source, sound, heard)
            out.append((source, sound))
        for key in [k for k in self._known if k not in alive]:
            sound = self._known.pop(key)
            if sound.media is not None:
                self._engine.link_media(sound.id, None)
            self._engine.remove_source(sound.id)
            SOUND_LEVELS.pop(getattr(sound.obj, "id", None), None)
        return out

    def _settings(self, source) -> dict:
        settings = getattr(source, "settings", {}) or {}
        try:
            gain = float(source.gain)
        except Exception as exc:  # noqa: BLE001 (a plugin's sound: quiet rather than the whole mix)
            self._problem(f"source {getattr(source, 'id', '?')} volume", exc)
            gain = 0.0
        tracks = list(source_tracks(source))
        if "recording" in tracks and getattr(source, "id", None) not in self.clean_out:
            tracks.append("clean")
        return {"gain": gain, "mono": bool(settings.get("mono")),
                "balance": float(settings.get("balance", 0.0) or 0.0),
                "delay_ms": int(settings.get("delay_ms", 0) or 0), "tracks": tuple(tracks),
                "monitor": is_monitored(source) or is_monitor_only(source), "voice": is_voice(source),
                "bloom_out": getattr(source, "id", None) in self.bloom_out}

    def _link(self, source, sound: _Sound, heard: dict) -> None:
        """A sound the engine plays itself, with no filters and no plugin listening: straight from its player."""
        player = None
        get = getattr(source, "native_sound", None)
        if get is not None and getattr(source, "audio_chain", None) is None \
                and getattr(source, "id", None) not in heard:
            try:
                player = get()
            except Exception as exc:  # noqa: BLE001 (pushed from Python instead)
                self._problem(f"source {getattr(source, 'id', '?')} player", exc)
        asleep = bool(getattr(source, "asleep", False))
        if player is sound.media and (player is None or sound.asleep == asleep):
            return
        try:
            self._engine.link_media(sound.id, player, asleep)
        except sprigcore.NativeError:  # closed meanwhile: pushed from Python until it has another
            player = None
            self._engine.link_media(sound.id, None)
        if player is not None and player is not sound.media:
            player.before_close(lambda engine=self._engine, sid=sound.id: _unlink(engine, sid))
        sound.media = player
        sound.asleep = asleep

    def _push(self, source, sound: _Sound, heard: dict) -> None:
        try:
            block = source.read_block(BLOCK)
            if getattr(block, "shape", None) != (BLOCK, 2):
                block = np.asarray(block, dtype=np.float32).reshape(BLOCK, 2)
        except Exception as exc:  # noqa: BLE001 (silent this block)
            self._problem(f"source {getattr(source, 'id', '?')}", exc)
            block = np.zeros((BLOCK, 2), np.float32)
        chain = getattr(source, "audio_chain", None)
        if chain is not None:
            try:
                block = chain.process(block)
            except Exception:
                log.exception("audio filters on %s failed; turning them off", getattr(source, "id", "?"))
                source.audio_chain = None
        sid = getattr(source, "id", None)
        if heard and sid in heard:
            gain = dict(sound.settings or ()).get("gain", 1.0)
            listen.hand(heard[sid], block * np.float32(gain) if gain != 1.0 else block)
        self._engine.push(sound.id, block)

    def _send_ducking(self) -> None:
        d = self.ducking
        if d != self._ducking_sent:
            self._engine.set_ducking(d.enabled, d.amount_db, d.threshold_db, d.attack_ms, d.release_ms, d.hold_ms)
            self._ducking_sent = d

    def _sync_taps(self) -> None:
        """The engine's taps for the outputs there are now (before the block they should get is mixed)."""
        with self._lock:
            taps, monitor_taps = self._taps, self._monitor_taps
        wanted = {(cb, tracks): [sprigcore.MIXER_TRACKS.index(t) for t in tracks] for cb, tracks in taps}
        wanted.update({(cb, ("monitor",)): [sprigcore.MIXER_MONITOR] for cb in monitor_taps})
        for key in [k for k in self._native_taps if k not in wanted]:
            self._engine.remove_tap(self._native_taps.pop(key)[0])
        for key, what in wanted.items():
            if key not in self._native_taps:
                self._native_taps[key] = (self._engine.add_tap(what), len(what))
        mix_heard = bool(listen.LISTENERS.get(listen.MIX))
        tap = self._listen_taps.get(listen.MIX)
        if mix_heard and tap is None:
            self._listen_taps[listen.MIX] = self._engine.add_tap([0])
        elif not mix_heard and tap is not None:
            self._engine.remove_tap(self._listen_taps.pop(listen.MIX))

    def _hand_out(self, heard: dict) -> None:
        for key, (tap, count) in list(self._native_taps.items()):
            while (got := self._engine.take(tap, count)) is not None:
                _index, data, _dropped = got
                callback, tracks = key
                try:
                    callback(data[0] if count == 1 else tuple(data[i] for i in range(count)))
                except Exception as exc:  # noqa: BLE001 - one output's problem: the others still get this block
                    self._problem("monitoring" if tracks == ("monitor",) else
                                  f"output {getattr(callback, '__qualname__', '?')}", exc)
        tap = self._listen_taps.get(listen.MIX)
        mix_heard = heard.get(listen.MIX) if heard else None
        if tap is not None:
            while (got := self._engine.take(tap, 1)) is not None:
                if mix_heard:
                    listen.hand(mix_heard, got[1][0])

    def _levels(self, sounds) -> None:
        meters = {}
        followed = self.followed
        for source, sound in sounds:
            peak, rms, level, _underruns = self._engine.levels(sound.id)
            sid = getattr(source, "id", None)
            if sid is None:
                continue
            meters[sid] = (peak, rms)
            if sid in followed:
                SOUND_LEVELS[sid] = level
        peak, rms, duck, _ = self._engine.levels(sprigcore.MIXER_MASTER)
        meters["master"] = (peak, rms)
        duck_db = to_db(duck)
        meters["duck"] = (duck_db, duck_db)
        self._meters = meters
        try:
            self.report(meters)
        except Exception as exc:  # noqa: BLE001
            self._problem("levels", exc)

    def _let_go_of_players(self) -> None:
        for sound in self._known.values():
            if sound.media is not None:
                try:
                    self._engine.link_media(sound.id, None)
                except Exception:  # noqa: BLE001, S110 - closed already
                    pass
                sound.media = None


def _unlink(engine, sound_id: int) -> None:
    """A player is about to be freed: the mixer stops reading it (MediaPlayer.before_close)."""
    try:
        engine.link_media(sound_id, None)
    except Exception:  # noqa: BLE001, S110 - the mixer or the sound is gone already
        pass


__all__ = ["FLOOR_DB", "NativeMixer", "make_mixer", "wanted"]
