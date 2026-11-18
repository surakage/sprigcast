"""The engine's built-in sound filters (gain, noise gate, compressor, limiter, invert polarity, ducking) against
core/filters.py's: the same blocks out of both mixers, block by block. A chain with a filter only Python has, or a
sound a plugin listens to, stays in Python."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from test_native_media import (
    player_lib,  # noqa: F401 (builds or finds the engine's media library)
)
from test_native_mixer_parity import Fake, both, same

from sprigcast import sprigcore
from sprigcast.audio.native_mixer import NativeMixer, engine_filters
from sprigcast.core.filters import SOUND_LEVELS, AudioChain


def chain(*filters):
    return AudioChain([SimpleNamespace(id=f"f{i}", kind=kind, enabled=True, settings=dict(settings))
                       for i, (kind, settings) in enumerate(filters)])


class Swell(Fake):
    """Loud, then nearly silent, then loud again: gates open and close, compressors pump."""

    def read_block(self, n):
        block = super().read_block(n)
        return block * np.float32((1.0, 0.002, 0.002, 0.002, 0.8, 0.0, 0.0, 0.0, 0.0, 1.0)[(self.n - 1) % 10])


def test_only_built_in_filters_go_to_the_engine():
    got = engine_filters(chain(("gain", {"db": 6}), ("limiter", {}), ("compressor", {}), ("invert_polarity", {}),
                               ("ducking", {"sound_id": "music"}), ("noise_gate", {})), {"music": 7})
    assert [k for k, _, _ in got] == [sprigcore.FILTER_GAIN, sprigcore.FILTER_LIMITER, sprigcore.FILTER_COMPRESSOR,
                                      sprigcore.FILTER_INVERT, sprigcore.FILTER_DUCKING, sprigcore.FILTER_NOISE_GATE]
    assert got[0][2] == (6.0,) and got[4][1] == 7
    assert engine_filters(chain(("gain", {}), ("low_cut", {})), {}) is None
    assert engine_filters(chain(("ducking", {"sound_id": "gone"})), {})[0][1] == 0


@pytest.mark.usefixtures("player_lib")
def test_filters_match_python():
    def sounds():
        mic = Swell("mic", 0.3, kind="audio_input", name="Mic", mono=True)
        mic.audio_chain = chain(("noise_gate", {"attack_ms": 5, "release_ms": 40, "hold_ms": 30}),
                                ("compressor", {"ratio": 4, "threshold_db": -20, "output_db": 3}), ("gain", {"db": 2}))
        music = Swell("music", 0.5, name="Music", gain=0.7)
        music.audio_chain = chain(("limiter", {"threshold_db": -9}), ("invert_polarity", {}))
        game = Fake("game", 0.2, name="Game", balance=0.3)
        game.audio_chain = chain(("ducking", {"sound_id": "mic", "threshold_db": -30, "attack_ms": 20,
                                              "hold_ms": 50, "release_ms": 80}))
        return [mic, music, game]

    def setup(mixer):
        mixer.followed = frozenset({"mic"})

    SOUND_LEVELS.clear()
    (py, _, _), (nat, _, _) = both(sounds, ("stream",), 40, setup=setup)
    SOUND_LEVELS.clear()
    assert len(py) == len(nat) == 40
    for a, b in zip(py, nat):
        same(a[0], b[0], tol=2e-6)
    assert np.max(np.abs(np.asarray(py))) > 0.01  # not silence matched against silence


@pytest.mark.usefixtures("player_lib")
def test_settings_change_keeps_the_filters_and_a_new_kind_goes_back_to_python():
    def sounds():
        mic = Swell("mic", 0.3, kind="audio_input", name="Mic")
        mic.audio_chain = chain(("gain", {"db": 0}))
        return [mic]

    def between(mixer, sources, i):
        mic = sources[0]
        if i == 5:
            mic.audio_chain.processors[0].settings = {"db": -12.0}
        if i == 10:
            mic.audio_chain = chain(("gain", {"db": -12}), ("low_cut", {}))

    (py, _, _), (nat, _, _) = both(sounds, ("stream",), 16, between=between)
    for a, b in zip(py, nat):
        same(a[0], b[0], tol=2e-6)


@pytest.mark.usefixtures("player_lib")
def test_engine_runs_the_chain_and_python_does_not():
    mixer = NativeMixer(lambda levels: None, clocked=False)
    try:
        mic = Fake("mic", 0.3, kind="audio_input", name="Mic")
        mic.audio_chain = chain(("gain", {"db": -6}))
        mic.audio_chain.process = lambda block: pytest.fail("Python ran the engine's filters")
        mixer.set_sources([mic])
        for _ in range(3):
            mixer.tick()
        sound = next(iter(mixer._known.values()))
        assert sound.filters == ((sprigcore.FILTER_GAIN, 0, (-6.0,)),)
    finally:
        mixer._engine.close()


def test_a_fully_open_gate_stays_open_without_fluttering():
    from sprigcast.core.filters import NoiseGate, settings_for

    gate = NoiseGate(settings_for("noise_gate", {"attack_ms": 1}))
    loud = np.full((1024, 2), 0.5, np.float32)
    gate.process(loud)
    out = gate.process(loud)
    assert gate.level == 1.0 and np.all(out == loud)  # it used to dip by a release step every other chunk
