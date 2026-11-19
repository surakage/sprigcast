"""Sound capture in the engine (native/core/src/capture.cpp): a device read on a thread of the engine's own and
pushed straight into the mix. Without a sound card the test tone stands in for a microphone; a device the engine
can't find goes back to Python's capture."""

from __future__ import annotations

import time

import numpy as np
import pytest
from test_native_media import player_lib  # noqa: F401 (builds or finds the engine's media library)

from sprigcast import sprigcore
from sprigcast.audio import native_mixer
from sprigcast.audio.native_mixer import NativeMixer


def _wait(check, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return check()


def test_the_test_tone_is_pushed_into_the_mix(player_lib):  # noqa: F811
    mixer = sprigcore.SoundMixer(clocked=False)
    try:
        sound = mixer.add_source(tracks=("stream",))
        tap = mixer.add_tap([0])
        capture = mixer.capture(sound, sprigcore.CAPTURE_TEST_TONE)
        assert _wait(lambda: capture.stats()["frames"] >= 2 * sprigcore.MIXER_BLOCK)
        st = capture.stats()
        assert st["state"] == "capturing" and st["device"] == sprigcore.CAPTURE_TEST_TONE and st["rate"] == 48000
        mixer.tick(1)
        _, data, _ = mixer.take(tap, 1)
        assert 0.2 < float(np.abs(data[0]).max()) <= 0.26  # the 0.25 tone, straight from the capture thread
        capture.close()
        assert capture.stats()["state"] == "closed"
    finally:
        mixer.close()


def test_closing_the_mixer_ends_its_captures_first(player_lib):  # noqa: F811
    mixer = sprigcore.SoundMixer(clocked=True)
    sound = mixer.add_source(tracks=("stream",))
    capture = mixer.capture(sound, sprigcore.CAPTURE_TEST_TONE)
    assert _wait(lambda: capture.stats()["frames"] > 0)
    mixer.close()  # would push into a freed mixer if the capture still ran
    assert capture.stats()["state"] == "closed"


class _Mic:
    """A microphone source as the native mixer sees it (sources/audio.py _WasapiSource)."""

    def __init__(self, device):
        self.id, self.settings, self.gain, self.status = "mic", {"tracks": ["stream"]}, 1.0, "starting"
        self.device, self.handed = device, []
        self.pushed = 0

    def native_capture(self):
        return (self.device, False)

    def engine_captures(self, on):
        self.handed.append(on)

    def read_block(self, n):
        self.pushed += 1
        return np.zeros((n, 2), np.float32)


def test_a_microphone_is_read_by_the_engine_and_never_pushed_from_python(player_lib, monkeypatch):  # noqa: F811
    monkeypatch.setattr(native_mixer, "source_tracks", lambda s: ("stream",))
    m = NativeMixer(lambda levels: None, clocked=False)
    try:
        mic = _Mic(sprigcore.CAPTURE_TEST_TONE)
        m.set_sources([mic])
        m.tick()
        assert mic.handed == [True]
        sound = next(iter(m._known.values()))
        assert sound.capture is not None
        for _ in range(5):
            m.tick()
        assert mic.pushed == 0  # Python's capture is closed; the engine pushes
        assert _wait(lambda: sound.capture.stats()["frames"] > 0)
        m._watch_captures(m._sounds)
        assert mic.status == f"capturing {sprigcore.CAPTURE_TEST_TONE}"
        mic.audio_chain = object()  # a filter: Python makes its sound again
        m.tick()
        assert mic.handed == [True, False] and sound.capture is None
    finally:
        m.stop()


def test_a_device_the_engine_cant_find_goes_back_to_python(player_lib, monkeypatch):  # noqa: F811
    monkeypatch.setattr(native_mixer, "source_tracks", lambda s: ("stream",))
    monkeypatch.setattr(native_mixer, "CAPTURE_GIVE_UP_S", 0.0)
    m = NativeMixer(lambda levels: None, clocked=False)
    try:
        mic = _Mic("Microphone (no such device)")  # off Windows: "sound capture needs Windows"
        m.set_sources([mic])
        m.tick()
        sound = next(iter(m._known.values()))
        assert _wait(lambda: sound.capture.stats()["state"] in ("not found", "failed"))
        m._watch_captures(m._sounds)
        assert sound.capture is None and mic.handed == [True, False]
        m.tick()
        assert sound.capture is None and mic.pushed >= 1  # not tried again for this device: Python captures it
    finally:
        m.stop()


@pytest.mark.parametrize("off", ["SPRIGCAST_NO_NATIVE_CAPTURE"])
def test_the_switch_keeps_pythons_capture(player_lib, monkeypatch, off):  # noqa: F811
    monkeypatch.setenv(off, "1")
    monkeypatch.setattr(native_mixer, "source_tracks", lambda s: ("stream",))
    m = NativeMixer(lambda levels: None, clocked=False)
    try:
        mic = _Mic(sprigcore.CAPTURE_TEST_TONE)
        m.set_sources([mic])
        m.tick()
        assert mic.handed == [] and mic.pushed == 1
    finally:
        m.stop()


def test_which_side_captures_is_logged_with_why(player_lib, monkeypatch, caplog):  # noqa: F811
    import logging

    from sprigcast.core.filters import AudioChain

    monkeypatch.setattr(native_mixer, "source_tracks", lambda s: ("stream",))
    m = NativeMixer(lambda levels: None, clocked=False)
    try:
        mic = _Mic(sprigcore.CAPTURE_TEST_TONE)
        with caplog.at_level(logging.INFO, logger="sprigcast.audio.native_mixer"):
            m.set_sources([mic])
            m.tick()
            m.tick()
            from types import SimpleNamespace

            mic.audio_chain = AudioChain([SimpleNamespace(id="f", kind="low_cut", enabled=True, settings={})])
            m.tick()
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("sound mic:")]
        assert lines == [f"sound mic: the engine captures {sprigcore.CAPTURE_TEST_TONE}",
                         "sound mic: Python captures it: it has filters only Python has (LowCut)"]
    finally:
        m._engine.close()
