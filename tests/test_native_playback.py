"""Sound playback in the engine (native/core/src/playback.cpp): monitoring and "Sound out" played on a device by a
thread of the engine's own, straight from the mix. Without a sound card the test sink stands in for speakers; if the
engine can't play, Python's Monitor does."""

from __future__ import annotations

import time

from test_native_media import player_lib  # noqa: F401 (builds or finds the engine's media library)

from sprigcast import sprigcore
from sprigcast.audio import native_mixer
from sprigcast.audio.mixer import Mixer
from sprigcast.audio.monitor import Monitor
from sprigcast.audio.native_mixer import EngineMonitor, NativeMixer, make_monitor


def _wait(check, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return check()


def test_the_monitor_mix_plays_on_the_test_sink(player_lib):  # noqa: F811
    mixer = sprigcore.SoundMixer(clocked=True)
    try:
        sound = mixer.add_source(tracks=("stream",), monitor=True)
        capture = mixer.capture(sound, sprigcore.CAPTURE_TEST_TONE)
        playback = mixer.play(sprigcore.MIXER_MONITOR, sprigcore.PLAYBACK_TEST_SINK)
        assert _wait(lambda: playback.stats()["frames"] >= 48000 // 4)
        st = playback.stats()
        assert st["state"] == "playing" and st["device"] == sprigcore.PLAYBACK_TEST_SINK
        assert (st["rate"], st["channels"]) == (48000, 2)
        assert st["dropped"] < 48000 // 5  # never more than 120 ms kept
        playback.close()
        assert playback.stats()["state"] == "closed"
        capture.close()
    finally:
        mixer.close()


def test_closing_the_mixer_ends_its_playback_first(player_lib):  # noqa: F811
    mixer = sprigcore.SoundMixer(clocked=True)
    playback = mixer.play(sprigcore.MIXER_TRACKS.index("stream"), sprigcore.PLAYBACK_TEST_SINK)
    assert _wait(lambda: playback.stats()["state"] == "playing")
    mixer.close()  # would take from a freed mixer if the playback still ran
    assert playback.stats()["state"] == "closed"


def test_monitoring_is_played_by_the_engine_not_handed_to_python(player_lib):  # noqa: F811
    mixer = NativeMixer(lambda levels: None, clocked=False)
    try:
        monitor = make_monitor(mixer, sprigcore.PLAYBACK_TEST_SINK)
        assert isinstance(monitor, EngineMonitor) and monitor.status == "off"
        monitor.start()
        mixer.add_monitor_tap(monitor.write)
        assert monitor.native and mixer._monitor_taps == []
        assert _wait(lambda: monitor.status == f"playing on {sprigcore.PLAYBACK_TEST_SINK}")
        mixer.remove_monitor_tap(monitor.write)
        monitor.stop()
        assert monitor.status == "off" and not monitor.native

        out = make_monitor(mixer, sprigcore.PLAYBACK_TEST_SINK)  # Sound out: the stream mix on a device
        out.start()
        mixer.add_tap(out.write, ("stream",))
        assert out.native and mixer._taps == []
        mixer.remove_tap(out.write)
        out.stop()
    finally:
        mixer._engine.close()


def test_python_plays_it_when_the_engine_cant(player_lib, monkeypatch):  # noqa: F811
    mixer = NativeMixer(lambda levels: None, clocked=False)
    started = []
    monkeypatch.setattr(Monitor, "start", lambda self: started.append(self.device))
    monkeypatch.setattr(Monitor, "stop", lambda self: started.append(None))

    def refuse(what, device=""):
        raise sprigcore.NativeError(-1, "no")

    monkeypatch.setattr(mixer._engine, "play", refuse)
    try:
        monitor = make_monitor(mixer, "Speakers")
        monitor.start()
        mixer.add_monitor_tap(monitor.write)
        assert not monitor.native and started == ["Speakers"]
        assert mixer._monitor_taps == [monitor.write]  # blocks go to Python's Monitor
        mixer.remove_monitor_tap(monitor.write)
        monitor.stop()
        assert started == ["Speakers", None] and mixer._monitor_taps == []
    finally:
        mixer._engine.close()


def test_python_mixer_or_switched_off_keeps_pythons_monitor(player_lib, monkeypatch):  # noqa: F811
    assert type(make_monitor(Mixer(lambda levels: None))) is Monitor
    mixer = NativeMixer(lambda levels: None, clocked=False)
    try:
        monkeypatch.setenv("SPRIGCAST_NO_NATIVE_MONITOR", "1")
        assert type(make_monitor(mixer)) is Monitor
    finally:
        mixer._engine.close()


def test_engine_monitor_status_says_what_is_wrong():
    class Stats:
        def __init__(self, st):
            self.st = st

        def stats(self):
            return self.st

    m = EngineMonitor(mixer=None)
    m._playback = Stats({"state": "not found", "error": "Speakers isn't plugged in", "device": ""})
    assert m.status == "error: Speakers isn't plugged in"
    m._playback = None
    assert native_mixer.EngineMonitor(None).status == "off"
