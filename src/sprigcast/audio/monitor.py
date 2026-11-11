"""Audio monitoring: play the sources you choose on your speakers or headphones.

The mixer hands every block of the monitored sources (48 kHz stereo) to write(). A WASAPI output
stream on the default playback device pulls it back out at the device's own rate. Monitoring never
changes what goes to the stream or the recording: those always get every unmuted source.

The stream opens when the first source is monitored and closes when none are, so an idle monitor
costs nothing.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from sprigcast.sources import audio as audio_sources
from sprigcast.sources.audio import SAMPLE_RATE, AudioRing, PortAudio

log = logging.getLogger(__name__)

LATENCY_S = 0.12  # buffered at most; older audio is dropped so monitoring never drifts behind
RETRY_SECONDS = 2.0


class Monitor:
    def __init__(self):
        self.status = "off"
        self._ring: AudioRing | None = None
        self._resampler = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sprigcast-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
        with self._lock:
            self._ring = self._resampler = None
        self.status = "off"

    def write(self, block: np.ndarray) -> None:
        """A block of 48 kHz stereo float32 from the mixer thread. Dropped until the device is open."""
        with self._lock:
            ring, resampler = self._ring, self._resampler
        if ring is None:
            return
        if resampler is not None:
            block = resampler.resample_chunk(np.ascontiguousarray(block, dtype=np.float32))
        ring.write(np.ascontiguousarray(block, dtype=np.float32))

    def _callback(self, pa_mod, channels: int, ring: AudioRing):
        def callback(in_data, frame_count, time_info, status_flags):
            try:
                stereo = ring.read(frame_count)
                if channels == 2:
                    out = stereo
                else:
                    out = np.zeros((frame_count, channels), dtype=np.float32)
                    if channels == 1:
                        out[:, 0] = stereo.mean(axis=1)
                    else:
                        out[:, :2] = stereo  # front left and right of a surround device
                return (np.ascontiguousarray(out, dtype=np.float32).tobytes(), pa_mod.paContinue)
            except Exception as exc:  # never let an exception escape into PortAudio's thread
                log.warning("monitor: dropped audio: %s", exc)
                return (bytes(frame_count * channels * 4), pa_mod.paContinue)

        return callback

    def _run(self) -> None:
        try:
            pa_mod = audio_sources._pyaudio()
        except RuntimeError as exc:
            self.status = str(exc)
            return
        import soxr

        while not self._stop.is_set():
            stream = None
            try:
                with PortAudio.lock:
                    pa = PortAudio.get(pa_mod)
                    info = pa.get_default_wasapi_device(d_out=True)
                    rate = int(info["defaultSampleRate"])
                    channels = max(1, int(info.get("maxOutputChannels", 2)))
                    ring = AudioRing(capacity=int(rate * LATENCY_S))
                    stream = pa.open(
                        format=pa_mod.paFloat32,
                        channels=channels,
                        rate=rate,
                        output=True,
                        output_device_index=int(info["index"]),
                        frames_per_buffer=rate // 100,  # 10 ms
                        stream_callback=self._callback(pa_mod, channels, ring),
                    )
                    PortAudio.opened()
                with self._lock:
                    self._ring = ring
                    self._resampler = (
                        soxr.ResampleStream(SAMPLE_RATE, rate, 2, dtype="float32") if rate != SAMPLE_RATE else None
                    )
                self.status = f"monitoring on {info['name']}"
                while not self._stop.is_set():
                    if not stream.is_active():
                        self.status = "playback device lost, retrying"
                        break
                    self._stop.wait(0.5)
            except Exception as exc:  # no playback device, exclusive mode, driver errors
                self.status = f"error: {exc}"
                log.info("monitor: playback failed: %s", exc)
            finally:
                with self._lock:
                    self._ring = self._resampler = None
                if stream is not None:
                    with PortAudio.lock:
                        try:
                            stream.stop_stream()
                            stream.close()
                        except Exception:
                            pass
                        PortAudio.closed()
            self._stop.wait(RETRY_SECONDS)
