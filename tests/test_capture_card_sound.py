"""A video capture's own sound: a capture card (Elgato HD60 X...) lists its sound as an input of its own, and a new
video capture takes it along, the way OBS's device audio does."""

import os

import pytest

from sprigcast.core.capture_sound import INPUT, capture_sound, paired_input, set_capture_sound, sound_choice
from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig

INPUTS = ["Microphone (Elgato Wave:3)", "Game Capture HD60 X", "Mic FX (Elgato Virtual Audio)",
          "Digital Audio Interface (Cam Link 4K)", "Line In (Realtek(R) Audio)"]


@pytest.mark.parametrize("camera, sound", [
    ("Game Capture HD60 X", "Game Capture HD60 X"),
    ("EƖgato Game Capture HD60 X", "Game Capture HD60 X"),  # the odd l some Elgato names use
    ("Cam Link 4K", "Digital Audio Interface (Cam Link 4K)"),
    ("Elgato Facecam MK.2", None),  # no sound of its own: never the Wave:3 just because both say Elgato
    ("EƖgato Virtual Camera", None),  # never a Wave Link channel
    ("USB Video", None),
])
def test_a_capture_card_finds_its_own_sound_by_name(camera, sound):
    assert paired_input(camera, INPUTS) == sound


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        sources=[SourceConfig(id="mic", kind="audio_input", name="Mic"),
                 SourceConfig(id="card", kind="camera", name="Console", settings={"name": "Game Capture HD60 X"})],
        scenes=[Scene(id="s", name="Gaming", canvas="horizontal", items=[SceneItem(source="card")])],
        active_scenes={"horizontal": "s"},
    )


def test_its_sound_is_a_sound_of_its_own_that_never_acts_as_a_mic():
    col = _collection()
    sound = set_capture_sound(col, col.source("card"), INPUT + "Game Capture HD60 X")
    assert sound.kind == "audio_input" and sound.name == "Console sound"
    assert sound.settings["device"] == "Game Capture HD60 X" and sound.settings["capture_of"] == "card"
    assert sound.settings["group"] == "Game" and sound.settings["duck_voice"] is False  # never ducks the game
    assert sound.settings["monitor"] is True  # you hear the card in your headphones
    assert sound_choice(col, "card") == INPUT + "Game Capture HD60 X"
    assert col.global_device_ids() == ["mic"] and col.audio.push_mic(col) == "mic"  # still your mic
    col.scenes[0].items = []
    col.remove_source_if_unused("card")
    assert capture_sound(col, "card") is None  # goes with the capture


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def window(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("CaptureCardSoundTest")
    QSettings().clear()
    from sprigcast.app import main_window
    from sprigcast.sources.audio import AudioDevice

    monkeypatch.setattr(main_window, "list_audio_devices",
                        lambda loopback, rescan=True: [] if loopback else [AudioDevice(n, False) for n in INPUTS])
    w = main_window.MainWindow(_collection(), tmp_path / "scenes.json")
    yield w
    w.close()
    QSettings().clear()


def test_a_new_capture_card_brings_its_sound_and_right_click_changes_it(window):
    from PySide6.QtWidgets import QMenu

    w = window
    w.collection.sources = [s for s in w.collection.sources if s.id != "card"]
    w.collection.scenes[0].items = []
    w._pair_capture_card_sound(SourceConfig(id="gone", kind="camera", name="X"))  # not added: nothing happens
    card = SourceConfig(id="card2", kind="camera", name="Console", settings={"name": "Game Capture HD60 X"})
    w.collection.sources.append(card)
    w._pair_capture_card_sound(card)
    assert sound_choice(w.collection, "card2") == INPUT + "Game Capture HD60 X"
    menu = QMenu(w)
    w._fill_capture_sound_menu(menu, "card2")
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts[:2] == ["No sound of its own", "From an input"] and "The app's own sound" not in texts
    ticked = [a.text() for a in menu.actions() if a.isChecked()]
    assert len(ticked) == 1 and "HD60 X" in ticked[0]
    next(a for a in menu.actions() if a.text() == "No sound of its own").trigger()
    assert capture_sound(w.collection, "card2") is None
    cam = SourceConfig(id="face", kind="camera", name="Facecam", settings={"name": "Elgato Facecam MK.2"})
    w.collection.sources.append(cam)
    w._pair_capture_card_sound(cam)
    assert capture_sound(w.collection, "face") is None  # a webcam with no sound of its own gets none


def test_live_sound_card_back_button_is_a_round_icon():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication, QPushButton

    QApplication.instance() or QApplication([])
    from sprigcast.app.sound_card import TunePage

    page = TunePage(lambda *a: None, lambda *a: None, lambda: None)
    back = next(b for b in page.findChildren(QPushButton) if b.toolTip() == "Back to all sounds")
    assert back.text() == "" and not back.icon().isNull() and back.size().width() == back.size().height() == 30
