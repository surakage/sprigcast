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


ELGATO_2 = [
    {"index": 10, "name": "Game (Elgato Virtual Audio) [Loopback]", "maxInputChannels": 2, "defaultSampleRate": 48000,
     "isLoopbackDevice": True},
    {"index": 11, "name": "Sceptre F27 (NVIDIA High Definition Audio) [Loopback]", "maxInputChannels": 2,
     "defaultSampleRate": 48000, "isLoopbackDevice": True},
    {"index": 12, "name": "Microphone (Elgato Wave:3)", "maxInputChannels": 1, "defaultSampleRate": 48000,
     "isLoopbackDevice": False},
]


def test_wave_link_2_channels_named_elgato_virtual_audio_come_first_with_plain_labels(fake, monkeypatch):
    from sprigcast.sources.audio import device_label, list_audio_devices

    monkeypatch.setattr(sys.modules[__name__], "DEVICES", ELGATO_2)
    assert [d.name for d in list_audio_devices(loopback=True)] == [
        "Game (Elgato Virtual Audio)", "Sceptre F27 (NVIDIA High Definition Audio)"]
    assert device_label("Game (Elgato Virtual Audio)") == "Game · Elgato Wave Link"
    assert device_label("Voice chat (Elgato Virtual Audio)") == "Voice chat · Elgato Wave Link"
    assert device_label("Wave Link Game (Elgato Wave:3)") == "Game · Elgato Wave Link"
    assert device_label("Speakers") == "Speakers"


def test_a_window_capture_gets_its_own_sound_like_obs_capture_audio():
    from sprigcast.audio.mixer import source_tracks
    from sprigcast.core.capture_sound import APP, set_capture_sound, sound_choice
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig

    col = SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36)],
        sources=[SourceConfig(id="mic", kind="audio_input", name="Mic"),
                 SourceConfig(id="win", kind="window_capture", name="Valorant",
                              settings={"title": "VALORANT", "process": "VALORANT-Win64-Shipping.exe"})],
        scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source="win")])],
    )
    win = col.source("win")
    assert sound_choice(col, "win") is None
    sound = set_capture_sound(col, win, APP)
    assert sound.kind == "app_audio" and sound.name == "Valorant sound"
    assert sound.settings["apps"] == ["VALORANT-Win64-Shipping.exe"] and sound.settings["group"] == "Game"
    assert sound_choice(col, "win") == APP and "stream" in source_tracks(sound)
    sound.settings = {**sound.settings, "volume_db": -6.0}
    again = set_capture_sound(col, win, "Game (Elgato Virtual Audio)")
    assert again.id == sound.id and again.kind == "audio_output"
    assert again.settings["device"] == "Game (Elgato Virtual Audio)" and again.settings["volume_db"] == -6.0
    assert col.global_device_ids() == ["mic"]  # the capture's sound never becomes the global desktop audio
    assert not col.device_off(again)
    col.scenes[0].items = []
    col.remove_source_if_unused("win")
    assert [s.id for s in col.sources] == ["mic"]  # the sound went with its capture
    assert set_capture_sound(col, win, None) is None
