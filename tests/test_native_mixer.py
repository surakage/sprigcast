"""The engine's sound mixer (native/core/src/mixer.cpp): the same mix as audio/mixer.py, made on a thread of the
engine's own. Checked here unclocked (sc_mixer_tick), block by block, against the Python mixer's numbers."""

from __future__ import annotations

import time

import numpy as np
import pytest
from test_native_media import (
    files,  # noqa: F401 (media files made with PyAV)
    player_lib,  # noqa: F401 (builds or finds the engine's media library)
)

from sprigcast import sprigcore
from sprigcast.sprigcore import MIXER_BLOCK as BLOCK
from sprigcast.sprigcore import (
    MIXER_MASTER,
    MIXER_MONITOR,
    MIXER_SOURCE_TAP,
    MIXER_TRACKS,
)

MAIN = ("stream", "recording", "clips")


@pytest.fixture
def mixer(player_lib):  # noqa: F811
    m = sprigcore.SoundMixer(clocked=False)
    yield m
    m.close()


def tone(value: float, n: int = BLOCK) -> np.ndarray:
    return np.full((n, 2), value, np.float32)


def play(m, pushes: dict, ticks: int) -> None:
    """Each tick, a block of each sound pushed first, as the Python sounds feed it (the queue holds 200 ms)."""
    for _ in range(ticks):
        for source, value in pushes.items():
            m.push(source, tone(value))
        m.tick(1)


def take_all(m, tap, entries):
    out = []
    while (got := m.take(tap, entries)) is not None:
        out.append(got)
    return out


def test_sounds_mix_into_their_tracks_with_volume_and_clipping(mixer):
    a = mixer.add_source(gain=0.5, tracks=MAIN)
    b = mixer.add_source(gain=1.0, tracks=("recording",))
    tap = mixer.add_tap([MIXER_TRACKS.index("stream"), MIXER_TRACKS.index("recording")])
    for _ in range(2):  # a block ahead first: pushed sounds play once one is queued
        mixer.push(a, tone(0.4))
        mixer.push(b, tone(0.9))
    mixer.tick(2)
    blocks = take_all(mixer, tap, 2)
    assert [index for index, _, _ in blocks] == [0, 1]
    _, data, dropped = blocks[-1]
    assert dropped == 0
    assert np.allclose(data[0], 0.2)  # stream: a at half volume
    assert np.allclose(data[1], 1.0)  # recording: 0.2 + 0.9 clipped to 1


def test_mono_balance_and_delay(mixer):
    left = np.zeros((BLOCK, 2), np.float32)
    left[:, 0] = 0.8
    s = mixer.add_source(tracks=("stream",), mono=True)
    tap = mixer.add_tap([0])
    mixer.push(s, left)
    mixer.push(s, left)
    mixer.tick(1)
    _, data, _ = mixer.take(tap, 1)
    assert np.allclose(data[0], 0.4)  # both sides as one
    mixer.set_source(s, tracks=("stream",), balance=1.0)
    mixer.tick(1)
    _, data, _ = mixer.take(tap, 1)
    assert np.allclose(data[0][:, 0], 0.0) and np.allclose(data[0][:, 1], 0.0)  # all right: the left is gone
    mixer.set_source(s, tracks=("stream",), delay_ms=10)  # 480 samples of silence first
    for _ in range(3):
        mixer.push(s, tone(0.3))
    mixer.tick(2)
    blocks = take_all(mixer, tap, 1)
    first = blocks[0][1][0]
    assert np.allclose(first[:480], 0.0) and np.allclose(first[480:], 0.3)


def test_a_voice_ducks_the_others_and_never_itself(mixer):
    mixer.set_ducking(True, -20.0, -38.0, 1.0, 900.0, 400.0)
    mic = mixer.add_source(tracks=MAIN, voice=True)
    music = mixer.add_source(tracks=MAIN)
    tap = mixer.add_tap([MIXER_SOURCE_TAP + mic, MIXER_SOURCE_TAP + music])
    play(mixer, {mic: 0.5, music: 0.5}, 10)
    _, data, _ = take_all(mixer, tap, 2)[-1]
    assert np.allclose(data[0], 0.5)  # the voice as it is
    assert np.allclose(data[1], 0.05, atol=0.005)  # the music 20 dB down
    assert abs(mixer.levels(MIXER_MASTER)[2] - 0.1) < 0.01  # the ducking gain


def test_a_sound_per_bloom_fades_out_and_back(mixer):
    s = mixer.add_source(tracks=("stream",))
    tap = mixer.add_tap([0])
    play(mixer, {s: 0.5}, 1)
    mixer.set_source(s, tracks=("stream",), bloom_out=True)
    play(mixer, {s: 0.5}, 13)  # 0.25 s is about 11.7 blocks
    blocks = take_all(mixer, tap, 1)
    fading = blocks[1][1][0]
    assert fading[0, 0] == pytest.approx(0.5) and fading[-1, 0] < 0.5
    assert np.allclose(blocks[-1][1][0], 0.0)
    mixer.set_source(s, tracks=("stream",))
    play(mixer, {s: 0.5}, 13)
    assert np.allclose(take_all(mixer, tap, 1)[-1][1][0], 0.5)


def test_the_monitor_mix_and_the_clean_track(mixer):
    me = mixer.add_source(tracks=(), monitor=True)  # just me: speakers only
    game = mixer.add_source(tracks=("stream", "recording", "clean"))
    alert = mixer.add_source(tracks=("stream", "recording"))  # kept out of the clean track
    tap = mixer.add_tap([0, MIXER_TRACKS.index("clean"), MIXER_MONITOR])
    for s, v in ((me, 0.1), (game, 0.2), (alert, 0.3)):
        mixer.push(s, tone(v))
        mixer.push(s, tone(v))
    mixer.tick(1)
    _, data, _ = mixer.take(tap, 3)
    assert np.allclose(data[0], 0.5) and np.allclose(data[1], 0.2) and np.allclose(data[2], 0.1)


def test_a_pushed_sound_that_runs_dry_is_silent_then_waits_for_a_whole_block(mixer):
    s = mixer.add_source(tracks=("stream",))
    tap = mixer.add_tap([0])
    mixer.push(s, tone(0.5, BLOCK // 2))
    mixer.tick(1)  # half a block queued: not yet
    assert np.allclose(mixer.take(tap, 1)[1], 0.0)
    mixer.push(s, tone(0.5, BLOCK // 2))
    mixer.tick(2)  # plays the block, then runs dry
    blocks = take_all(mixer, tap, 1)
    assert np.allclose(blocks[0][1], 0.5) and np.allclose(blocks[1][1], 0.0)
    assert mixer.levels(s)[3] == 1
    for _ in range(20):  # never more than 200 ms queued: the oldest goes
        mixer.push(s, tone(0.5))
    play(mixer, {}, 10)
    blocks = take_all(mixer, tap, 1)
    assert sum(1 for _, data, _ in blocks if np.allclose(data, 0.5)) == 9


def test_a_sound_arriving_in_uneven_bursts_keeps_a_little_more_ahead(mixer):
    """A capture card's sound while it also sends 1440p60 video comes in uneven bursts: 10 ms packets, each up to
    30 ms late. It ran dry about once a second (PC 2026-11-21, Elgato HD60 X); now it keeps a little more ahead
    after the first few."""
    rng = np.random.default_rng(7)
    packet = 480
    seconds = 60
    arrive = sorted((i * 0.010 + rng.uniform(0, 0.030), i) for i in range(seconds * 100))
    s = mixer.add_source(tracks=("stream",))
    at = 0
    dry_at = []
    for tick in range(int(seconds * 48000 / BLOCK)):
        now = tick * BLOCK / 48000
        while at < len(arrive) and arrive[at][0] <= now:
            mixer.push(s, tone(0.5, packet))
            at += 1
        before = mixer.levels(s)[3]
        mixer.tick(1)
        if mixer.levels(s)[3] > before:
            dry_at.append(now)
    late = [t for t in dry_at if t > 10]
    assert len(late) <= 2, f"ran dry {len(dry_at)} times, {len(late)} after the first 10 s"


def test_a_sound_well_ahead_drops_the_extra(mixer):
    """A capture whose clock runs a little fast builds up delay; what stays well ahead for 2 s is dropped."""
    s = mixer.add_source(tracks=("stream",))
    tap = mixer.add_tap([0])
    mixer.push(s, tone(0.0, BLOCK * 6))  # six blocks ahead from the start
    for t in range(1, 201):
        mixer.push(s, tone(t / 1000))  # each block says when it was pushed
        mixer.tick(1)
    last = take_all(mixer, tap, 1)[-1][1]
    late = 200 - round(float(last[0, 0]) * 1000)
    assert mixer.levels(s)[3] == 0 and late <= 2  # never ran dry, and no longer six blocks late


def test_levels_and_a_full_tap_drops_its_oldest(mixer):
    s = mixer.add_source(gain=0.5, tracks=("stream",))
    tap = mixer.add_tap([0])
    play(mixer, {s: 0.5}, 70)
    peak, rms, level, _ = mixer.levels(s)
    assert peak == pytest.approx(20 * np.log10(0.25), abs=0.01) and rms == pytest.approx(peak, abs=0.01)
    assert level == pytest.approx(0.25, abs=0.001)
    blocks = take_all(mixer, tap, 1)
    assert len(blocks) == 64 and blocks[0][0] == 6 and blocks[-1][2] == 6


def test_a_sound_file_plays_straight_from_the_engines_player(mixer, files):  # noqa: F811
    player = sprigcore.MediaPlayer(playing=True)
    try:
        player.set_file(str(files["sound"]), video=False, playing=True)
        player.control(sprigcore.MEDIA_START)
        s = mixer.add_source(tracks=("stream",))
        mixer.link_media(s, player)
        tap = mixer.add_tap([0])
        heard = 0.0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and heard < 0.01:
            time.sleep(BLOCK / 48000)
            mixer.tick(1)
            for _, data, _ in take_all(mixer, tap, 1):
                heard = max(heard, float(np.abs(data).max()))
        assert heard > 0.01
        mixer.link_media(s, None)  # unlinked before the player goes
    finally:
        player.close()


def test_the_clocked_mixer_mixes_on_its_own(player_lib):  # noqa: F811
    m = sprigcore.SoundMixer(clocked=True)
    try:
        s = m.add_source(tracks=("stream",))
        tap = m.add_tap([0])
        start = m.wait(-1, 1000)
        for _ in range(8):
            m.push(s, tone(0.25))
        now = m.wait(start + 5, 1000)
        assert now > start + 5
        assert any(np.allclose(data, 0.25) for _, data, _ in take_all(m, tap, 1))
        with pytest.raises(sprigcore.NativeError):
            m.tick(1)  # it keeps its own time
    finally:
        m.close()
