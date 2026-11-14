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
    from sprigcast.app.sound_card import Sound, SoundCard, auto_group

    assert auto_group("Mic", "audio_input") == "Voice"
    assert auto_group("Spotify", "audio_output") == "Music"
    assert auto_group("Alerts (Vertical)", "audio_output") == "Alerts"
    assert auto_group("Desktop audio", "audio_output") == "Game"
    changes = []
    card = SoundCard(lambda sid, ch: changes.append((sid, ch)), lambda sid: changes.append((sid, "filters")))
    card.resize(264, 600)
    card.show()
    card.show_sound([Sound("mic", "Mic", group="Voice", pinned=True), Sound("al", "Alerts", group="Alerts"),
                     Sound("tts", "TTS", group="Alerts"), Sound("sp", "Spotify", group="Music", muted=True)])
    assert set(card.group_meters) == {"Voice", "Alerts", "Music"}
    card.set_levels({"mic": (-60, -60), "al": (-60, -60), "tts": (-60, -60), "sp": (-60, -60)}, now=100.0)
    shown = {sid for sid, row in card.rows.items() if row.isVisibleTo(card)}
    assert shown == {"mic", "sp"}  # pinned mic, muted Spotify; quiet alerts fold away
    assert card.quiet_buttons["Alerts"].text() == "2 quiet ▸"
    card.set_levels({"al": (-10, -20)}, now=101.0)
    assert card.rows["al"].isVisibleTo(card) and card.quiet_buttons["Alerts"].text() == "1 quiet ▸"
    card.set_levels({"al": (-60, -60)}, now=106.0)  # quiet again for longer than LINGER_S
    assert not card.rows["al"].isVisibleTo(card)
    card.quiet_buttons["Alerts"].click()
    assert card.rows["tts"].isVisibleTo(card) and card.quiet_buttons["Alerts"].text() == "Hide quiet ▴"
    card.edit.click()
    assert card.editing and all(r.isVisibleTo(card) for r in card.rows.values())
    # Group mute mutes every member.
    card.edit.click()
    changes.clear()
    # Tune one sound inside the card.
    card.rows["mic"].click()
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
