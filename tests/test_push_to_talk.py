"""Push to talk / push to mute on the mic, "Listen through" and Windows ducking: setting keys, the mixers (Python's
and the engine's, per block) and the key listener."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest
from test_native_media import player_lib  # noqa: F401 (builds or finds the engine's media library)

from sprigcast.audio.mixer import BLOCK, Mixer
from sprigcast.core.scene import AudioSettings, Canvas, SceneCollection, SourceConfig


def test_setting_keys_and_their_defaults():
    audio = AudioSettings()
    assert audio.monitor_device == "" and audio.windows_ducking_off is True
    assert (audio.push_mode, audio.push_key, audio.push_release_ms) == ("off", "", 250)
    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                          sources=[SourceConfig(id="desk", kind="audio_output", name="Desktop"),
                                   SourceConfig(id="mic", kind="audio_input", name="Mic"),
                                   SourceConfig(id="mic2", kind="audio_input", name="Mic 2")])
    assert col.audio.push_mic(col) == "mic"
    col.audio.global_sources = ["desk", "mic2"]
    assert col.audio.push_mic(col) == "mic2"


class Fake:
    def __init__(self, sid, value=0.5, kind="audio_input"):
        self.id, self.value, self.kind, self.gain, self.settings = sid, value, kind, 1.0, {}

    def read_block(self, n):
        return np.full((n, 2), self.value, dtype=np.float32)


@pytest.mark.parametrize("make", ["python", "engine"])
def test_push_to_talk_opens_while_held_and_lets_go_late(make, request):
    if make == "engine":
        request.getfixturevalue("player_lib")
        from sprigcast.audio.native_mixer import NativeMixer

        mixer = NativeMixer(report=lambda levels: None, clocked=False)
    else:
        mixer = Mixer(report=lambda levels: None)
    try:
        mic, music = Fake("mic", 0.25), Fake("music", 0.5, "app_audio")
        mixer.set_sources([mic, music])
        mixer.set_push("mic", "talk", 100)  # 100 ms = 5 blocks
        for _ in range(4):
            mixer.tick()
        assert _stream(mixer) == pytest.approx(0.5, abs=1e-3)  # not held: only the music
        mixer.push_key(True)
        assert _stream(mixer) == pytest.approx(0.75, abs=1e-3)  # held: heard (it glides in over one block)
        mixer.push_key(False)
        held_on = [_stream(mixer) for _ in range(6)]
        assert held_on[0] == pytest.approx(0.75, abs=1e-3) and held_on[3] == pytest.approx(0.75, abs=1e-3)
        assert held_on[-1] == pytest.approx(0.5, abs=1e-3)  # let go after about 100 ms
        mixer.set_push("mic", "mute", 0)
        assert _stream(mixer) == pytest.approx(0.75, abs=1e-3)  # push to mute: heard until the key goes down
        mixer.push_key(True)
        _stream(mixer)
        assert _stream(mixer) == pytest.approx(0.5, abs=1e-3)
        mixer.push_key(False)
        _stream(mixer)
        assert _stream(mixer) == pytest.approx(0.75, abs=1e-3)  # 0 ms: back at once
        mixer.set_push("mic", "off", 0)
        mixer.push_key(True)
        assert _stream(mixer) == pytest.approx(0.75, abs=1e-3)  # off: the key does nothing
    finally:
        if make == "engine":
            mixer.stop()


def _stream(mixer) -> float:
    """One block mixed; the stream track's last sample."""
    got = []
    if not hasattr(mixer, "_test_tap"):
        mixer._test_tap = got
        mixer.add_tap(lambda block: mixer._test_tap.append(block), ("stream",))
    mixer._test_tap.clear()
    mixer.tick()
    return float(mixer._test_tap[-1][-1, 0])


def test_engine_hands_push_to_talk_and_listen_through_to_the_mixer(monkeypatch):
    from sprigcast.render import engine as engine_module
    from sprigcast.render.engine import Engine

    calls, monitors = [], []

    class Mix:
        def set_ducking(self, d): pass
        def set_push(self, *a): calls.append(a)
        def add_monitor_tap(self, cb): pass
        def remove_monitor_tap(self, cb): pass
        def push_key(self, down): calls.append(("key", down))

    class Mon:
        def __init__(self, device): self.device, self.running = device, False
        def start(self): self.running = True
        def stop(self): self.running = False
        def write(self, b): pass

    monkeypatch.setattr(engine_module, "make_monitor", lambda mixer, device=None: monitors.append(device) or Mon(device))
    ducking = []
    from sprigcast import sprigcore

    monkeypatch.setattr(sprigcore, "set_windows_ducking", lambda on: ducking.append(on) or True)
    e = Engine.__new__(Engine)
    e.mixer, e.monitor, e._monitoring = Mix(), Mon(None), False
    e._collection = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                                    sources=[SourceConfig(id="mic", kind="audio_input", name="Mic",
                                                          settings={"monitor": True})])
    a = e._collection.audio
    a.push_mode, a.push_release_ms, a.monitor_device = "talk", 500, "Headphones (Elgato Wave:3)"
    e._sync_audio_options()
    assert calls[-1] == ("mic", "talk", 500)
    assert monitors == ["Headphones (Elgato Wave:3)"] and e.monitor.running and e._monitoring
    assert ducking == [True]
    e._sync_audio_options()
    assert monitors == ["Headphones (Elgato Wave:3)"] and ducking == [True]  # nothing changed: nothing remade
    a.monitor_device, a.windows_ducking_off = "", False
    e._sync_audio_options()
    assert monitors[-1] is None and e.monitor.running and ducking == [True, False]
    e.push_key(True)
    assert calls[-1] == ("key", True)


def test_push_key_says_when_the_whole_combo_is_down(monkeypatch):
    from sprigcast.app.hotkeys import PushKey

    class Listener:
        made = []

        def __init__(self, on_press, on_release):
            self.on_press, self.on_release, self.stopped = on_press, on_release, False
            Listener.made.append(self)

        def canonical(self, k):
            return k

        def start(self): pass
        def stop(self): self.stopped = True

    class HotKey:
        @staticmethod
        def parse(text):
            return text.split("+")

    fake = types.ModuleType("pynput")
    fake.keyboard = types.SimpleNamespace(Listener=Listener, HotKey=HotKey)
    monkeypatch.setitem(sys.modules, "pynput", fake)
    said = []
    push = PushKey(said.append)
    push.apply("Ctrl+T", active=False)
    assert push.mode == "off" and not Listener.made  # push to talk off: no keyboard listening at all
    push.apply("Ctrl+T")
    listener = Listener.made[-1]
    assert push.mode == "global"
    listener.on_press("<ctrl>")
    assert said == []
    listener.on_press("t")
    listener.on_press("t")  # key repeat: said once
    assert said == [True]
    listener.on_release("<ctrl>")
    assert said == [True, False]
    listener.on_press("<ctrl>")
    push.stop()  # held as it stops: let go
    assert said == [True, False, True, False] and listener.stopped


def test_stream_and_me_on_a_phone_or_page_sound_opens_the_monitor(monkeypatch):
    """PC 2026-11-26: Android Screen's sound on Stream + me moved the meter but nothing was heard: only device
    and file sounds opened the monitor. A display capture's "monitor" (its screen) still doesn't."""
    from sprigcast.render import engine as engine_module
    from sprigcast.render.engine import Engine

    class Mix:
        def set_ducking(self, d): pass
        def set_push(self, *a): pass
        def add_monitor_tap(self, cb): pass
        def remove_monitor_tap(self, cb): pass

    class Mon:
        def __init__(self, device=None): self.running = False
        def start(self): self.running = True
        def stop(self): self.running = False
        def write(self, b): pass

    from sprigcast import sprigcore

    monkeypatch.setattr(engine_module, "make_monitor", lambda mixer, device=None: Mon(device))
    monkeypatch.setattr(sprigcore, "set_windows_ducking", lambda on: True)
    for kind, settings, heard in (("android_screen", {"monitor": True}, True), ("browser", {"monitor": True}, True),
                                  ("display_capture", {"monitor": 1}, False)):
        e = Engine.__new__(Engine)
        e.mixer, e.monitor, e._monitoring = Mix(), Mon(), False
        e._collection = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                                        sources=[SourceConfig(id="s", kind=kind, name="S", settings=settings)])
        e._sync_audio_options()
        assert e._monitoring is heard, kind
