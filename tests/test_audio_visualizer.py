"""The Audio visualizer store plugin: hearing a sound the mixer already has, turning it into bands, and
drawing the four looks cheaply (and nothing at all while off air or silent)."""

import sys
import threading
import time

import numpy as np
import pytest

from sprigcast import plugin_store
from sprigcast.audio import listen
from sprigcast.audio.mixer import BLOCK, Mixer
from sprigcast.control.native import ApiError, NativeApi
from sprigcast.core.scene import SourceConfig
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")

from test_control_api import FakeHost, _collection  # noqa: E402

RATE = 48_000


class Fake:
    def __init__(self, sid, value=0.0, gain=1.0):
        self.id, self.kind, self.gain, self.settings = sid, "audio_input", gain, {}
        self.value = value

    def read_block(self, n):
        return np.full((n, 2), self.value, dtype=np.float32)


@pytest.fixture
def viz(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    plugin = next(p for p in plugin_store.catalog() if p.id == "audio_visualizer")
    assert plugin.name == "Audio visualizer" and plugin.adds == "Source"
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    yield sys.modules["sprigcast_plugin_audio_visualizer"]
    plugin_store.remove(tmp_path / "plugins", "audio_visualizer")
    assert "audio_visualizer" not in known_kinds()
    assert not listen.LISTENERS


def _tone(hz, level=0.5, start=0):
    t = (start + np.arange(BLOCK)) / RATE
    wave = (level * np.sin(2 * np.pi * hz * t)).astype(np.float32)
    return np.stack([wave, wave], axis=1)


def _feed(source, hz, blocks=2, level=0.5):
    for i in range(blocks):
        listen.hand(listen.LISTENERS[source._heard], _tone(hz, level, i * BLOCK))


def test_the_mixer_hands_a_sound_and_the_mix_only_to_listeners():
    mic, music = Fake("mic", 0.2, gain=0.5), Fake("music", 0.1)
    mixer = Mixer(report=lambda levels: None)
    mixer.set_sources([mic, music])
    heard, mixed = [], []
    mixer.tick()  # nobody listening: nothing extra
    listen.add("mic", heard.append)
    listen.add(listen.MIX, mixed.append)
    try:
        mixer.tick()
        assert len(heard) == 1 and heard[0].shape == (BLOCK, 2)
        assert heard[0][0, 0] == pytest.approx(0.1)  # after its volume
        assert mixed[0][0, 0] == pytest.approx(0.2)  # everything viewers hear
    finally:
        listen.remove("mic", heard.append)
        listen.remove(listen.MIX, mixed.append)
    mixer.tick()
    assert len(heard) == 1 and not listen.LISTENERS


def test_a_failing_listener_is_dropped_never_the_mixer():
    mixer = Mixer(report=lambda levels: None)
    mixer.set_sources([Fake("mic", 0.2)])

    def broken(block):
        raise RuntimeError("boom")

    listen.add("mic", broken)
    mixer.tick()
    assert not listen.LISTENERS
    mixer.tick()


def _drawn(source, now):
    """Ask for a drawing at now, wait for the worker to finish it, and take it (None: nothing new)."""
    first = source.video_frame(now=now)
    end = time.monotonic() + 5
    while source._busy and time.monotonic() < end:
        time.sleep(0.002)
    assert not source._busy, "the drawing never finished"
    later = source.video_frame(now=now)  # no new drawing asked for at the same moment
    return later if later is not None else first


def test_bands_follow_the_pitch(viz):
    low, high = viz.Analyser(), viz.Analyser()
    for i in range(3):
        low.feed(_tone(100, start=i * BLOCK))
        high.feed(_tone(5000, start=i * BLOCK))
    a, _ = low.step(32, 0.0, 1.0, 1 / 30)
    b, _ = high.step(32, 0.0, 1.0, 1 / 30)
    assert a.argmax() < 8 < 20 < b.argmax()
    assert 0.5 < a.max() <= 1.0 and 0.5 < b.max() <= 1.0  # rises most of the way in one step
    edges = viz.band_edges(128)
    assert (np.diff(edges) > 0).all() and len(edges) == 129  # every band at least a bin, even at the bottom


def test_smoothing_falls_slowly_and_silence_settles(viz):
    a = viz.Analyser()
    a.feed(_tone(440))
    a.feed(_tone(440, start=BLOCK))
    top = a.step(16, 0.8, 1.0, 1 / 30)[0].max()
    a.feed(np.zeros((BLOCK, 2), np.float32))
    a.feed(np.zeros((BLOCK, 2), np.float32))
    after = a.step(16, 0.8, 1.0, 1 / 30)[0].max()
    assert 0.5 * top < after < top  # eases down
    for _ in range(80):
        a.feed(np.zeros((BLOCK, 2), np.float32))
        bands, wave = a.step(16, 0.8, 1.0, 1 / 30)
    assert bands.max() < viz.STILL and np.abs(wave).max() < viz.STILL


def test_mirror_puts_the_bass_in_the_middle(viz):
    out = viz.mirrored(np.array([5, 4, 3, 2, 1, 0], np.float32))
    assert out.tolist() == [0, 3, 5, 5, 3, 0]  # the highs too, at half the detail
    assert viz.mirrored(np.arange(5, dtype=np.float32)).tolist() == [4, 2, 0, 2, 4]


def test_each_look_draws_and_stays_inside_the_box(viz):
    for style in ("bars", "wave", "circle", "line"):
        for mirror in (False, True):
            source = create_source("audio_visualizer", "v", {"style": style, "mirror": mirror, "bars": 40})
            source.start()
            _feed(source, 220, blocks=3)
            frame = _drawn(source, 100.0)
            source.stop()
            assert frame.shape == (400, 1280, 4) and frame.dtype == np.uint8 and frame.flags.c_contiguous
            assert frame[..., 3].any(), style
            assert (frame[..., :3] <= frame[..., 3:4]).all()  # premultiplied: no colour brighter than its cover
            assert not frame[:4].any() and not frame[:, :4].any() and not frame[:, -4:].any()  # padding stays clear
    assert source.channel_order == "pBGRA"


def test_it_listens_only_while_shown_and_draws_nothing_in_silence(viz):
    source = create_source("audio_visualizer", "v", {"sound": "mic-1"})
    assert source.video_frame(now=1.0) is None and not listen.LISTENERS  # not started: nothing
    source.start()
    assert set(listen.LISTENERS) == {"mic-1"}
    _feed(source, 300)
    assert _drawn(source, 1.0) is not None
    assert _drawn(source, 1.01) is None  # 30 a second at most
    source.set_hidden(True)
    assert not listen.LISTENERS and source.video_frame(now=2.0) is None
    source.set_hidden(False)
    now = 3.0
    drawn = 0
    for _ in range(60):  # silence: it settles, draws the resting look once, then nothing
        now += 1 / 30
        listen.hand(listen.LISTENERS["mic-1"], np.zeros((BLOCK, 2), np.float32))
        drawn += _drawn(source, now) is not None
    assert 1 <= drawn <= 2
    source.update({**source.settings, "sound": listen.MIX})  # a new sound: listen to that instead
    assert set(listen.LISTENERS) == {listen.MIX}
    source.sleep()
    assert not listen.LISTENERS
    source.wake()
    assert set(listen.LISTENERS) == {listen.MIX}
    source.stop()


def test_bad_settings_fall_back(viz):
    source = create_source("audio_visualizer", "v", {"style": "zigzag", "bars": "lots", "smoothing": 9,
                                                     "sensitivity": None, "color": "green", "peak_color": "#12"})
    assert source.look() == ("bars", 48, 0.95, 1.0, False)
    source.start()
    _feed(source, 300)
    assert _drawn(source, 5.0) is not None
    source.stop()
    assert viz.colour("#ff000080", "#000000") == pytest.approx((1.0, 0.0, 0.0, 128 / 255))


def test_the_sound_list_offers_the_mix_then_sounds(viz):
    listen.set_sounds([("m", "Music"), ("a", "app sound")])
    try:
        assert viz._sounds() == [(listen.MIX, "Everything (stream)"), ("a", "app sound"), ("m", "Music")]
    finally:
        listen.set_sounds([])


def test_apps_can_change_the_look(viz):
    col = _collection()
    col.sources.append(SourceConfig(id="v", kind="audio_visualizer", name="Viz", settings={}))
    api = NativeApi(FakeHost(col))
    out = api.run("visualizer.set", {"style": "circle", "color": "#7fb8ff"})
    assert out["style"] == "circle" and col.sources[-1].settings["color"] == "#7fb8ff"
    with pytest.raises(ApiError):
        api.run("visualizer.set", {"style": "zigzag"})
    with pytest.raises(ApiError):
        api.run("visualizer.set", {"sensitivity": 50})
    with pytest.raises(ApiError):
        api.run("visualizer.set", {"color": "nope"})


def test_adding_it_puts_a_band_along_the_bottom(viz):
    col = _collection()
    col.sources.append(SourceConfig(id="v", kind="audio_visualizer", name="Audio visualizer"))
    cls = viz.AudioVisualizerSource
    part = col.own_sprig(col.scene("chat-h"), "v", cls.own_sprig, "Audio visualizer", cls.own_sprig_corner)
    for shape in col.shapes(part):
        canvas = col.canvas(shape.canvas)
        item = shape.items[0]
        assert item.y + 400 * item.scale <= canvas.height and item.y > canvas.height / 3


def test_the_render_thread_never_waits_on_the_drawing(viz):
    """On the PC the drawing took 25-105 ms a frame on the render thread: each NumPy step let Python's lock go
    and queued behind busy threads to get it back. With threads keeping the lock busy, asking for a frame
    must still take well under a millisecond or two; the worker does the drawing."""
    stop = threading.Event()

    def busy():  # pure Python: holds the lock until it's made to switch, like the encoders and sound
        while not stop.is_set():
            sum(range(20_000))

    burners = [threading.Thread(target=busy, daemon=True) for _ in range(2)]
    for t in burners:
        t.start()
    source = create_source("audio_visualizer", "v", {"style": "line", "bars": 128})
    source.start()
    times, frames = [], 0
    try:
        now = 50.0
        end = time.monotonic() + 4.0
        while time.monotonic() < end and frames < 6:
            now += 1 / 30
            _feed(source, 440)
            start = time.perf_counter()
            frame = source.video_frame(now=now)
            times.append(time.perf_counter() - start)
            frames += frame is not None
            time.sleep(1 / 60)
    finally:
        stop.set()
        for t in burners:
            t.join()
        source.stop()
    assert frames >= 1  # the worker still delivers
    times.sort()
    assert times[len(times) // 2] < 0.002, f"median {times[len(times) // 2] * 1000:.1f} ms on the render thread"


def test_drawing_stays_small_and_sleep_lets_it_all_go(viz):
    """On the PC the first drawing grew memory by about 40 MB, and it stayed: the sprites were built as
    whole-picture float arrays, and every frame made frame-sized temporary arrays. Now a first drawing
    peaks at about what it keeps (three frames and the look's sprites), a frame makes almost nothing new,
    and going to sleep drops all of it."""
    import tracemalloc

    analyser, rng = viz.Analyser(), np.random.default_rng(2)
    for style in ("bars", "wave", "circle", "line"):
        painter = viz.Painter()
        analyser.feed(rng.normal(0, 0.2, (BLOCK, 2)).astype(np.float32))
        bands, wave = analyser.step(48, 0.6, 1.0, 1 / 30)
        tracemalloc.start()
        try:
            painter.paint(style, bands, wave, {})
            first = tracemalloc.get_traced_memory()[1]
            tracemalloc.reset_peak()
            before = tracemalloc.get_traced_memory()[0]
            painter.paint(style, bands, wave, {})
            again = tracemalloc.get_traced_memory()[1] - before
        finally:
            tracemalloc.stop()
        assert first < 14 * 2**20, f"{style}: first drawing peaked at {first / 2**20:.1f} MB"
        assert again < 1 * 2**20, f"{style}: a frame made {again / 2**10:.0f} KB of temporary arrays"
        assert painter.held_bytes() < 13 * 2**20
    source = create_source("audio_visualizer", "v", {"style": "line"})
    source.start()
    _feed(source, 300)
    assert _drawn(source, 1.0) is not None
    assert source._painter.held_bytes() > 0
    source.sleep()
    assert source._painter.held_bytes() == 0 and source._ready is None
    source.stop()

