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


WAVE_LINK = ["Sceptre F27 (NVIDIA High Definition Audio)", "System (Elgato Virtual Audio)",
             "Game (Elgato Virtual Audio)", "Music (Elgato Virtual Audio)", "SFX (Elgato Virtual Audio)",
             "Voice chat (Elgato Virtual Audio)", "TTS (Elgato Virtual Audio)", "Browser (Elgato Virtual Audio)"]


def test_an_app_sound_from_an_output_reads_as_that_output():
    from sprigcast.sources.app_audio import describe, follows_game

    settings = {"preset": "games", "device": "Game (Elgato Virtual Audio)"}
    assert describe(settings) == "Game · Elgato Wave Link" and not follows_game(settings)


def test_an_app_sound_set_to_a_channel_plays_that_output(fake):
    from sprigcast.core.source import create_source

    src = create_source("app_audio", "g", {"preset": "games", "device": "Wave Link Game (Elgato Wave:3)"})
    src.start()
    try:
        assert src._device is not None and src._thread is None  # no app scanning
        assert src.read_block(64).shape == (64, 2)
        src.update({**src.settings, "device": ""})
        assert src._device is None
    finally:
        src.stop()


def test_starter_by_type_offers_every_output_on_each_row(monkeypatch):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from sprigcast.app import starter
    from sprigcast.core.scene import Canvas, SceneCollection

    dialog = starter.StarterDialog([], [], [], True, False, speakers=WAVE_LINK, running_apps=["Discord.exe"])
    assert not dialog.show_desktop.isChecked()  # Wave Link is here: outputs only until you ask for desktop audio
    assert dialog.app_sounds() == {}  # nothing picked for you: names differ from PC to PC
    _switch, pick = dialog.type_rows["games"]
    offered = [pick.itemData(i) for i in range(pick.count())]
    assert offered[0] == "" and [o["device"] for o in offered[1:]] == WAVE_LINK  # every output, on every row
    dialog.show_desktop.setChecked(True)
    texts = [pick.itemText(i) for i in range(pick.count())]
    assert "Discord" in texts and pick.currentData() is None  # the usual apps, as before
    assert dialog.app_sounds()["voice"] is None
    offered = [pick.itemData(i) for i in range(pick.count())]
    pick.setCurrentIndex(offered.index({"device": "Game (Elgato Virtual Audio)"}))
    picks = dialog.app_sounds()
    assert picks["games"] == {"device": "Game (Elgato Virtual Audio)"}
    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)])
    starter._add_app_sounds(col, picks)
    games = next(s for s in col.sources if s.settings.get("preset") == "games")
    assert games.kind == "app_audio" and games.settings["device"] == "Game (Elgato Virtual Audio)"
    assert col.global_device_ids() == []  # outputs picked here never count as the global desktop audio


def test_without_a_mixer_app_desktop_audio_shows_from_the_start():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from sprigcast.app import starter

    dialog = starter.StarterDialog([], [], [], True, False, speakers=["Speakers"], running_apps=["Spotify.exe"])
    assert dialog.show_desktop.isChecked()
    assert dialog.app_sounds()["music"] is None  # the usual apps, as before
