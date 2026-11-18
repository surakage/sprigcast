"""The engine's sound mixer driven as audio/mixer.py's Mixer (audio/native_mixer.py): the same blocks for the same
sounds, block by block, as Python's mixer makes. Run unclocked (NativeMixer.tick), so each tick mixes one block."""

from __future__ import annotations

import numpy as np
import pytest
from test_native_media import (
    player_lib,  # noqa: F401 (builds or finds the engine's media library)
)

from sprigcast.audio import listen
from sprigcast.audio.mixer import BLOCK, Ducking, Mixer
from sprigcast.audio.native_mixer import NativeMixer


class Fake:
    def __init__(self, sid, values, kind="audio_output", name="", gain=1.0, **settings):
        self.id, self.kind, self.gain, self.name = sid, kind, gain, name
        self.values = values
        self.settings = settings
        self.n = 0

    def read_block(self, n):
        rng = np.random.default_rng(hash((self.id, self.n)) & 0xFFFF)
        self.n += 1
        return (rng.standard_normal((n, 2)) * self.values).astype(np.float32)


def both(make_sources, tracks, ticks, setup=None, between=None):
    """Runs the same sounds through Python's mixer and the engine's; returns both taps' blocks."""
    out = []
    for kind in (Mixer, NativeMixer):
        mixer = kind(report=lambda levels: None) if kind is Mixer else NativeMixer(lambda levels: None, clocked=False)
        try:
            sources = make_sources()
            mixer.set_sources(sources)
            got, monitor = [], []
            mixer.add_tap(got.append, tracks)
            mixer.add_monitor_tap(monitor.append)
            if setup:
                setup(mixer)
            for i in range(ticks):
                if between:
                    between(mixer, sources, i)
                mixer.tick()
            out.append((got, monitor, mixer.duck_gain))
        finally:
            if kind is NativeMixer:
                mixer._engine.close()
    return out


def same(a, b, tol=2e-6):
    a, b = np.asarray(a), np.asarray(b)
    assert a.shape == b.shape
    assert np.max(np.abs(a - b)) <= tol, float(np.max(np.abs(a - b)))


@pytest.mark.usefixtures("player_lib")
def test_tracks_volume_mono_balance_delay_and_the_monitor_mix_match_python():
    def sounds():
        return [Fake("game", 0.1, name="Game", balance=-0.5, track4=True),
                Fake("spotify", 0.2, name="Spotify", gain=0.5, monitor=True),
                Fake("mic", 0.3, kind="audio_input", name="Mic", mono=True, delay_ms=30),
                Fake("me", 0.05, monitor="only")]

    (py, py_mon, _), (nat, nat_mon, _) = both(sounds, ("stream", "recording", "vod", "voice", "track4"), 8)
    assert len(py) == len(nat) == 8
    for a, b in zip(py, nat):
        for x, y in zip(a, b):
            same(x, y)
    for a, b in zip(py_mon, nat_mon):
        same(a, b)


@pytest.mark.usefixtures("player_lib")
def test_ducking_and_sound_per_bloom_match_python():
    def sounds():
        return [Fake("mic", 0.3, kind="audio_input", name="Mic"), Fake("music", 0.3, name="Music")]

    def setup(mixer):
        mixer.set_ducking(Ducking(enabled=True, amount_db=-12.0, threshold_db=-40.0, attack_ms=50.0,
                                  release_ms=300.0, hold_ms=100.0))

    def between(mixer, sources, i):
        if i == 10:
            sources[0].values = 0.0  # stops talking: the music comes back up
        if i == 4:
            mixer.bloom_out = frozenset({"music"})
        if i == 20:
            mixer.bloom_out = frozenset()

    (py, _, py_duck), (nat, _, nat_duck) = both(sounds, ("stream",), 30, setup, between)
    for a, b in zip(py, nat):
        same(a, b, tol=2e-5)
    assert nat_duck == pytest.approx(py_duck, abs=1e-4) and py_duck < 1.0


@pytest.mark.usefixtures("player_lib")
def test_the_clean_track_leaves_out_hidden_sounds():
    def sounds():
        return [Fake("game", 0.1, name="Game"), Fake("alert", 0.2, name="Alert")]

    def setup(mixer):
        mixer.clean_out = frozenset({"alert"})

    (py, _, _), (nat, _, _) = both(sounds, ("recording", "clean"), 3, setup)
    for a, b in zip(py, nat):
        same(a[0], b[0])
        same(a[1], b[1])


@pytest.mark.usefixtures("player_lib")
def test_levels_reach_the_meters_and_a_plugin_hears_the_mix():
    reports = []
    heard = []
    mixer = NativeMixer(reports.append, clocked=False)
    try:
        mixer.set_sources([Fake("game", 0.1, name="Game"), Fake("mic", 0.2, kind="audio_input", name="Mic")])
        listen.add(listen.MIX, heard.append)
        for _ in range(6):
            mixer.tick()
        assert reports and {"game", "mic", "master", "duck"} <= set(reports[-1])
        assert -40 < reports[-1]["mic"][1] < 0
        assert heard and heard[-1].shape == (BLOCK, 2)
    finally:
        listen.remove(listen.MIX, heard.append)
        mixer._engine.close()


@pytest.mark.usefixtures("player_lib")
def test_a_sound_that_fails_is_silent_and_the_rest_go_on():
    class Broken(Fake):
        def read_block(self, n):
            raise RuntimeError("unplugged")

    got = []
    mixer = NativeMixer(lambda levels: None, clocked=False)
    try:
        mixer.set_sources([Broken("bad", 0.1), Fake("ok", 0.0, name="Game")])
        mixer.add_tap(got.append, ("stream",))
        mixer.tick()
        mixer.tick()
        assert len(got) == 2 and np.allclose(got[-1], 0.0)
        mixer.set_sources([])  # gone: let go of in the engine too
        mixer.tick()
        assert mixer._known == {}
    finally:
        mixer._engine.close()


def test_clocked_a_file_plays_straight_from_its_player_and_closing_the_player_is_safe(player_lib, tmp_path):  # noqa: F811
    """The engine reads a sound file's player itself (no Python per block); the player closing while the mixer
    runs unlinks it first (MediaPlayer.before_close), never a read of a freed player."""
    import time

    import av

    from sprigcast import sprigcore

    path = tmp_path / "tone.wav"
    with av.open(str(path), "w") as out:
        stream = out.add_stream("pcm_s16le", rate=48000, layout="stereo")
        t = np.arange(48000) / 48000
        wave = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        pcm = (np.stack([wave, wave], axis=0) * 32767).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="stereo")
        frame.sample_rate = 48000
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)

    class FileSound:
        id, kind, name, gain, settings, asleep, audio_chain = "file", "media", "Tone", 1.0, {}, False, None

        def __init__(self):
            self.player = sprigcore.MediaPlayer(playing=True)
            self.player.set_file(str(path), False, True)
            self.player.control(sprigcore.MEDIA_START)
            self.blocks_read = 0

        def native_sound(self):
            return self.player

        def read_block(self, n):  # only while not linked
            self.blocks_read += 1
            return np.zeros((n, 2), np.float32)

    got = []
    sound = FileSound()
    mixer = NativeMixer(lambda levels: None)
    try:
        mixer.set_sources([sound])
        mixer.add_tap(got.append, ("stream",))
        mixer.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(np.abs(b).max() > 0.2 for b in got):
            time.sleep(0.02)
        assert any(np.abs(b).max() > 0.2 for b in got)
        assert sound.blocks_read == 0  # never through Python
        sound.player.close()  # while the mixer runs
        time.sleep(0.2)
    finally:
        mixer.stop()
