"""Microphone and desktop-audio sources through WASAPI (the PyAudioWPatch package).

Desktop sound can also come from a sound mixer app's own outputs. Elgato Wave Link, for example,
adds a playback device per channel ("Game (Elgato Virtual Audio)", Music, System...: captured like
speakers; older versions say "Wave Link Game") and may list its mixes as recording devices: those
are listed with desktop sound too, first, and a desktop sound set to one records from it directly.

Each source converts whatever its device delivers (mono, 5.1, 44.1 kHz, ...) to 48 kHz stereo
float32 in the device callback, and keeps it in a short buffer the mixer reads from.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
CHANNELS = 2
MAX_BUFFER = SAMPLE_RATE // 5  # keep at most 200 ms queued, so latency never builds up
RETRY_SECONDS = 2.0
DEFAULT_CHECK_S = 2.0  # how often a "Default" sound asks Windows whether its default device changed
DEFAULT = "default"  # follow the Windows default device


@dataclass(frozen=True)
class AudioDevice:
    name: str
    loopback: bool  # True = an output device captured as "what you hear"

    @property
    def mixer_app(self) -> bool:
        """One of a sound mixer app's devices (Elgato Wave Link)."""
        return is_mixer_app_device(self.name)


_MIXER_APP = re.compile(r"wave ?link|elgato virtual", re.IGNORECASE)  # never a plain Elgato mic


def is_mixer_app_device(name: str) -> bool:
    return bool(_MIXER_APP.search(name or ""))


_ELGATO_NAME = re.compile(r"^\s*(?:wave ?link\s+)?(?P<channel>.+?)\s*\((?:elgato virtual audio|elgato[^)]*)\)\s*$",
                          re.IGNORECASE)


def device_label(name: str) -> str:
    """How a device reads in pickers: Elgato Wave Link channels as "Game · Elgato Wave Link", and
    other apps' devices with their app ("… · Voicemod") when the name doesn't say."""
    if not is_mixer_app_device(name):
        from sprigcast.core.device_makers import label

        return label(name)
    match = _ELGATO_NAME.match(name)
    channel = match.group("channel") if match else re.sub(r"(?i)^wave ?link\s+", "", name)
    return f"{channel} · Elgato Wave Link"


def _pyaudio():
    try:
        import pyaudiowpatch
    except ImportError as exc:
        from sprigcast.core import systems

        if systems.is_linux() or systems.is_mac():
            raise RuntimeError(systems.not_yet("Microphone and desktop sound")) from exc
        raise RuntimeError("unavailable: needs Windows and PyAudioWPatch") from exc
    return pyaudiowpatch


class PortAudio:
    """The one PyAudio instance every audio source shares.

    PortAudio's initialise and terminate are global and not thread-safe: creating or terminating
    a PyAudio instance on one thread while another source's stream is running can crash the whole
    process. So all PortAudio calls go through this lock, and the instance is only re-created (to
    see devices plugged in later) while no stream is open.
    """

    lock = threading.RLock()
    _mod = None
    _pa = None
    _open_streams = 0

    @classmethod
    def get(cls, pa_mod, rescan: bool = False):
        """Call with `lock` held."""
        if cls._pa is not None and cls._mod is pa_mod and not (rescan and cls._open_streams == 0):
            return cls._pa
        if cls._pa is not None and cls._mod is pa_mod and cls._open_streams:
            return cls._pa  # streams are running: keep the current device list rather than risk a crash
        if cls._pa is not None:
            try:
                cls._pa.terminate()
            except Exception:
                pass
        cls._mod, cls._pa, cls._open_streams = pa_mod, pa_mod.PyAudio(), 0
        return cls._pa

    @classmethod
    def opened(cls) -> None:
        cls._open_streams += 1

    @classmethod
    def closed(cls) -> None:
        cls._open_streams = max(0, cls._open_streams - 1)


def list_audio_devices(loopback: bool, rescan: bool = True) -> list[AudioDevice]:
    """WASAPI microphones (loopback=False) or desktop sound to capture (loopback=True).

    Desktop sound is every output device ("what you hear"), plus a mixer app's mixes, which Windows
    lists as recording devices (Wave Link Stream...). Mixer app devices come first.
    rescan=False reuses the last scan: a rescan restarts PortAudio (a few hundred ms on Windows)."""
    try:
        pa_mod = _pyaudio()
    except RuntimeError:
        return []
    with PortAudio.lock:
        pa = PortAudio.get(pa_mod, rescan=rescan)
        devices: dict[str, AudioDevice] = {}
        for info in pa.get_device_info_generator_by_host_api(host_api_type=pa_mod.paWASAPI):
            is_loop = bool(info.get("isLoopbackDevice"))
            name = str(info["name"]).replace(" [Loopback]", "")
            if loopback and is_loop:
                devices.setdefault(name, AudioDevice(name, True))
            elif not is_loop and info["maxInputChannels"] > 0 and (not loopback or is_mixer_app_device(name)):
                devices.setdefault(name, AudioDevice(name, False))
        found = list(devices.values())
        return sorted(found, key=lambda d: not d.mixer_app) if loopback else found


def list_playback_devices(rescan: bool = False) -> list[str]:
    """Speakers, headphones and virtual cables SprigCast can play to (WASAPI outputs), by name."""
    try:
        pa_mod = _pyaudio()
    except RuntimeError:
        return []
    with PortAudio.lock:
        pa = PortAudio.get(pa_mod, rescan=rescan)
        names: list[str] = []
        for info in pa.get_device_info_generator_by_host_api(host_api_type=pa_mod.paWASAPI):
            name = str(info["name"])
            if not info.get("isLoopbackDevice") and info["maxOutputChannels"] > 0 and name not in names:
                names.append(name)
        return names


def to_stereo(samples: np.ndarray) -> np.ndarray:
    """(N, C) float32 -> (N, 2): mono is duplicated, surround keeps front left and right."""
    if samples.ndim == 1:
        samples = samples[:, None]
    if samples.shape[1] == 1:
        return np.repeat(samples, 2, axis=1)
    return samples[:, :2]


class AudioRing:
    """Thread-safe FIFO of stereo samples with a hard cap (oldest samples are dropped)."""

    def __init__(self, capacity: int = MAX_BUFFER):
        self.capacity = capacity
        self._chunks: deque[np.ndarray] = deque()
        self._count = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return self._count

    def write(self, samples: np.ndarray) -> None:
        if not len(samples):
            return
        with self._lock:
            self._chunks.append(samples)
            self._count += len(samples)
            while self._count > self.capacity:
                excess = self._count - self.capacity
                head = self._chunks[0]
                if len(head) <= excess:
                    self._chunks.popleft()
                    self._count -= len(head)
                else:
                    self._chunks[0] = head[excess:]
                    self._count -= excess

    def read(self, n: int) -> np.ndarray:
        """Exactly n frames; silence fills any shortfall (nothing playing, device starting)."""
        out = np.zeros((n, CHANNELS), dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < n and self._chunks:
                head = self._chunks[0]
                take = min(n - filled, len(head))
                out[filled : filled + take] = head[:take]
                filled += take
                self._count -= take
                if take == len(head):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = head[take:]
        return out


class _WasapiSource(Source):
    """Base for audio sources. Settings: device (name or "default"), volume_db, muted."""

    loopback = False

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self.ring = AudioRing()
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._thread: threading.Thread | None = None
        self._engine_has_it = False  # the engine's mixer reads the device itself (audio/native_mixer.py)
        self.status = "starting"

    @property
    def size(self) -> tuple[int, int]:
        return (0, 0)  # audio only

    @property
    def gain(self) -> float:
        if self.settings.get("muted"):
            return 0.0
        return float(10 ** (float(self.settings.get("volume_db", 0.0)) / 20))

    def read_block(self, n: int) -> np.ndarray:
        return self.ring.read(n)

    def native_capture(self) -> tuple[str, bool]:
        """(device name or "" for the Windows default, desktop sound?) for the engine to capture itself."""
        device = str(self.settings.get("device", DEFAULT))
        return ("" if device == DEFAULT else device, self.loopback)

    def engine_captures(self, on: bool) -> None:
        """The engine reads the device itself (on): this source's own capture closes, and opens again when off."""
        if on != self._engine_has_it:
            self._engine_has_it = on
            log.info("audio %s: %s", self.id, "the engine reads this device; Python's capture of it closes" if on
                     else "Python captures this device again")
            self._restart.set()
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"audio-{self.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._restart.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def update(self, settings: dict[str, Any]) -> None:
        device_changed = settings.get("device") != self.settings.get("device")
        super().update(settings)  # volume and mute apply immediately through `gain`
        if device_changed:
            self._restart.set()

    def _windows_default(self) -> tuple[str, str] | None:
        """(ID, name) of the device Windows uses as the default now, when this sound follows the default."""
        if str(self.settings.get("device", DEFAULT)) != DEFAULT:
            return None
        from sprigcast.audio import default_device

        try:
            return default_device.default_endpoint(render=self.loopback)
        except Exception as exc:  # noqa: BLE001 (asking Windows must never stop the sound)
            log.debug("%s: default device check failed: %s", self.id, exc)
            return None

    def _find(self, pa, pa_mod) -> dict | None:
        wanted = str(self.settings.get("device", DEFAULT))
        if wanted == DEFAULT:
            # PortAudio only knows the default from when it started; Windows says what it is now.
            now = self._windows_default()
            if now and now[1]:
                for info in pa.get_device_info_generator_by_host_api(host_api_type=pa_mod.paWASAPI):
                    name = str(info["name"]).replace(" [Loopback]", "")
                    if name == now[1] and bool(info.get("isLoopbackDevice")) == self.loopback:
                        return info
            return pa.get_default_wasapi_loopback() if self.loopback else pa.get_default_wasapi_device(d_in=True)
        fallback = None
        for info in pa.get_device_info_generator_by_host_api(host_api_type=pa_mod.paWASAPI):
            name = str(info["name"]).replace(" [Loopback]", "")
            if name != wanted:
                continue
            if bool(info.get("isLoopbackDevice")) == self.loopback:
                return info
            if self.loopback and not info.get("isLoopbackDevice") and info["maxInputChannels"] > 0:
                fallback = info  # desktop sound from a mixer app's mix (a recording device, e.g. Wave Link Stream)
        return fallback

    def _run(self) -> None:
        try:
            pa_mod = _pyaudio()
        except RuntimeError as exc:
            self.status = str(exc)
            return
        import soxr

        rescan = False
        while not self._stop.is_set():
            self._restart.clear()
            if self._engine_has_it:  # the engine captures it: nothing open here until it hands it back
                self.ring.read(len(self.ring))
                self._restart.wait()
                continue
            stream = None
            try:
                with PortAudio.lock:
                    pa = PortAudio.get(pa_mod, rescan=rescan)
                    info = self._find(pa, pa_mod)
                    if info is not None:
                        stream, channels = self._open(pa, pa_mod, info, soxr)
                        PortAudio.opened()
                if info is None:
                    self.status = "device not found, retrying"
                    rescan = True
                    self._restart.wait(RETRY_SECONDS)
                    continue
                rescan = False
                self.status = f"capturing {str(info['name']).replace(' [Loopback]', '')}"
                if channels > 2:
                    self.status += f" ({channels} channels mixed to stereo)"
                following = self._windows_default()
                checked = time.monotonic()
                while not (self._stop.is_set() or self._restart.is_set()):
                    if not stream.is_active():
                        self.status = "device lost, retrying"
                        rescan = True
                        break
                    if following is not None and time.monotonic() - checked >= DEFAULT_CHECK_S:
                        checked = time.monotonic()
                        now = self._windows_default()
                        if now is not None and now[0] != following[0]:  # Windows switched its default device
                            log.info("%s: Windows default changed to %s, following it", self.id, now[1])
                            rescan = True
                            self._restart.set()  # straight over, no retry pause
                            break
                    self._restart.wait(0.5)
            except Exception as exc:  # unplugged mid-open, exclusive mode, unsupported format, driver errors
                self.status = f"error: {exc}"
                log.info("%s: audio failed: %s", self.id, exc)
                rescan = True
            finally:
                if stream is not None:
                    with PortAudio.lock:
                        try:
                            stream.stop_stream()
                            stream.close()
                        except Exception:
                            pass
                        PortAudio.closed()
            if not (self._stop.is_set() or self._restart.is_set()):
                self._restart.wait(RETRY_SECONDS)

    def _open(self, pa, pa_mod, info: dict, soxr):
        """Open a capture stream, trying the device's own channel count first, then stereo, then mono.

        Some drivers (surround and virtual-surround headsets, Bluetooth hands-free) refuse the channel
        count they advertise, which PortAudio reports as "Invalid number of channels".
        """
        rate = int(info["defaultSampleRate"])
        advertised = int(info["maxInputChannels"])
        candidates = [c for c in dict.fromkeys([advertised, 2, 1]) if c > 0]
        last_error: Exception | None = None
        for channels in candidates:
            resampler = soxr.ResampleStream(rate, SAMPLE_RATE, CHANNELS, dtype="float32") if rate != SAMPLE_RATE else None
            try:
                stream = pa.open(
                    format=pa_mod.paFloat32,
                    channels=channels,
                    rate=rate,
                    input=True,
                    input_device_index=int(info["index"]),
                    frames_per_buffer=rate // 100,  # 10 ms
                    stream_callback=self._callback(pa_mod, channels, resampler),
                )
                return stream, channels
            except Exception as exc:
                log.info("%s: %d channels at %d Hz refused: %s", self.id, channels, rate, exc)
                last_error = exc
        raise last_error or RuntimeError("device has no input channels")

    def _callback(self, pa_mod, channels: int, resampler):
        def callback(in_data, frame_count, time_info, status_flags):
            try:
                samples = np.frombuffer(in_data, dtype=np.float32).reshape(-1, channels)
                samples = to_stereo(samples) if channels != 2 else samples
                if resampler is not None:
                    samples = resampler.resample_chunk(np.ascontiguousarray(samples))
                self.ring.write(np.ascontiguousarray(samples, dtype=np.float32))
            except Exception as exc:  # never let an exception escape into PortAudio's thread
                log.warning("%s: dropped audio: %s", self.id, exc)
            return (None, pa_mod.paContinue)

        return callback


@register("audio_input")
class MicSource(_WasapiSource):
    loopback = False


def leaves_out_sprigcast(settings: dict[str, Any]) -> bool:
    """Desktop sound on the Windows default: captured as every app's sound except SprigCast's own, so what you
    listen to through SprigCast never comes back into the stream (user 2026-11-20: it played SprigCast back into
    itself). Needs Windows 10 2004+; SPRIGCAST_PLAIN_DESKTOP_SOUND=1 goes back to plain loopback."""
    import os
    import sys

    if sys.platform != "win32" or os.environ.get("SPRIGCAST_PLAIN_DESKTOP_SOUND"):
        return False
    from sprigcast.winver import windows_build

    return str(settings.get("device", DEFAULT)) == DEFAULT and windows_build() >= 19041


@register("audio_output")
class DesktopAudioSource(_WasapiSource):
    loopback = True

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self.leaves_out_sprigcast = leaves_out_sprigcast(settings)  # read by audio/native_mixer.py
        self._others = None  # the open "everything but SprigCast" capture (sources/app_audio._ProcessCapture)

    def read_block(self, n: int) -> np.ndarray:
        if self.leaves_out_sprigcast:
            others = self._others
            return others.ring.read(n) if others is not None else np.zeros((n, CHANNELS), dtype=np.float32)
        return super().read_block(n)

    def needs_new(self, settings: dict[str, Any]) -> bool:
        """To or from the Windows default: a new source opens in the other way of capturing."""
        return leaves_out_sprigcast(settings) != self.leaves_out_sprigcast

    def _run(self) -> None:
        if not self.leaves_out_sprigcast:
            super()._run()
            return
        import os

        from sprigcast.sources import app_audio

        app_audio._com_init()
        wait = RETRY_SECONDS
        try:
            while not self._stop.is_set():
                others = self._others
                if others is None or not others.alive:
                    if others is not None:
                        others.close()
                        self._others = None
                    try:
                        self._others = app_audio._ProcessCapture(os.getpid(), "everything but SprigCast", self.id,
                                                                 exclude=True)
                        self.status = "capturing everything but SprigCast"
                        wait = RETRY_SECONDS
                    except Exception as exc:  # noqa: BLE001 - Windows refused: plain loopback instead
                        log.info("audio %s: can't leave SprigCast out (%s); plain desktop sound", self.id, exc)
                        self.leaves_out_sprigcast = False
                        super()._run()
                        return
                self._stop.wait(wait)
        finally:
            others, self._others = self._others, None
            if others is not None:
                others.close()
