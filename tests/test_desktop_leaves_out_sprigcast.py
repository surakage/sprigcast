"""Desktop sound on the Windows default leaves SprigCast's own sound out (no echo of what you listen to)."""

import os
import sys
import time

import numpy as np

from sprigcast.sources import app_audio, audio


class FakeCapture:
    made: list = []

    def __init__(self, pid, name, owner, exclude=False):
        FakeCapture.made.append((pid, exclude))
        self.alive = True
        self.ring = audio.AudioRing()
        self.ring.write(np.full((960, 2), 0.25, dtype=np.float32))

    def close(self):
        self.alive = False


def _windows(monkeypatch, build=26100):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("sprigcast.winver.windows_build", lambda: build)
    monkeypatch.delenv("SPRIGCAST_PLAIN_DESKTOP_SOUND", raising=False)


def test_only_the_default_device_leaves_sprigcast_out(monkeypatch):
    _windows(monkeypatch)
    assert audio.leaves_out_sprigcast({"device": "default"})
    assert audio.leaves_out_sprigcast({})
    assert not audio.leaves_out_sprigcast({"device": "Headphones (Elgato Virtual Audio)"})
    monkeypatch.setenv("SPRIGCAST_PLAIN_DESKTOP_SOUND", "1")
    assert not audio.leaves_out_sprigcast({"device": "default"})
    monkeypatch.delenv("SPRIGCAST_PLAIN_DESKTOP_SOUND")
    _windows(monkeypatch, build=18363)  # older than Windows 10 2004: plain desktop sound
    assert not audio.leaves_out_sprigcast({"device": "default"})


def test_desktop_sound_captures_everything_but_sprigcast(monkeypatch):
    _windows(monkeypatch)
    FakeCapture.made = []
    monkeypatch.setattr(app_audio, "_ProcessCapture", FakeCapture)
    monkeypatch.setattr(app_audio, "_com_init", lambda: None)
    source = audio.DesktopAudioSource("desk", {"device": "default"})
    assert source.leaves_out_sprigcast and not source.needs_new({"device": "default"})
    assert source.needs_new({"device": "Speakers"})  # a named device: plain loopback, as a new source
    source.start()
    for _ in range(100):
        if source._others is not None:
            break
        time.sleep(0.01)
    assert FakeCapture.made == [(os.getpid(), True)]
    assert source.status == "capturing everything but SprigCast"
    assert np.allclose(source.read_block(480), 0.25)
    source.stop()
    assert source._others is None


def test_native_mixer_leaves_it_to_python():
    from sprigcast.audio import native_mixer

    src = open(native_mixer.__file__, encoding="utf-8").read()
    assert "leaves_out_sprigcast" in src
