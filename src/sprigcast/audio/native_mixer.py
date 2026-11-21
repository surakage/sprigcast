"""The engine's sound mixer driven like audio/mixer.py's Mixer (docs/native-engine.md "Sound").

With the native engine (SPRIGCAST_ENGINE=native) the mix itself is made in C++ (sprigcore.SoundMixer) on a thread of
the engine's own, paced by the clock, so a busy Python (the GIL) never makes the sound stutter: per sound mono,
balance, delay, the fade of a sound per bloom, volume, ducking, every named track, the clean track and the monitor
mix. Sounds the engine plays itself (video and sound files) go straight from their player into the mix; the others
are still made in Python and pushed in by the feeder thread here, one block ahead, with their filters applied. The
feeder also hands the finished blocks to each output and the levels to the meters.

Microphones and desktop sounds with no filters are read by the engine too (sprigcore.SoundCapture, WASAPI on a
thread of its own pushing straight into the mix); one whose device the engine can't find goes back to Python's
capture.

Built-in filters (gain, noise gate, compressor, limiter, invert polarity, ducking) run in the mix too; a chain with
any other filter stays in Python. Monitoring and "Sound out" to a device are played by the engine
(sprigcore.SoundPlayback, WASAPI on its own thread) straight from the mix: EngineMonitor. Audio visualizers hear
and draw in the engine too (sprigcore.SoundVisual, NativeMixer.visual): their sound never passes through Python.

SPRIGCAST_NO_NATIVE_MIXER=1 keeps Python's mixer; SPRIGCAST_NO_NATIVE_CAPTURE=1 keeps Python's capture;
SPRIGCAST_NO_NATIVE_FILTERS=1 runs every filter chain in Python; SPRIGCAST_NO_NATIVE_MONITOR=1 plays monitoring
from Python; SPRIGCAST_NO_NATIVE_VISUALIZER=1 draws audio visualizers in Python.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import weakref
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

CAPTURE_GIVE_UP_S = 4.0  # a capture that never found its device by then goes back to Python's
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


def make_monitor(mixer, device: str | None = None):
    """What plays monitoring (device None: the Windows default) or a mix on a named device: the engine itself when
    it mixes, else audio/monitor.py's Monitor. Same calls either way."""
    from sprigcast.audio.monitor import Monitor

    if getattr(mixer, "native", False) and not os.environ.get("SPRIGCAST_NO_NATIVE_MONITOR"):
        return EngineMonitor(mixer, device)
    return Monitor(device)


class EngineMonitor:
    """audio/monitor.py's Monitor, played by the engine. Handed to NativeMixer.add_monitor_tap or add_tap like a
    Monitor's write; the mixer then has the engine play that mix on the device instead of handing blocks to
    Python. If the engine can't, a Python Monitor plays it (write goes there)."""

    def __init__(self, mixer, device: str | None = None):
        self.device = device
        self._mixer = mixer
        self._playback = None  # sprigcore.SoundPlayback while attached
        self._python = None  # audio/monitor.py's Monitor, if the engine couldn't
        self._started = False
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._started

    @property
    def native(self) -> bool:
        return self._playback is not None

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False
        self.detach()

    def attach(self, what: int) -> bool:
        """Play mix `what` (sprigcore.MIXER_MONITOR or a track index) in the engine. False: Python plays it."""
        with self._lock:
            if self._playback is not None or self._python is not None:
                return self._playback is not None
            try:
                self._playback = self._mixer._engine.play(what, self.device or "")
                return True
            except Exception as exc:  # noqa: BLE001 (Python plays it)
                log.warning("monitor: the engine can't play it (%s); Python plays it", exc)
                from sprigcast.audio.monitor import Monitor

                self._python = Monitor(self.device)
                self._python.start()
                return False

    def detach(self) -> None:
        with self._lock:
            playback, python, self._playback, self._python = self._playback, self._python, None, None
        if playback is not None:
            playback.close()
        if python is not None:
            python.stop()

    def write(self, block: np.ndarray) -> None:
        python = self._python
        if python is not None:
            python.write(block)

    @property
    def status(self) -> str:
        """As Monitor's: "off", "playing on <device>", or what is wrong."""
        python, playback = self._python, self._playback
        if python is not None:
            return python.status
        if playback is None:
            return "off"
        try:
            st = playback.stats()
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"
        if st["state"] == "playing":
            return f"playing on {st['device']}"
        if st["state"] == "starting":
            return "starting"
        return f"error: {st['error'] or st['state']}"


_CURRENT: Callable[[], NativeMixer | None] = lambda: None


def current() -> NativeMixer | None:
    """The engine's mixer running now, if sound is mixed by the engine."""
    return _CURRENT()


def make_mixer(report: Callable[[dict[str, tuple[float, float]]], None]):
    """The mixer render/engine.py uses: the engine's when it can, else Python's."""
    from sprigcast.audio.mixer import Mixer

    if wanted():
        try:
            mixer = NativeMixer(report)
            log.info("sound: mixed by the engine (its own thread); SPRIGCAST_NO_NATIVE_MIXER=1 mixes in Python")
            return mixer
        except Exception as exc:  # noqa: BLE001 (Python's mixer instead)
            log.warning("sound: the engine's mixer isn't available (%s); Python mixes", exc)
    return Mixer(report)


def _number(settings: dict, key: str) -> float:
    return float(settings[key])


def engine_filters(chain, engine_ids: dict) -> tuple | None:
    """A chain's filters as the engine runs them (sprigcore.SoundMixer.set_filters), or None if any of them is one
    only Python has. Settings are read each time: AudioChain.update() swaps them in place."""
    from sprigcast.core import filters as f

    processors = getattr(chain, "processors", None)
    if processors is None:
        return None
    out = []
    for proc in processors:
        s, kind = proc.settings, type(proc)
        if kind is f.GainFilter:
            out.append((sprigcore.FILTER_GAIN, 0, (_number(s, "db"),)))
        elif kind is f.NoiseGate:
            out.append((sprigcore.FILTER_NOISE_GATE, 0, tuple(_number(s, k) for k in (
                "close_db", "open_db", "attack_ms", "hold_ms", "release_ms"))))
        elif kind is f.Limiter:  # before Compressor: a Limiter is one
            out.append((sprigcore.FILTER_LIMITER, 0, (_number(s, "threshold_db"), _number(s, "release_ms"))))
        elif kind is f.Compressor:
            out.append((sprigcore.FILTER_COMPRESSOR, 0, tuple(_number(s, k) for k in (
                "ratio", "threshold_db", "attack_ms", "release_ms", "output_db"))))
        elif kind is f.InvertPolarity:
            out.append((sprigcore.FILTER_INVERT, 0, ()))
        elif kind is f.Ducking:
            follow = engine_ids.get(str(s["sound_id"]), 0) if s["sound_id"] else 0
            out.append((sprigcore.FILTER_DUCKING, follow, tuple(_number(s, k) for k in (
                "amount_db", "threshold_db", "attack_ms", "hold_ms", "release_ms"))))
        else:
            return None
    return tuple(out)


def _python_only(source) -> list[str]:
    """The kinds of a sound's filters that only Python runs (for the log)."""
    from sprigcast.core import filters as f

    native = (f.GainFilter, f.NoiseGate, f.Compressor, f.Limiter, f.InvertPolarity, f.Ducking)
    chain = getattr(source, "audio_chain", None)
    names = [type(p).__name__ for p in getattr(chain, "processors", ()) if type(p) not in native]
    return names or ["unknown"]


def _python_chain(source, sound) -> object | None:
    """The filters Python runs on this sound: none when the engine runs them."""
    return None if sound.filters is not None else getattr(source, "audio_chain", None)


class _Sound:
    """One sound as the engine knows it."""

    __slots__ = ("asleep", "capture", "capture_key", "capture_since", "capture_said", "filters", "id", "media", "obj",
                 "refused", "settings")

    def __init__(self, obj, native_id: int):
        self.obj = obj
        self.id = native_id
        self.settings: tuple | None = None
        self.media = None  # the engine player it reads straight from, if any
        self.asleep: bool | None = None  # what the player was last told
        self.capture = None  # the engine's capture of its device, if any (sprigcore.SoundCapture)
        self.capture_key: tuple | None = None  # (device, loopback) it captures
        self.capture_since = 0.0
        self.refused: tuple | None = None  # a (device, loopback) the engine couldn't find: Python captures it
        self.capture_said = ""  # why Python or the engine captures it, as last logged
        self.filters: tuple | None = None  # the built-in filters the engine runs on it (None: Python runs its chain)


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
        self._push_wanted: tuple[str, str, int] = ("", "off", 0)  # push to talk: (source id, mode, release ms)
        self._push_sent: tuple | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sprigcast-audio", daemon=True)
        self._said: dict[str, tuple[str, float]] = {}
        self._meters: dict[str, tuple[float, float]] = {}
        self._fed = -1  # the last block number pushed for
        self._sounds: list = []  # (source, _Sound) as last fed
        self._visuals: dict = {}  # SoundVisual -> the sound it hears (a source id or listen.MIX); swapped whole
        self.ticks = 0
        global _CURRENT
        _CURRENT = weakref.ref(self)

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

    def set_push(self, source_id: str, mode: str, release_ms: int) -> None:
        """As Mixer.set_push: sent to the engine with the sound's id there on the next block."""
        self._push_wanted = (source_id or "", mode if mode in ("talk", "mute") else "off", int(release_ms))

    def push_key(self, down: bool) -> None:
        self._engine.push_key(bool(down))

    def add_extra(self, player) -> None:
        with self._lock:
            if player not in self._extras:
                self._extras = [*self._extras, player]

    def remove_extra(self, player) -> None:
        with self._lock:
            self._extras = [p for p in self._extras if p is not player]

    def visual(self, sound: str):
        """An audio visualizer drawn by the engine, hearing `sound` (a source id or listen.MIX). It starts hearing
        on the mixer's next block (the feeder points it at the sound, and again whenever that sound comes back)."""
        made = self._engine.visual(-1)
        with self._lock:
            self._visuals = {**self._visuals, made: str(sound)}
        return made

    def visual_hears(self, visual, sound: str) -> None:
        with self._lock:
            if visual in self._visuals:
                self._visuals = {**self._visuals, visual: str(sound)}

    def drop_visual(self, visual) -> None:
        with self._lock:
            self._visuals = {v: s for v, s in self._visuals.items() if v is not visual}
        visual.close()

    def same_mix(self, a: str, b: str) -> bool:
        with self._lock:
            sources = self._sources + self._extras
        return all((a in source_tracks(s)) == (b in source_tracks(s)) for s in sources)

    def add_tap(self, callback: Callable, tracks: tuple[str, ...] = ("stream",)) -> None:
        owner = getattr(callback, "__self__", None)
        if isinstance(owner, EngineMonitor) and len(tracks) == 1 and tracks[0] in sprigcore.MIXER_TRACKS \
                and owner.attach(sprigcore.MIXER_TRACKS.index(tracks[0])):
            return  # the engine plays it on the device
        with self._lock:
            self._taps = [*self._taps, (callback, tuple(tracks))]

    def remove_tap(self, callback: Callable) -> None:
        owner = getattr(callback, "__self__", None)
        if isinstance(owner, EngineMonitor):
            owner.detach()
        with self._lock:
            self._taps = [t for t in self._taps if t[0] != callback]

    def set_ducking(self, ducking: Ducking) -> None:
        self.ducking = ducking

    def add_monitor_tap(self, callback: Callable[[np.ndarray], None]) -> None:
        owner = getattr(callback, "__self__", None)
        if isinstance(owner, EngineMonitor) and owner.attach(sprigcore.MIXER_MONITOR):
            return  # the engine plays it on the speakers
        with self._lock:
            self._monitor_taps = [*self._monitor_taps, callback]

    def remove_monitor_tap(self, callback: Callable[[np.ndarray], None]) -> None:
        owner = getattr(callback, "__self__", None)
        if isinstance(owner, EngineMonitor):
            owner.detach()
        with self._lock:
            self._monitor_taps = [t for t in self._monitor_taps if t != callback]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        global _CURRENT
        if _CURRENT() is self:
            _CURRENT = lambda: None
        self._stop.set()
        if self._thread.ident is not None:  # never started when ticked by hand (checks)
            self._thread.join(timeout=2)
        self._let_go_of_players()
        for sound in list(self._known.values()):
            self._end_capture(sound, hand_back=True)  # Python's mixer (if any comes next) needs their sound
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
        self._send_push(sounds)
        self._sync_taps()
        for _ in range(blocks):
            for source, sound in sounds:
                if sound.capture is not None:
                    continue  # the engine reads its device itself
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
            self._watch_captures(self._sounds)
            if self.ticks % (LEVELS_EVERY * 8) == 0:
                # the engine's own lines (captures, playback, the mixer's problems) into the log as they happen
                try:
                    sprigcore.drain_log()
                except Exception as exc:  # noqa: BLE001
                    self._problem("the engine's log", exc)

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
            self._filters(source, sound, heard)
            self._link(source, sound, heard)
            self._capture(source, sound, heard)
            out.append((source, sound))
        for key in [k for k in self._known if k not in alive]:
            sound = self._known.pop(key)
            self._end_capture(sound, hand_back=True)
            if sound.media is not None:
                self._engine.link_media(sound.id, None)
                sound.media = None  # its player closing later has nothing left to let go of
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

    def _filters(self, source, sound: _Sound, heard: dict) -> None:
        """Built-in filters the engine has (gain, noise gate, compressor, limiter, invert, ducking) run there; a chain
        with any other filter, or a sound a plugin listens to (it hears the filtered sound), stays in Python."""
        chain = getattr(source, "audio_chain", None)
        wanted = None
        if chain is not None and not os.environ.get("SPRIGCAST_NO_NATIVE_FILTERS") \
                and getattr(source, "id", None) not in heard:
            try:
                wanted = engine_filters(chain, self._engine_ids())
            except Exception as exc:  # noqa: BLE001 (Python runs them)
                self._problem(f"source {getattr(source, 'id', '?')} filter settings", exc)
        if wanted == sound.filters:
            return
        try:
            self._engine.set_filters(sound.id, list(wanted or ()))
        except Exception as exc:  # noqa: BLE001 (Python runs them)
            self._problem(f"source {getattr(source, 'id', '?')} filters", exc)
            wanted = None
            try:
                self._engine.set_filters(sound.id, [])
            except Exception:  # noqa: BLE001
                pass
        sound.filters = wanted

    def _engine_ids(self) -> dict:
        """Source id -> its sound in the engine (a ducking filter follows it there)."""
        ids = {}
        for sound in self._known.values():
            sid = getattr(sound.obj, "id", None)
            if sid is not None:
                ids[str(sid)] = sound.id
        return ids

    def _link(self, source, sound: _Sound, heard: dict) -> None:
        """A sound the engine plays itself, with no filters and no plugin listening: straight from its player."""
        player = None
        get = getattr(source, "native_sound", None)
        if get is not None and _python_chain(source, sound) is None and getattr(source, "id", None) not in heard:
            try:
                player = get()
            except Exception as exc:  # noqa: BLE001 (pushed from Python instead)
                self._problem(f"source {getattr(source, 'id', '?')} player", exc)
        asleep = bool(getattr(source, "asleep", False))
        if player is sound.media and (player is None or sound.asleep == asleep):
            return
        new = player is not None and player is not sound.media
        sound.media, sound.asleep = player, asleep  # before linking: a close from now on finds it linked
        try:
            self._engine.link_media(sound.id, player, asleep)
        except sprigcore.NativeError:  # closed meanwhile: pushed from Python until it has another
            sound.media, new = None, False
            self._engine.link_media(sound.id, None)
        if new:
            player.before_close(lambda engine=self._engine, s=sound, p=player: _unlink(engine, s, p))

    def _capture(self, source, sound: _Sound, heard: dict) -> None:
        """A microphone or desktop sound with no Python-only filters and no plugin listening: the engine reads its
        device. Which one reads it, and why, is logged each time that changes."""
        get = getattr(source, "native_capture", None)
        if get is None:
            return
        sid = getattr(source, "id", "?")
        key, why = None, ""
        if os.environ.get("SPRIGCAST_NO_NATIVE_CAPTURE"):
            why = "SPRIGCAST_NO_NATIVE_CAPTURE is set"
        elif _python_chain(source, sound) is not None:
            why = "it has filters only Python has (" + ", ".join(_python_only(source)) + ")"
        elif sid in heard:
            why = "a plugin listens to it"
        elif getattr(source, "leaves_out_sprigcast", False):
            why = "it leaves out SprigCast's own sound"
        else:
            try:
                key = tuple(get())
            except Exception as exc:  # noqa: BLE001 (Python captures it)
                self._problem(f"source {sid} device", exc)
                why = f"its device couldn't be read ({exc})"
        if key is not None and key == sound.refused:
            key, why = None, "the engine couldn't capture its device"
        self._say_capture(sid, sound, f"the engine captures {key[0] or 'the Windows default'}" if key is not None
                          else f"Python captures it: {why}")
        if key == sound.capture_key:
            return
        self._end_capture(sound, hand_back=key is None)
        if key is None:
            return
        try:
            sound.capture = self._engine.capture(sound.id, key[0], key[1])
        except Exception as exc:  # noqa: BLE001 (Python captures it)
            self._problem(f"source {sid} capture", exc)
            sound.refused = key
            source.engine_captures(False)
            self._say_capture(sid, sound, f"Python captures it: the engine couldn't open it ({exc})")
            return
        sound.capture_key, sound.capture_since = key, time.monotonic()
        source.engine_captures(True)

    @staticmethod
    def _say_capture(sid, sound: _Sound, said: str) -> None:
        if said != sound.capture_said:
            sound.capture_said = said
            log.info("sound %s: %s", sid, said)

    def _end_capture(self, sound: _Sound, hand_back: bool) -> None:
        if sound.capture is None:
            return
        capture, sound.capture, sound.capture_key = sound.capture, None, None
        try:
            capture.close()
        except Exception as exc:  # noqa: BLE001 (closed with the mixer)
            self._problem("closing a capture", exc)
        if hand_back:
            give = getattr(sound.obj, "engine_captures", None)
            if give is not None:
                give(False)

    def _watch_captures(self, sounds) -> None:
        """Now and then: each engine capture's state on its source; one that never found its device goes back to
        Python's capture (its device list may know a name the engine doesn't)."""
        now = time.monotonic()
        for source, sound in sounds:
            capture = sound.capture
            if capture is None:
                continue
            try:
                st = capture.stats()
            except Exception as exc:  # noqa: BLE001
                self._problem("a capture's state", exc)
                continue
            if st["state"] == "capturing":
                source.status = f"capturing {st['device']}"
                if st["channels"] > 2:
                    source.status += f" ({st['channels']} channels mixed to stereo)"
            elif st["state"] in ("not found", "failed") and not st["frames"] and now - sound.capture_since > \
                    CAPTURE_GIVE_UP_S:
                log.info("sound %s: the engine can't capture it (%s); Python captures it",
                         getattr(source, "id", "?"), st["error"])
                sound.refused = sound.capture_key
                self._end_capture(sound, hand_back=True)
                sound.capture_said = "Python captures it: the engine couldn't capture its device"
            elif st["error"]:
                source.status = st["error"]

    def _push(self, source, sound: _Sound, heard: dict) -> None:
        try:
            block = source.read_block(BLOCK)
            if getattr(block, "shape", None) != (BLOCK, 2):
                block = np.asarray(block, dtype=np.float32).reshape(BLOCK, 2)
        except Exception as exc:  # noqa: BLE001 (silent this block)
            self._problem(f"source {getattr(source, 'id', '?')}", exc)
            block = np.zeros((BLOCK, 2), np.float32)
        chain = _python_chain(source, sound)
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

    def _send_push(self, sounds) -> None:
        source_id, mode, release_ms = self._push_wanted
        native = next((sound.id for source, sound in sounds if getattr(source, "id", None) == source_id), 0)
        modes = {"talk": sprigcore.PUSH_TALK, "mute": sprigcore.PUSH_MUTE}
        wanted = (native, modes.get(mode, sprigcore.PUSH_OFF) if native else sprigcore.PUSH_OFF, release_ms)
        if wanted != self._push_sent:
            self._engine.set_push(*wanted)
            self._push_sent = wanted

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
        visuals = self._visuals
        if visuals:
            ids = self._engine_ids()
            for visual, sound in visuals.items():
                what = 0 if sound == listen.MIX else (sprigcore.MIXER_SOURCE_TAP + ids[sound] if sound in ids else -1)
                try:
                    visual.listen(what)
                except Exception as exc:  # noqa: BLE001 - closed meanwhile, or the engine refused: it hears nothing
                    if not visual.closed:
                        self._problem("audio visualizer", exc)
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
        short = {}
        for source, sound in sounds:
            peak, rms, level, underruns = self._engine.levels(sound.id)
            short[getattr(source, "name", None) or getattr(source, "id", "?")] = underruns
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
        self._note_short(short)
        try:
            self.report(meters)
        except Exception as exc:  # noqa: BLE001
            self._problem("levels", exc)

    SHORT_EVERY_S = 30.0  # how often sounds that ran dry are summed up in the log
    SHORT_AT_LEAST = 3  # a sound ran dry at least this often in that time (one now and then is a pause)

    def _note_short(self, totals: dict, now: float | None = None) -> str | None:
        """Sounds that ran dry (the mixer needed their next block and it hadn't come: a gap you hear) are logged
        every SHORT_EVERY_S with how often. Live on the PC (2026-11-21) viewers heard choppy voice and the log
        had nothing about sound."""
        now = time.monotonic() if now is None else now
        seen = self.__dict__.setdefault("_short_seen", {})
        window = self.__dict__.setdefault("_short_window", {})
        for name, count in totals.items():
            before = seen.get(name, count)
            if count > before:
                window[name] = window.get(name, 0) + count - before
            seen[name] = count
        since = self.__dict__.setdefault("_short_since", now)
        if now - since < self.SHORT_EVERY_S:
            return None
        self._short_since = now
        worst = sorted(((n, c) for n, c in window.items() if c >= self.SHORT_AT_LEAST), key=lambda nc: -nc[1])
        window.clear()
        if not worst:
            return None
        line = (f"sound ran dry (gaps you hear) in the last {self.SHORT_EVERY_S:.0f} s: "
                + ", ".join(f"{name} {count} times" for name, count in worst))
        log.warning(line)
        return line

    def _let_go_of_players(self) -> None:
        for sound in self._known.values():
            if sound.media is not None:
                try:
                    self._engine.link_media(sound.id, None)
                except Exception:  # noqa: BLE001, S110 - closed already
                    pass
                sound.media = None


def _unlink(engine, sound: _Sound, player) -> None:
    """A player is about to be freed: the mixer stops reading it (MediaPlayer.before_close). Only while this sound
    still reads that player: once the sound is gone from the mixer (a media leaf added or edited makes its sound
    again) or reads another player, asking logged "sc_mixer_source_media: no such sound" (PC, 2026-11-20)."""
    if sound.media is not player:
        return
    try:
        engine.link_media(sound.id, None)
    except Exception:  # noqa: BLE001, S110 - the mixer is gone already
        pass


__all__ = ["FLOOR_DB", "NativeMixer", "current", "make_mixer", "wanted"]
