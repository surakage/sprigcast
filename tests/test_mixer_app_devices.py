"""Elgato Wave Link: its channels and mixes are offered as desktop sound, and a mix records directly."""

import sys
import types

import pytest

DEVICES = [
    {"index": 0, "name": "Microphone (Elgato Wave:3)", "maxInputChannels": 1, "defaultSampleRate": 48000,
     "isLoopbackDevice": False},
    {"index": 1, "name": "Wave Link Stream (Elgato Wave:3)", "maxInputChannels": 2, "defaultSampleRate": 48000,
     "isLoopbackDevice": False},
    {"index": 2, "name": "Speakers [Loopback]", "maxInputChannels": 2, "defaultSampleRate": 48000,
     "isLoopbackDevice": True},
    {"index": 3, "name": "Wave Link Game (Elgato Wave:3) [Loopback]", "maxInputChannels": 2,
     "defaultSampleRate": 48000, "isLoopbackDevice": True},
    {"index": 4, "name": "Webcam Mic", "maxInputChannels": 1, "defaultSampleRate": 48000, "isLoopbackDevice": False},
]


@pytest.fixture
def fake(monkeypatch):
    from sprigcast.sources.audio import PortAudio

    mod = types.ModuleType("pyaudiowpatch")
    mod.paWASAPI, mod.paFloat32, mod.paContinue = 13, 1, 0

    class PyAudio:
        def get_device_info_generator_by_host_api(self, host_api_type):
            return iter(DEVICES)

        def terminate(self):
            pass

    mod.PyAudio = PyAudio
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", mod)
    monkeypatch.setattr(PortAudio, "_pa", None)
    return mod


def test_desktop_sound_lists_wave_link_channels_and_mixes_first(fake):
    from sprigcast.sources.audio import list_audio_devices

    desktop = list_audio_devices(loopback=True)
    assert [d.name for d in desktop] == ["Wave Link Stream (Elgato Wave:3)", "Wave Link Game (Elgato Wave:3)",
                                         "Speakers"]
    assert [d.mixer_app for d in desktop] == [True, True, False]
    mics = [d.name for d in list_audio_devices(loopback=False)]
    assert mics == ["Microphone (Elgato Wave:3)", "Wave Link Stream (Elgato Wave:3)", "Webcam Mic"]


def test_desktop_sound_set_to_a_mix_records_from_it(fake):
    from sprigcast.core.source import create_source

    pa = fake.PyAudio()
    mix = create_source("audio_output", "d", {"device": "Wave Link Stream (Elgato Wave:3)"})
    assert mix._find(pa, fake)["index"] == 1
    channel = create_source("audio_output", "g", {"device": "Wave Link Game (Elgato Wave:3)"})
    assert channel._find(pa, fake)["index"] == 3
    assert create_source("audio_output", "w", {"device": "Webcam Mic"})._find(pa, fake) is not None  # by name, as asked
    assert create_source("audio_output", "x", {"device": "Gone"})._find(pa, fake) is None
    mic = create_source("audio_input", "m", {"device": "Speakers"})
    assert mic._find(pa, fake) is None  # a mic never takes a speaker
