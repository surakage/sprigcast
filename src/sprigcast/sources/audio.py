"""Microphone and desktop-audio sources through WASAPI (the PyAudioWPatch package).

Each source converts whatever its device delivers (mono, 5.1, 44.1 kHz, ...) to 48 kHz stereo
float32 in the device callback, and keeps it in a short buffer the mixer reads from.
"""

from __future__ import annotations

import logging
import threading
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
DEFAULT = "default"  # follow the Windows default device


@dataclass(frozen=True)
class AudioDevice:
    name: str
    loopback: bool  # True = an output device captured as "what you hear"


def _pyaudio():
    try:
        import pyaudiowpatch
    except ImportError as exc:
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
    """WASAPI microphones (loopback=False) or speakers to capture (loopback=True).

    rescan=False reuses the last scan: a rescan restarts PortAudio (a few hundred ms on Windows)."""
    try:
        pa_mod = _pyaudio()
    except RuntimeError:
        return []
    with PortAudio.lock:
        pa = PortAudio.get(pa_mod, rescan=rescan)
        devices = []
        for info in pa.get_device_info_generator_by_host_api(host_api_type=pa_mod.paWASAPI):
            if loopback and info.get("isLoopbackDevice"):
                devices.append(AudioDevice(str(info["name"]).replace(" [Loopback]", ""), True))
            elif not loopback and not info.get("isLoopbackDevice") and info["maxInputChannels"] > 0:
                devices.append(AudioDevice(str(info["name"]), False))
        return devices


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

    def _find(self, pa, pa_mod) -> dict | None:
        wanted = str(self.settings.get("device", DEFAULT))
        if wanted == DEFAULT:
            return pa.get_default_wasapi_loopback() if self.loopback else pa.get_default_wasapi_device(d_in=True)
        for info in pa.get_device_info_generator_by_host_api(host_api_type=pa_mod.paWASAPI):
            name = str(info["name"]).replace(" [Loopback]", "")
            if name == wanted and bool(info.get("isLoopbackDevice")) == self.loopback:
                return info
        return None

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
                while not (self._stop.is_set() or self._restart.is_set()):
                    if not stream.is_active():
                        self.status = "device lost, retrying"
                        rescan = True
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


@register("audio_output")
class DesktopAudioSource(_WasapiSource):
    loopback = True
