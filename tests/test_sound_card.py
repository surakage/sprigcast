import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication

from sprigcast.audio.mixer import BLOCK, shape


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_shape_mono_balance_and_delay():
    block = np.zeros((BLOCK, 2), dtype=np.float32)
    block[:, 0] = 1.0  # a mic only in the left ear
    mono = shape(block, {"mono": True}, {}, "m")
    assert np.allclose(mono, 0.5)
    right = shape(np.ones((BLOCK, 2), np.float32), {"balance": 0.5}, {}, "b")
    assert np.allclose(right[:, 0], 0.5) and np.allclose(right[:, 1], 1.0)
    pending: dict = {}
    first = shape(np.ones((BLOCK, 2), np.float32), {"delay_ms": 10}, pending, "d")  # 480 samples
    assert np.allclose(first[:480], 0.0) and np.allclose(first[480:], 1.0)
    second = shape(np.full((BLOCK, 2), 2.0, np.float32), {"delay_ms": 10}, pending, "d")
    assert np.allclose(second[:480], 1.0) and np.allclose(second[480:], 2.0)
    shape(block, {}, pending, "d")
    assert "d" not in pending


def test_just_me_never_reaches_the_stream():
    from sprigcast.audio.mixer import Mixer

    class Src:
        def __init__(self, sid, settings):
            self.id, self.settings, self.gain, self.kind = sid, settings, 1.0, "audio_output"

        def read_block(self, n):
            return np.full((n, 2), 0.25, np.float32)

    mixer = Mixer(lambda levels: None)
    heard = []
    mixer.add_monitor_tap(heard.append)
    mixer.set_sources([Src("a", {"monitor": "only"}), Src("b", {})])
    out = mixer.tick()
    assert np.allclose(out, 0.25) and np.allclose(heard[-1], 0.25)  # stream: b only; speakers: a only


def test_groups_show_only_what_is_playing(app):
    from sprigcast.app import theme
    from sprigcast.app.sound_card import GROUP_COLOURS, Sound, SoundCard, auto_group

    assert auto_group("Mic", "audio_input") == "Voice"
    assert auto_group("Spotify", "audio_output") == "Music"
    assert auto_group("Alerts (Vertical)", "audio_output") == "Alerts"
    assert auto_group("Desktop audio", "audio_output") == "Game"
    changes = []
    mic_first = []
    card = SoundCard(lambda sid, ch: changes.append((sid, ch)), lambda sid: changes.append((sid, "filters")),
                     mic_first.append)
    card.mic_first.click()
    assert mic_first == [True]
    card.set_mic_first(False)
    assert mic_first == [True] and not card.mic_first.isChecked()
    card.resize(264, 600)
    card.show()
    card.show_sound([Sound("mic", "Mic", group="Voice"), Sound("al", "Alerts", group="Alerts"),
                     Sound("tts", "TTS", group="Alerts", pinned=True), Sound("sp", "Spotify", group="Music", muted=True)])
    assert set(card.group_meters) == {"Voice", "Alerts", "Music"}
    shown = lambda: {sid for sid, row in card.rows.items() if row.isVisibleTo(card)}  # noqa: E731
    sub = lambda g: card.parts[g]["sub"].text()  # noqa: E731
    card.set_levels({"mic": (-60, -60), "al": (-60, -60), "tts": (-60, -60), "sp": (-60, -60)}, now=100.0)
    assert shown() == {"tts"}  # calm: groups only, plus a sound set to Always show
    assert sub("Voice") == "quiet" and sub("Alerts") == "2 quiet" and sub("Music") == "Spotify muted"
    # Alerts plays: the group lights up in its colour and names what's playing.
    card.set_levels({"al": (-10, -20), "mic": (-60, -60)}, now=101.0)
    assert sub("Alerts") == "Alerts" and GROUP_COLOURS["Alerts"] in card.parts["Alerts"]["name"].styleSheet()
    assert theme.MUTED in card.parts["Voice"]["name"].styleSheet()
    card.set_levels({"al": (-60, -60)}, now=106.0)  # quiet again for longer than LINGER_S
    assert sub("Alerts") == "2 quiet"
    card.toggle_group("Alerts")
    assert shown() == {"al", "tts"}
    card.toggle_group("Alerts")
    card.edit.click()
    assert card.editing and shown() == set(card.rows)
    card.edit.click()
    changes.clear()
    # Tune one sound inside the card.
    card.rows["mic"].click()  # rows open Tune even from a shut group (via Edit, or click())
    assert card.pages.currentIndex() == 1 and card.tune.title.text() == "Mic"
    card.tune.balance.setValue(-50)
    card.tune.mono.click()
    card.tune.delay.setValue(120)
    card.tune.hear["only"].click()
    card.tune.group.setCurrentText("Other")
    assert ("mic", {"balance": -0.5}) in changes and ("mic", {"mono": True}) in changes
    assert ("mic", {"delay_ms": 120}) in changes and ("mic", {"monitor": "only"}) in changes
    assert ("mic", {"group": "Other"}) in changes
    assert card.tune.balance_label.text() == "50% left"
    assert card.tune.hear["only"].isChecked() and not card.tune.hear[True].isChecked()
    card.close_tune()
    assert card.pages.currentIndex() == 0


def test_new_audio_filters_keep_the_block_shape():
    from sprigcast.core.filters import AudioChain, filter_kinds
    from sprigcast.core.scene import Filter

    labels = {k.label for k in filter_kinds("audio")}
    assert {"3-Band Equalizer", "Expander", "Upward Compressor", "Noise Suppression", "Invert Polarity",
            "Gain", "Noise Gate", "Compressor", "Limiter"} <= labels
    rng = np.random.default_rng(1)
    x = (rng.standard_normal((BLOCK, 2)) * 0.1).astype(np.float32)
    for kind in ("eq3", "expander", "upward_compressor", "noise_suppression"):
        chain = AudioChain([Filter(id="f", kind=kind, name=kind)])
        for _ in range(4):
            y = chain.process(x)
            assert y.shape == x.shape and y.dtype == np.float32 and np.isfinite(y).all()
    flipped = AudioChain([Filter(id="f", kind="invert_polarity", name="flip")]).process(x)
    assert np.allclose(flipped, -x)
    louder = AudioChain([Filter(id="f", kind="eq3", name="eq", settings={"low_db": 12.0})])
    low = np.repeat(np.sin(np.arange(BLOCK * 8) * 2 * np.pi * 80 / 48000)[:, None] * 0.1, 2, 1).astype(np.float32)
    out = np.concatenate([louder.process(low[i:i + BLOCK]) for i in range(0, len(low), BLOCK)])
    assert np.abs(out[-BLOCK:]).max() > 0.25  # +12 dB on an 80 Hz tone
