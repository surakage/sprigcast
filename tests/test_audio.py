import sys
import time
import types

import numpy as np
import pytest

from sprigcast.audio.mixer import BLOCK, FLOOR_DB, Mixer, levels, mix, to_db
from sprigcast.core.source import create_source
from sprigcast.sources.audio import AudioRing, to_stereo


def test_ring_reads_exact_blocks_and_pads_with_silence():
    ring = AudioRing(capacity=10_000)
    ring.write(np.ones((300, 2), dtype=np.float32))
    ring.write(np.full((300, 2), 0.5, dtype=np.float32))
    block = ring.read(500)
    assert block.shape == (500, 2)
    assert block[299, 0] == 1.0 and block[300, 0] == 0.5
    rest = ring.read(500)
    assert rest[99, 0] == 0.5 and rest[100, 0] == 0.0  # 100 left, then silence
    assert len(ring) == 0


def test_ring_drops_oldest_when_full():
    ring = AudioRing(capacity=100)
    ring.write(np.zeros((80, 2), dtype=np.float32))
    ring.write(np.ones((80, 2), dtype=np.float32))
    assert len(ring) == 100
    block = ring.read(100)
    assert block[19, 0] == 0.0 and block[20, 0] == 1.0  # only the newest 100 frames survive


def test_to_stereo():
    mono = np.arange(4, dtype=np.float32)[:, None]
    assert to_stereo(mono).shape == (4, 2)
    surround = np.zeros((4, 6), dtype=np.float32)
    assert to_stereo(surround).shape == (4, 2)


def test_levels_and_db():
    assert to_db(1.0) == pytest.approx(0.0)
    assert to_db(0.0) == FLOOR_DB
    block = np.full((BLOCK, 2), 0.5, dtype=np.float32)
    peak, rms = levels(block)
    assert peak == pytest.approx(-6.02, abs=0.01) and rms == pytest.approx(-6.02, abs=0.01)


def test_mix_applies_gain_and_clips():
    a = np.full((BLOCK, 2), 0.6, dtype=np.float32)
    out = mix([(a, 1.0), (a, 1.0)])
    assert out.max() == 1.0  # 1.2 clipped
    out = mix([(a, 0.5), (a, 0.0)])
    assert out[0, 0] == pytest.approx(0.3)


class FakeSource:
    def __init__(self, sid, value, gain):
        self.id, self.value, self.gain = sid, value, gain

    def read_block(self, n):
        return np.full((n, 2), self.value, dtype=np.float32)


def test_mixer_tick_reports_per_source_and_master_levels():
    mixer = Mixer(report=lambda levels: None)
    mixer.set_sources([FakeSource("mic", 0.25, 1.0), FakeSource("desk", 0.5, 0.0), object()])
    out = mixer.tick()
    assert out[0, 0] == pytest.approx(0.25)
    assert mixer._meters["desk"] == (FLOOR_DB, FLOOR_DB)  # muted
    assert mixer._meters["master"][0] == pytest.approx(-12.04, abs=0.01)
    assert len(mixer.output) == 1


def test_gain_from_settings():
    src = create_source("audio_input", "mic", {"volume_db": -6.0})
    assert src.gain == pytest.approx(0.501, abs=0.001)
    src.update({"volume_db": -6.0, "muted": True})
    assert src.gain == 0.0


@pytest.fixture
def fake_pyaudio(monkeypatch):
    mod = types.ModuleType("pyaudiowpatch")
    mod.paWASAPI, mod.paFloat32, mod.paContinue = 13, 1, 0
    opened = []

    class Stream:
        def __init__(self, callback, channels, rate):
            self.active = True
            self.callback, self.channels, self.rate = callback, channels, rate

        def is_active(self):
            if self.active:
                frames = self.rate // 100
                self.callback(np.full((frames, self.channels), 0.5, dtype=np.float32).tobytes(), frames, None, 0)
            return self.active

        def stop_stream(self):
            self.active = False

        def close(self):
            pass

    devices = [
        {"index": 0, "name": "USB Mic", "maxInputChannels": 1, "defaultSampleRate": 44100, "isLoopbackDevice": False},
        {"index": 1, "name": "Speakers [Loopback]", "maxInputChannels": 2, "defaultSampleRate": 48000, "isLoopbackDevice": True},
    ]

    class PyAudio:
        def get_device_info_generator_by_host_api(self, host_api_type):
            return iter(devices)

        def get_default_wasapi_device(self, d_in=False, d_out=False):
            return devices[0]

        def get_default_wasapi_loopback(self):
            return devices[1]

        def open(self, format, channels, rate, input, input_device_index, frames_per_buffer, stream_callback):
            stream = Stream(stream_callback, channels, rate)
            opened.append((input_device_index, channels, rate))
            return stream

        def terminate(self):
            pass

    mod.PyAudio = PyAudio
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", mod)
    return opened


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_list_devices(fake_pyaudio):
    from sprigcast.sources.audio import list_audio_devices

    assert [d.name for d in list_audio_devices(loopback=False)] == ["USB Mic"]
    assert [d.name for d in list_audio_devices(loopback=True)] == ["Speakers"]


def test_mono_44k_mic_becomes_48k_stereo(fake_pyaudio):
    src = create_source("audio_input", "mic", {"device": "default"})
    src.start()
    try:
        assert wait_for(lambda: len(src.ring) > 2000)
        assert fake_pyaudio[0] == (0, 1, 44100)
        block = src.read_block(1024)
        assert block.shape == (1024, 2)
        assert np.allclose(block[200:800], 0.5, atol=0.02)  # resampled, both channels filled
        assert src.status.startswith("capturing USB Mic")
    finally:
        src.stop()


def test_desktop_audio_by_name(fake_pyaudio):
    src = create_source("audio_output", "desk", {"device": "Speakers"})
    src.start()
    try:
        assert wait_for(lambda: fake_pyaudio and fake_pyaudio[0][0] == 1)
    finally:
        src.stop()
