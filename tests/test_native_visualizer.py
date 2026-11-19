"""The Audio visualizer drawn by the engine (native/core/src/visual.cpp) against store/audio_visualizer.py's Analyser
and Painter: the same band heights and wave, the same pixels, and the store plugin handing its drawing to the engine
when the engine mixes the sound (and back to Python if the engine's ever stops)."""

from __future__ import annotations

import time

import numpy as np
import pytest
from test_audio_visualizer import viz  # noqa: F401 (installs the store plugin)
from test_native_media import (
    player_lib,  # noqa: F401 (builds or finds the engine's media library)
)
from test_native_mixer_parity import Fake

from sprigcast import sprigcore
from sprigcast.audio import listen
from sprigcast.audio.mixer import BLOCK
from sprigcast.audio.native_mixer import NativeMixer, current
from sprigcast.store import audio_visualizer as av

pytestmark = pytest.mark.usefixtures("player_lib")


def _visual(settings: dict) -> sprigcore.SoundVisual:
    v = sprigcore.SoundVisual(None, -1, clocked=False)
    _look(v, settings)
    return v


def _look(v, settings: dict) -> None:
    style, count, smoothing, sensitivity, mirror = av.look(settings)
    v.set_look(style, count, smoothing, sensitivity, mirror, av.colour(settings.get("color"), "#4fd88f"),
               av.colour(settings.get("peak_color"), "#bff3d6"))


def _off(a: np.ndarray, b: np.ndarray, by: int = 0) -> int:
    """Pixels where any channel differs by more than `by`."""
    return int((np.abs(a.astype(int) - b.astype(int)).max(axis=2) > by).sum())


def _block(i: int, rng) -> np.ndarray:
    t = (np.arange(BLOCK) + i * BLOCK) / 48_000
    tone = 0.3 * np.sin(2 * np.pi * (180 + 70 * i) * t)
    block = np.stack([tone, 0.2 * rng.standard_normal(BLOCK)], axis=1).astype(np.float32)
    return block * np.float32(0 if i % 9 == 8 else 1)


@pytest.mark.parametrize("style", ["bars", "wave", "line", "circle"])
def test_each_look_draws_the_same_pixels_as_python(style):
    rng = np.random.default_rng(7)
    for count in (8, 33, 48, 128):
        settings = {"style": style, "bars": count, "color": "#ff6a3d", "peak_color": "#ffe0a0c0"}
        v = _visual(settings)
        try:
            bands = rng.random(count).astype(np.float32)
            wave = (rng.random(256) * 2 - 1).astype(np.float32)
            python, engine = av.Painter().paint(style, bands, wave, settings), v.draw(bands, wave)
            if style == "circle":  # float32 sines and arctangents may differ by a last bit: a level, here and there
                assert _off(python, engine, 1) == 0 and _off(python, engine) < 100
            else:
                assert _off(python, engine) == 0, (style, count)
        finally:
            v.close()


@pytest.mark.parametrize("mirror", [False, True])
def test_the_bands_and_the_wave_follow_python_step_by_step(mirror):
    rng = np.random.default_rng(3)
    for style in ("bars", "line"):
        settings = {"style": style, "bars": 64, "mirror": mirror, "smoothing": 0.7, "sensitivity": 1.4}
        style_, count, smoothing, sensitivity, _ = av.look(settings)
        v, analyser, painter = _visual(settings), av.Analyser(), av.Painter()
        out = np.zeros((400, 1280, 4), np.uint8)
        try:
            for i in range(36):
                block = _block(i, rng)
                analyser.feed(block)
                v.push(block)
                dt = (1 / 30, 1 / 25, 0.05)[i % 3]
                bands, wave = analyser.step(count, smoothing, sensitivity, dt)
                v.step(dt)
                got_bands, got_wave = v.values()
                assert np.max(np.abs(got_bands - bands)) < 1e-5 and np.max(np.abs(got_wave - wave)) < 1e-5
                if mirror:
                    bands, wave = av.mirrored(bands), av.mirrored(wave)
                if v.frame(out):
                    assert _off(painter.paint(style_, bands, wave, settings), out, 1) < 20  # edges of a hair's breadth
        finally:
            v.close()


def test_every_bar_count_splits_the_sound_as_python_does():
    rng = np.random.default_rng(5)
    block = rng.standard_normal((BLOCK, 2)).astype(np.float32) * np.float32(0.4)
    for count in range(8, 129):
        v, analyser = _visual({"bars": count, "smoothing": 0.0}), av.Analyser()
        try:
            for _ in range(2):
                analyser.feed(block)
                v.push(block)
            bands, _wave = analyser.step(count, 0.0, 1.0, 1 / 30)
            v.step(1 / 30)
            assert np.max(np.abs(v.values()[0] - bands)) < 1e-5, count
        finally:
            v.close()


def test_silence_is_drawn_once_then_nothing():
    v = _visual({"smoothing": 0.0})
    out = np.zeros((400, 1280, 4), np.uint8)
    try:
        v.push(np.full((BLOCK, 2), 0.5, np.float32))
        assert v.step(1 / 30) and v.frame(out)
        v.push(np.zeros((BLOCK, 2), np.float32))
        v.push(np.zeros((BLOCK, 2), np.float32))
        for _ in range(4):
            v.step(1 / 30)
        assert not v.step(1 / 30) and v.frame(out)  # silence drawn once...
        assert not v.frame(out)  # ...and nothing new after it
        _look(v, {"style": "wave"})
        assert v.step(1 / 30) and v.frame(out)  # a new look is drawn straight away, even in silence
    finally:
        v.close()


def test_its_own_thread_draws_what_it_hears():
    v = sprigcore.SoundVisual(None, -1, clocked=True)
    out = np.zeros((400, 1280, 4), np.uint8)
    try:
        _look(v, {"style": "bars"})
        deadline = time.monotonic() + 3
        drawn = False
        while time.monotonic() < deadline and not drawn:
            v.push(_block(1, np.random.default_rng(1)))
            time.sleep(0.02)
            drawn = v.frame(out) and out[..., 3].any()
        assert drawn
    finally:
        v.close()
    v.close()  # twice is fine
    with pytest.raises(sprigcore.NativeError):
        v.frame(out)


def test_it_hears_the_mix_or_one_sound_from_the_engines_mixer():
    mixer = NativeMixer(lambda levels: None, clocked=False)
    try:
        mic, music = Fake("mic", 0.3, kind="audio_input", name="Mic"), Fake("music", 0.0, name="Music")
        mixer.set_sources([mic, music])
        assert current() is mixer
        whole, quiet = mixer.visual(listen.MIX), mixer.visual("music")
        for v in (whole, quiet):
            _look(v, {"smoothing": 0.0})
        for _ in range(3):
            mixer.tick()
        assert whole.what == 0 and quiet.what >= sprigcore.MIXER_SOURCE_TAP
        whole.step(1 / 30)
        quiet.step(1 / 30)
        assert whole.values()[0].max() > 0.3  # the mix has the mic in it
        assert quiet.values()[0].max() < av.STILL  # the music is silent
        mixer.visual_hears(quiet, "gone")
        mixer.tick()
        assert quiet.what == -1  # a sound that isn't there: it hears nothing
        mixer.drop_visual(quiet)
        assert quiet.closed
    finally:
        mixer.stop()
    assert whole.closed and current() is None  # closed with the mixer, before it


def test_the_store_plugin_lets_the_engine_draw_it(viz):  # noqa: F811
    mixer = NativeMixer(lambda levels: None, clocked=False)
    source = viz.AudioVisualizerSource("v1", {"style": "circle"})
    try:
        mixer.set_sources([Fake("mic", 0.3, kind="audio_input", name="Mic")])
        source.start()
        visual = source.engine_player
        assert isinstance(visual, sprigcore.SoundVisual) and not listen.LISTENERS  # nothing heard in Python
        for _ in range(3):
            mixer.tick()
        visual.step(1 / 30)
        frame = source.video_frame()  # Python's compositor drawing: a copy of the engine's
        assert frame is not None and frame.shape == (400, 1280, 4) and frame[..., 3].any()
        assert source.video_frame() is None  # nothing new
        source.update({**source.settings, "style": "bars"})
        assert visual.step(1 / 30)  # the new look went to the engine
        source.sleep()
        assert visual.closed and source.engine_player is None
        source.wake()
        assert source.engine_player is not None and source.engine_player is not visual
        source.engine_player.close()  # the engine's went away underneath: Python takes over
        assert source.video_frame() is None
        assert source.engine_player is None and listen.LISTENERS.get(listen.MIX)
    finally:
        source.stop()
        mixer.stop()
    assert not listen.LISTENERS


def test_the_switch_keeps_it_in_python(viz, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SPRIGCAST_NO_NATIVE_VISUALIZER", "1")
    mixer = NativeMixer(lambda levels: None, clocked=False)
    source = viz.AudioVisualizerSource("v1", {})
    try:
        source.start()
        assert source.engine_player is None and listen.LISTENERS.get(listen.MIX)
    finally:
        source.stop()
        mixer.stop()


@pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")
def test_the_engine_shows_the_drawings_itself():
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
    from sprigcast.render import native_bridge

    col = SceneCollection(
        canvases=[Canvas(id="h", width=1280, height=400)],
        sources=[SourceConfig(id="viz", kind="browser", name="Viz", settings={"width": 1280, "height": 400})],
        scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source="viz", x=0, y=0, align="top_left")])],
        active_scenes={"h": "a"},
    )
    visual = sprigcore.SoundVisual(None, -1, clocked=True)
    bridge = native_bridge.NativeCanvases(col, warp=True)
    try:
        _look(visual, {"style": "bars", "sensitivity": 10, "smoothing": 0.0})
        assert bridge.attach_media("viz", visual)
        bridge.sync(col, {}, lambda collection, layout: (layout, None), False)
        bridge.watch("h", (128, 40), 0)
        deadline, green = time.monotonic() + 5, False
        while time.monotonic() < deadline and not green:
            visual.push(_block(1, np.random.default_rng(1)))
            got = bridge.take("h")
            if got is not None:
                img = np.frombuffer(got[0], np.uint8).reshape(got[2], got[1], 4)
                green = int(img[..., 1].max()) > 100  # the bars, drawn by the engine with no picture from Python
            time.sleep(0.02)
        assert green and not bridge.failed
        assert not bridge.attach_media("viz", None)
    finally:
        bridge.close()
        visual.close()
