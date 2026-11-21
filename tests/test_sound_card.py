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
    assert auto_group("Alerts (Vertical)", "audio_output") == "SFX"
    assert auto_group("Text to speech", "app_audio") == "TTS" and auto_group("Speaker.bot", "audio_output") == "TTS"
    assert auto_group("Overlay", "browser") == "Browser"
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
    card.show_sound([Sound("mic", "Mic", group="Voice"), Sound("al", "Alerts", group="SFX"),
                     Sound("tts", "TTS", group="SFX", pinned=True), Sound("sp", "Spotify", group="Music", muted=True)])
    assert set(card.group_meters) == {"Voice", "SFX", "Music"}
    shown = lambda: {sid for sid, row in card.rows.items() if row.isVisibleTo(card)}  # noqa: E731
    sub = lambda g: card.parts[g]["sub"].text()  # noqa: E731
    card.set_levels({"mic": (-60, -60), "al": (-60, -60), "tts": (-60, -60), "sp": (-60, -60)}, now=100.0)
    assert shown() == {"tts"}  # calm: groups only, plus a sound set to Always show
    assert sub("Voice") == "quiet" and sub("SFX") == "2 quiet" and sub("Music") == "Spotify muted"
    # SFX plays: the group lights up in its colour and names what's playing.
    card.set_levels({"al": (-10, -20), "mic": (-60, -60)}, now=101.0)
    assert sub("SFX") == "Alerts" and GROUP_COLOURS["SFX"] in card.parts["SFX"]["name"].styleSheet()
    assert theme.MUTED in card.parts["Voice"]["name"].styleSheet()
    card.set_levels({"al": (-60, -60)}, now=106.0)  # quiet again for longer than LINGER_S
    assert sub("SFX") == "2 quiet"
    # Speech between words: the last slice is quiet, but the loudest moment since the last update counts.
    card.set_levels({"mic": (-14, -58)}, now=107.0)
    assert sub("Voice") == "Mic" and card.meters["mic"].level > 0.7
    card.set_levels({"mic": (-60, -60)}, now=112.0)
    card.toggle_group("SFX")
    assert shown() == {"al", "tts"}
    card.toggle_group("SFX")
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
    card.tune.goes["clips"].click()  # keep the mic out of saved clips
    card.tune.hear["only"].click()
    assert ("mic", {"tracks": ["stream", "recording"]}) in changes
    assert card.sounds["mic"].tracks == ("stream", "recording")
    card.tune.group.setCurrentText("Other")
    assert ("mic", {"balance": -0.5}) in changes and ("mic", {"mono": True}) in changes
    assert ("mic", {"delay_ms": 120}) in changes and ("mic", {"monitor": "only"}) in changes
    assert ("mic", {"group": "Other"}) in changes
    assert card.tune.balance_label.text() == "50% left"
    assert card.tune.hear["only"].isChecked() and not card.tune.hear[True].isChecked()
    assert not card.tune.goes["stream"].isEnabled()  # "just me" goes nowhere else
    card.close_tune()
    assert card.pages.currentIndex() == 0


def test_new_audio_filters_keep_the_block_shape():
    from sprigcast.core.filters import AudioChain, filter_kinds
    from sprigcast.core.scene import Filter

    labels = {k.label for k in filter_kinds("audio")}
    assert {"Equalizer", "Expander", "Upward Compressor", "Noise Suppression", "Invert Polarity",
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


def test_dragging_a_sound_rows_bar_sets_its_volume(app):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from sprigcast.app.sound_card import Sound, SoundCard, VolumeMeter

    changes = []
    card = SoundCard(lambda sid, ch: changes.append((sid, ch)), lambda sid: None)
    card.resize(320, 400)
    card.show()
    card.show_sound([Sound("mic", "Mic", group="Voice")])
    card.open_groups.add("Voice")
    card.show_sound([Sound("mic", "Mic", group="Voice")])
    meter = card.meters["mic"]
    assert isinstance(meter, VolumeMeter) and meter.db == 0
    row = card.rows["mic"]
    QTest.mouseClick(meter, Qt.MouseButton.LeftButton, pos=QPoint(6, meter.height() // 2))
    assert changes[-1] == ("mic", {"volume_db": -60.0})
    assert card.notes["mic"].text() == "-60 dB"
    card.show_sound([Sound("mic", "Mic", group="Voice", volume_db=-60.0)])
    assert card.rows["mic"] is row  # a volume change doesn't rebuild the rows (a drag keeps going)
    QTest.mouseDClick(meter, Qt.MouseButton.LeftButton, pos=QPoint(20, meter.height() // 2))
    assert changes[-1] == ("mic", {"volume_db": 0.0})
    card.close()


def test_ducking_filter_follows_another_sound_through_the_mixer():
    import numpy as np

    from sprigcast.audio.mixer import Mixer
    from sprigcast.core.filters import AudioChain
    from sprigcast.core.scene import Filter

    class Fake:
        def __init__(self, sid, value):
            self.id, self.value, self.kind, self.gain, self.settings = sid, value, "audio_output", 1.0, {}
            self.audio_chain = None

        def read_block(self, n):
            return np.full((n, 2), self.value, dtype=np.float32)

    music, mic = Fake("music", 0.5), Fake("mic", 0.0)
    music.audio_chain = AudioChain([Filter(id="d", kind="ducking", name="Ducking",
                                           settings={"sound_id": "mic", "amount_db": -20.0, "attack_ms": 10})])
    mixer = Mixer(report=lambda levels: None)
    mixer.set_sources([music, mic])
    mixer.followed = frozenset({"mic"})
    for _ in range(5):
        out = mixer.tick()
    assert out[-1, 0] == pytest.approx(0.5)  # mic quiet: music as it was
    mic.value = 0.2  # talking
    for _ in range(20):
        out = mixer.tick()
    assert out[-1, 0] == pytest.approx(0.5 * 10 ** (-20 / 20) + 0.2, abs=0.01)
    mic.value = 0.0
    for _ in range(200):
        out = mixer.tick()
    assert out[-1, 0] == pytest.approx(0.5, abs=0.01)  # back up after the hold and fade


def test_low_cut_and_de_esser_keep_the_block_shape():
    import numpy as np

    from sprigcast.core.filters import AudioChain
    from sprigcast.core.scene import Filter

    t = np.arange(4096) / 48000
    rumble = np.repeat(np.sin(2 * np.pi * 30 * t)[:, None], 2, axis=1).astype(np.float32)
    chain = AudioChain([Filter(id="l", kind="low_cut", name="Low Cut", settings={"hz": 150})])
    out = np.concatenate([chain.process(rumble[i:i + 1024]) for i in range(0, 4096, 1024)])
    assert out.shape == rumble.shape and np.abs(out[2048:]).max() < 0.2  # 30 Hz rumble mostly gone
    chain = AudioChain([Filter(id="s", kind="de_esser", name="De-esser")])
    assert chain.process(rumble[:1024]).shape == (1024, 2)


def test_a_new_set_of_sounds_reuses_the_rows(app):
    """PC test, 2026-11-20: after a garden switch, making every group and row again held the window 332 ms
    while live. Rows are now used again, and still act for the sound they show."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from sprigcast.app.sound_card import Sound, SoundCard

    changes = []
    card = SoundCard(lambda sid, ch: changes.append((sid, ch)), lambda sid: None)
    card.resize(320, 600)
    card.show()
    card.set_editing(True)
    card.show_sound([Sound("mic", "Mic", group="Voice"), Sound("game", "Game", group="Game"),
                     Sound("pc", "Desktop", group="Game", muted=True)])
    rows = set(map(id, card.rows.values()))
    card.show_sound([Sound("mic2", "Other mic", group="Voice", volume_db=-6.0),
                     Sound("music", "Music", group="Game", muted=True), Sound("x", "Extra", group="Game")])
    assert set(map(id, card.rows.values())) == rows  # the same widgets, showing the new sounds
    assert card.names["mic2"].text() == "Other mic" and card.notes["mic2"].text() == "-6 dB"
    assert card.notes["music"].text() == "muted" and card.notes["x"].text() == ""
    assert card.parts["Game"]["count"] == 2 and card.parts["Game"]["ids"] == ["music", "x"]
    meter = card.meters["x"]
    QTest.mouseClick(meter, Qt.MouseButton.LeftButton, pos=QPoint(6, meter.height() // 2))
    assert changes[-1] == ("x", {"volume_db": -60.0})
    card.parts["Game"]["mute"].click()  # mutes the group's sounds now, not the old ones
    assert ("x", {"muted": True}) in changes and not any(sid in ("game", "pc") for sid, _ in changes)
    card.show_sound([Sound("solo", "Solo", group="Voice")])  # fewer sounds: the rest wait unseen
    assert list(card.parts) == ["Voice"] and card.rows["solo"].isVisible()
    card.show_sound([])
    assert not card.rows
    card.close()
