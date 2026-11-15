"""Sound to a playback device next to SprigCast Camera (TikTok LIVE Studio)."""

import pytest

from sprigcast.audio.monitor import Monitor


class FakePA:
    def __init__(self, devices):
        self.devices = devices

    def get_default_wasapi_device(self, d_out=True):
        return {"name": "Speakers", "maxOutputChannels": 2}

    def get_device_info_generator_by_host_api(self, host_api_type=None):
        return iter(self.devices)


class FakeMod:
    paWASAPI = 13


DEVICES = [
    {"name": "CABLE Input (VB-Audio Virtual Cable)", "maxOutputChannels": 2},
    {"name": "CABLE Input (VB-Audio Virtual Cable) [Loopback]", "maxOutputChannels": 0, "isLoopbackDevice": True},
    {"name": "Microphone", "maxOutputChannels": 0},
]


def test_default_device_without_a_name():
    assert Monitor()._device_info(FakePA(DEVICES), FakeMod)["name"] == "Speakers"


def test_named_device():
    info = Monitor("CABLE Input (VB-Audio Virtual Cable)")._device_info(FakePA(DEVICES), FakeMod)
    assert info["maxOutputChannels"] == 2


def test_missing_device():
    with pytest.raises(RuntimeError):
        Monitor("Gone")._device_info(FakePA(DEVICES), FakeMod)


def test_engine_taps_the_stream_mix_only_while_asked(monkeypatch):
    from sprigcast.render import engine as engine_mod

    started = []
    monkeypatch.setattr(engine_mod.Monitor, "start", lambda self: started.append(self.device))
    monkeypatch.setattr(engine_mod.Monitor, "stop", lambda self: started.append(None))

    class Mixer:
        def __init__(self):
            self.taps = []

        def add_tap(self, cb, tracks):
            self.taps.append((cb, tracks))

        def remove_tap(self, cb):
            self.taps = [t for t in self.taps if t[0] != cb]

    eng = engine_mod.Engine.__new__(engine_mod.Engine)
    eng.mixer = Mixer()
    eng.set_sound_out("CABLE Input")
    eng.set_sound_out("CABLE Input")  # same: nothing new
    assert started == ["CABLE Input"] and eng.mixer.taps[0][1] == ("stream",)
    eng.set_sound_out(None)
    assert eng.mixer.taps == [] and started == ["CABLE Input", None] and eng.sound_out_status == "off"
