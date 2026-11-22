"""Sound filters in the Filters window (user 2026-11-21): a live view picked by the filter (curve, spectrum or
waveform), Before and after meters, a Listen switch, and preview first: you hear a change in the window and the
stream keeps the old filters until Apply."""

from __future__ import annotations

import os

import numpy as np
import pytest
from test_native_media import (
    player_lib,  # noqa: F401 (builds or finds the engine's media library)
)

from sprigcast.audio import peek
from sprigcast.audio.mixer import BLOCK, Mixer
from sprigcast.audio.preview import Preview
from sprigcast.core.filters import AudioChain
from sprigcast.core.scene import Filter, SourceConfig


def _tone(level: float = 0.5) -> np.ndarray:
    t = np.arange(BLOCK) / 48000
    wave = (level * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return np.stack([wave, wave], axis=1)


class Speakers:
    def __init__(self, device):
        self.device, self.blocks, self.started, self.stopped = device, [], False, False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def write(self, block):
        self.blocks.append(np.array(block))


def test_preview_runs_the_draft_filters_not_the_real_ones():
    # Its own sound id: peek.WATCHERS is shared, and a mixer another test left running with a sound called "mic"
    # handed this preview its silent blocks too, so the newest level read -90 dB in a full run (2026-11-22).
    sid = "preview-test-mic"
    made = []
    preview = Preview(sid, [Filter(id="g", kind="gain", name="Gain", settings={"db": -6})], device="Headphones",
                      speakers=lambda d: made.append(Speakers(d)) or made[-1], threaded=False)
    preview.start()
    try:
        assert sid in peek.WATCHERS
        peek.hand(peek.WATCHERS[sid], _tone())
        preview.work()
        before, after, levels = preview.snapshot()
        assert abs(levels[0] - 20 * np.log10(0.5)) < 0.1 and abs(levels[1] - levels[0] + 6) < 0.1
        assert np.max(np.abs(after)) == pytest.approx(np.max(np.abs(before)) * 10 ** (-6 / 20), rel=1e-3)
        preview.set_filters([Filter(id="g", kind="gain", name="Gain", settings={"db": -12})])  # an edit: heard next
        preview.set_listen("after")
        peek.hand(peek.WATCHERS[sid], _tone())
        preview.work()
        assert made and made[0].device == "Headphones" and made[0].started
        assert np.max(np.abs(made[0].blocks[-1])) == pytest.approx(0.5 * 10 ** (-12 / 20), rel=1e-3)
        preview.set_listen("before")
        peek.hand(peek.WATCHERS[sid], _tone())
        preview.work()
        assert np.max(np.abs(made[0].blocks[-1])) == pytest.approx(0.5, rel=1e-3)
        preview.set_listen("off")
        preview.work()
        assert made[0].stopped
    finally:
        preview.stop()
    assert sid not in peek.WATCHERS


def test_the_mixer_hands_the_sound_before_its_filters_and_keeps_its_own():
    class Mic:
        id, kind, name, gain, settings = "mic", "audio_input", "Mic", 1.0, {}

        def read_block(self, n):
            return _tone()[:n]

    mic = Mic()
    mic.audio_chain = AudioChain([Filter(id="g", kind="gain", name="Gain", settings={"db": -6})])
    mixer = Mixer(report=lambda levels: None)
    got, heard = [], []
    mixer.set_sources([mic])
    mixer.add_tap(got.append, ("stream",))
    peek.add("mic", heard.append)
    try:
        mixer.tick()
    finally:
        peek.remove("mic", heard.append)
    assert np.max(np.abs(heard[0])) == pytest.approx(0.5, rel=1e-4)  # before its filters
    assert np.max(np.abs(got[0])) == pytest.approx(0.5 * 10 ** (-6 / 20), rel=1e-3)  # the stream: unchanged


@pytest.mark.usefixtures("player_lib")
def test_the_engine_hands_a_sound_before_its_filters():
    from sprigcast import sprigcore

    mixer = sprigcore.SoundMixer(clocked=False)
    try:
        sound = mixer.add_source(gain=0.5, tracks=("stream",))
        mixer.set_filters(sound, [(sprigcore.FILTER_GAIN, 0, (-6.0,))])
        tap = mixer.add_tap([sprigcore.MIXER_SOURCE_RAW_TAP + sound, sprigcore.MIXER_SOURCE_TAP + sound])
        for _ in range(3):
            mixer.push(sound, np.full((BLOCK, 2), 0.5, np.float32))
            mixer.tick()
        blocks = []
        while (got := mixer.take(tap, 2)) is not None:
            blocks.append(got[1])
        raw, after = blocks[-1][0], blocks[-1][1]
        assert float(raw.max()) == pytest.approx(0.5, rel=1e-4)
        assert float(after.max()) == pytest.approx(0.5 * 10 ** (-6 / 20) * 0.5, rel=1e-2)
        mixer.remove_tap(tap)
    finally:
        mixer.close()


# The window --------------------------------------------------------------------------------------------

def _app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class FakeHear:
    def __init__(self):
        self.filters, self.listen, self.started, self.stopped = None, "off", False, False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def set_filters(self, filters):
        self.filters = [(f.kind, f.enabled, dict(f.settings)) for f in filters]

    def set_listen(self, mode):
        self.listen = mode

    def snapshot(self):
        t = np.arange(24000) / 48000
        before = (0.4 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        return before, before * 0.5, (-8.0, -14.0, -11.0, -17.0)


def test_view_follows_what_the_filter_does():
    from sprigcast.app.filters_dialog import curve_out, view_for

    assert view_for("compressor") == view_for("noise_gate") == view_for("limiter") == "curve"
    assert view_for("eq3") == view_for("low_cut") == view_for("noise_suppression") == "spectrum"
    assert view_for("gain") == view_for("ducking") == view_for(None) == "waveform"
    comp = {"threshold_db": -20, "ratio": 4, "output_db": 0}
    assert curve_out("compressor", comp, -30) == -30 and curve_out("compressor", comp, 0) == -15
    assert curve_out("limiter", {"threshold_db": -6}, 0) == -6
    assert curve_out("noise_gate", {"open_db": -26, "close_db": -32}, -40) < -90
    assert curve_out("upward_compressor", {"threshold_db": -20, "ratio": 2}, -40) == -30


def test_sound_window_previews_first_then_applies():
    app = _app()
    from sprigcast.app.filters_dialog import FiltersDialog

    hearing = FakeHear()
    source = SourceConfig(id="mic", kind="audio_input", name="Mic",
                          filters=[Filter(id="c", kind="compressor", name="Compressor")])
    calls = []
    d = FiltersDialog(source, lambda save=True: calls.append(save), hear=lambda sid, filters: hearing)
    assert d.preview_first and hearing.started and d.sound_view is not None
    d.resize(920, 690)
    d.show()
    app.processEvents()
    assert d.sound_view.view() == "curve"
    assert not d.sound_view.grab().isNull()  # draws the curve
    d.add("eq3")
    assert d.sound_view.view() == "spectrum"
    assert not d.sound_view.grab().isNull()
    d.add("gain")
    d.form.set_values({"db": -9})
    assert d.sound_view.view() == "waveform"
    assert not d.sound_view.grab().isNull()
    assert [k for k, _, _ in hearing.filters] == ["compressor", "eq3", "gain"] and hearing.filters[-1][2]["db"] == -9
    assert [f.kind for f in source.filters] == ["compressor"] and calls == []  # the stream still has the old ones
    d.sound_view.listen_buttons["after"].click()
    assert hearing.listen == "after"
    d.apply()
    assert [f.kind for f in source.filters] == ["compressor", "eq3", "gain"] and calls[-1] is True
    app.processEvents()
    import time

    for _ in range(50):  # let go off the UI thread
        if hearing.stopped:
            break
        time.sleep(0.01)
    assert hearing.stopped


def test_sound_window_cancel_drops_the_changes():
    _app()
    from sprigcast.app.filters_dialog import FiltersDialog

    source = SourceConfig(id="mic", kind="audio_input", name="Mic")
    d = FiltersDialog(source, lambda save=True: None, hear=lambda sid, filters: FakeHear())
    d.add("gain")
    assert source.filters == []
    d.reject()
    assert source.filters == []
