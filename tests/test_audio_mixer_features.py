"""Ducking, monitoring, and the mixer dock's views (rows or strips, hidden sources)."""

import types

import numpy as np
import pytest

from sprigcast.audio.mixer import BLOCK, Ducking, Mixer, levels


class Fake:
    def __init__(self, sid, value, kind="audio_output", **settings):
        self.id, self.value, self.kind, self.gain = sid, value, kind, 1.0
        self.settings = settings

    def read_block(self, n):
        return np.full((n, 2), self.value, dtype=np.float32)


def _music_db(mixer):
    return mixer._meters["music"][1]


def test_voice_ducks_the_others_smoothly_and_they_come_back():
    voice, music = Fake("mic", 0.3, kind="audio_input"), Fake("music", 0.3)
    mixer = Mixer(report=lambda levels: None)
    mixer.set_sources([voice, music])
    mixer.set_ducking(Ducking(enabled=True, amount_db=-12, threshold_db=-40, attack_ms=100, release_ms=200, hold_ms=100))
    start = levels(np.full((BLOCK, 2), 0.3, dtype=np.float32))[1]
    mixer.tick()
    first = _music_db(mixer)
    assert start - 12 < first < start  # glides down, no sudden jump
    for _ in range(40):
        mixer.tick()
    assert _music_db(mixer) == pytest.approx(start - 12, abs=0.3)
    assert mixer._meters["mic"][1] == pytest.approx(start, abs=0.01)  # the voice itself is never ducked
    assert mixer._meters["duck"][0] == pytest.approx(-12, abs=0.3)
    voice.value = 0.0  # stop talking
    mixer.tick()
    assert _music_db(mixer) == pytest.approx(start - 12, abs=0.3)  # held a moment between words
    for _ in range(80):
        mixer.tick()
    assert _music_db(mixer) == pytest.approx(start, abs=0.2) and mixer.duck_gain == pytest.approx(1.0, abs=1e-3)


def test_no_ducking_when_off_quiet_or_muted():
    voice, music = Fake("mic", 0.3, kind="audio_input"), Fake("music", 0.3)
    mixer = Mixer(report=lambda levels: None)
    mixer.set_sources([voice, music])
    for _ in range(10):
        mixer.tick()
    assert mixer.duck_gain == 1.0  # off by default
    mixer.set_ducking(Ducking(enabled=True))
    voice.gain = 0.0  # muted mic doesn't duck
    for _ in range(10):
        mixer.tick()
    assert mixer.duck_gain == 1.0
    voice.gain, voice.value = 1.0, 0.001  # below the threshold
    for _ in range(10):
        mixer.tick()
    assert mixer.duck_gain == 1.0
    voice.settings["duck_voice"] = False  # a mic switched off as a voice is ducked like the rest
    voice.value = 0.3
    music.settings["duck_voice"] = True
    for _ in range(30):
        mixer.tick()
    assert mixer._meters["mic"][1] < levels(np.full((BLOCK, 2), 0.3, dtype=np.float32))[1] - 5


def test_monitor_mix_has_only_monitored_sources_and_stream_gets_all():
    mic, desk = Fake("mic", 0.25, kind="audio_input", monitor=True), Fake("desk", 0.5)
    mixer = Mixer(report=lambda levels: None)
    mixer.set_sources([mic, desk])
    heard, streamed = [], []
    mixer.add_monitor_tap(heard.append)
    mixer.add_tap(streamed.append)
    mixer.tick()
    assert heard[0][0, 0] == pytest.approx(0.25) and streamed[0][0, 0] == pytest.approx(0.75)
    mixer.remove_monitor_tap(heard.append)
    mixer.tick()
    assert len(heard) == 1 and len(streamed) == 2


@pytest.fixture
def fake_output(monkeypatch):
    from sprigcast.sources import audio

    mod = types.ModuleType("pyaudiowpatch")
    mod.paWASAPI, mod.paFloat32, mod.paContinue = 13, 1, 0
    opened = []

    class Stream:
        def __init__(self, callback):
            self.callback, self.active = callback, True

        def is_active(self):
            return self.active

        def stop_stream(self):
            self.active = False

        def close(self):
            pass

    class PyAudio:
        def get_default_wasapi_device(self, d_in=False, d_out=False):
            return {"index": 3, "name": "Headphones", "defaultSampleRate": 48000, "maxOutputChannels": 6}

        def open(self, format, channels, rate, output, output_device_index, frames_per_buffer, stream_callback):
            stream = Stream(stream_callback)
            opened.append((output_device_index, channels, rate, stream))
            return stream

        def terminate(self):
            pass

    mod.PyAudio = PyAudio
    monkeypatch.setattr(audio, "_pyaudio", lambda: mod)
    monkeypatch.setattr(audio.PortAudio, "_pa", None)
    monkeypatch.setattr(audio.PortAudio, "_mod", None)
    return opened


def test_monitor_plays_on_the_default_device(fake_output):
    import time

    from sprigcast.audio.monitor import Monitor

    monitor = Monitor()
    monitor.write(np.ones((BLOCK, 2), dtype=np.float32))  # before the device opens: dropped, no error
    monitor.start()
    for _ in range(300):
        if fake_output and monitor._ring is not None:
            break
        time.sleep(0.01)
    assert fake_output, monitor.status
    index, channels, rate, stream = fake_output[0]
    assert (index, channels, rate) == (3, 6, 48000)
    monitor.write(np.full((480, 2), 0.5, dtype=np.float32))
    data, _flag = stream.callback(None, 480, None, 0)
    out = np.frombuffer(data, dtype=np.float32).reshape(480, 6)
    assert out[:, :2] == pytest.approx(0.5) and not out[:, 2:].any()  # front left/right of a 5.1 device
    assert "Headphones" in monitor.status
    monitor.stop()
    assert monitor.status == "off" and not stream.active


def test_engine_starts_monitoring_only_while_a_source_is_monitored(monkeypatch):
    from sprigcast.core.scene import Canvas, SceneCollection, SourceConfig
    from sprigcast.render.engine import Engine

    col = SceneCollection(canvases=[Canvas(id="h", width=64, height=36)], sources=[
        SourceConfig(id="mic", kind="audio_input", name="Mic", settings={"monitor": False})])
    engine = Engine(col)
    calls = []
    monkeypatch.setattr(engine.monitor, "start", lambda: calls.append("start"))
    monkeypatch.setattr(engine.monitor, "stop", lambda: calls.append("stop"))
    engine._sync_audio_options()
    assert calls == [] and not engine.mixer._monitor_taps
    engine._collection.sources[0].settings["monitor"] = True
    engine._sync_audio_options()
    engine._sync_audio_options()
    assert calls == ["start"] and len(engine.mixer._monitor_taps) == 1
    engine._collection.audio.ducking = True
    engine._sync_audio_options()
    assert engine.mixer.ducking.enabled
    engine._collection.sources[0].settings["muted"] = True  # nothing to hear: close the device
    engine._sync_audio_options()
    assert calls == ["start", "stop"] and not engine.mixer._monitor_taps


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_ducking_dialog_saves_settings_and_voices(app):
    from sprigcast.app.ducking_dialog import DuckingDialog
    from sprigcast.core.scene import Canvas, SceneCollection, SourceConfig

    col = SceneCollection(canvases=[Canvas(id="h", width=64, height=36)], sources=[
        SourceConfig(id="mic", kind="audio_input", name="Mic"),
        SourceConfig(id="desk", kind="audio_output", name="Desktop audio")])
    dialog = DuckingDialog(col)
    assert dialog.voice_boxes["mic"].isChecked() and not dialog.voice_boxes["desk"].isChecked()
    dialog.enabled.setChecked(True)
    dialog.amount.setValue(20)
    dialog.voice_boxes["desk"].setChecked(True)
    dialog.apply_to(col)
    assert col.audio.ducking and col.audio.duck_amount_db == -20
    assert col.sources[1].settings["duck_voice"] is True
    SceneCollection.model_validate(col.model_dump())


def test_main_window_mixer_controls(app, tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings

    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import SourceConfig

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("MixerTest")
    QSettings().clear()
    col = load_collection(tmp_path / "scenes.json")
    col.sources.append(SourceConfig(id="test-mic", kind="audio_input", name="Mic", settings={"device": "default"}))
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w._refresh_audio()
        assert "test-mic" in w.live_side.sound.rows
        w._on_sound_changed("test-mic", {"monitor": True})
        w._on_sound_changed("test-mic", {"volume_db": -6.0})
        assert col.source("test-mic").settings["monitor"] is True and col.source("test-mic").settings["volume_db"] == -6.0
        w._on_sound_changed("test-mic", {"tracks": ["stream"]})
        assert w._sound_item(col.source("test-mic")).tracks == ("stream",)
    finally:
        w.close()
        QSettings().clear()


def test_named_tracks_each_get_the_sources_that_go_to_them():
    from sprigcast.audio.mixer import source_tracks

    mic = Fake("mic", 0.25, kind="audio_input")
    music = Fake("music", 0.5, tracks=["recording", "clips"])  # e.g. copyrighted music kept off the stream
    alert = Fake("alert", 0.125, tracks=["stream"])
    mine = Fake("mine", 0.0625, monitor="only")
    assert source_tracks(mic) == ("stream", "recording", "clips", "voice")
    assert source_tracks(music) == ("recording", "clips")
    assert source_tracks(mine) == ()
    mixer = Mixer(report=lambda levels: None)
    mixer.set_sources([mic, music, alert, mine])
    streamed, recorded = [], []
    mixer.add_tap(streamed.append)
    mixer.add_tap(recorded.append, ("recording", "voice", "clips"))
    mixed = mixer.tick()
    assert streamed[0][0, 0] == pytest.approx(0.375) and mixed is streamed[0]
    rec, voice, clips = recorded[0]
    assert rec[0, 0] == pytest.approx(0.75) and voice[0, 0] == pytest.approx(0.25)
    assert clips is rec  # same sources: mixed once
    assert mixer._meters["master"][1] == pytest.approx(levels(np.full((BLOCK, 2), 0.375, dtype=np.float32))[1])
