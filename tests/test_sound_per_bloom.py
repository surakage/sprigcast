"""Sound per bloom (a sound picked to play only in some blooms) and Build's Sound list menu."""

import os

import numpy as np
import pytest

from sprigcast.audio.mixer import BLOCK, BLOOM_FADE_S, Mixer
from sprigcast.core.scene import Canvas, Output, Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.sources.audio import SAMPLE_RATE


def _collection(game_blooms=None, mic_blooms=None):
    game = {"apps": ["game.exe"]} | ({"blooms": game_blooms} if game_blooms is not None else {})
    mic = {"device": "default"} | ({"blooms": mic_blooms} if mic_blooms is not None else {})
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080),
                  Canvas(id="vertical", width=1080, height=1920)],
        sources=[SourceConfig(id="bg", kind="color", name="Background"),
                 SourceConfig(id="mic", kind="audio_input", name="Mic", settings=mic),
                 SourceConfig(id="game", kind="app_audio", name="Game", settings=game),
                 SourceConfig(id="desk", kind="audio_output", name="Desktop", settings={"device": "default"})],
        scenes=[Scene(id="gaming", name="Gaming", canvas="horizontal", items=[SceneItem(source="bg")]),
                Scene(id="chat", name="Just Chatting", canvas="horizontal"),
                Scene(id="soon", name="Starting soon", canvas="horizontal"),
                Scene(id="v-gaming", name="Gaming", canvas="vertical", linked_to="gaming"),
                Scene(id="v-soon", name="Starting soon", canvas="vertical", linked_to="soon")],
        active_scenes={"horizontal": "soon", "vertical": "v-soon"},
        outputs=[Output(id="tw", type="stream", service="twitch", canvas="horizontal", key_ref="k/tw")],
    )


def test_sounds_play_only_in_their_blooms():
    col = _collection(game_blooms=["gaming"], mic_blooms=["gaming", "chat"])
    assert [b.id for b in col.bloom_choices()] == ["gaming", "chat", "soon"]  # twins aren't listed again
    assert col.sounds_off_here() == {"game", "mic"}  # nothing game related on Starting soon
    col.set_active("chat")
    assert col.sounds_off_here() == {"game"}
    col.set_active("v-gaming")  # switching the vertical twin switches the main bloom too
    assert col.sounds_off_here() == frozenset()
    assert col.sound_plays(col.source("desk"))  # no blooms picked: plays everywhere


def test_a_deleted_bloom_doesnt_silence_a_sound():
    col = _collection(game_blooms=["gone"])
    assert col.sound_blooms(col.source("game")) == []
    assert col.sounds_off_here() == frozenset()


def test_switched_off_shape_doesnt_keep_a_sound_on():
    col = _collection(game_blooms=["gaming"])
    col.active_scenes = {"horizontal": "soon", "vertical": "v-gaming"}  # unusual, but possible
    assert col.sounds_off_here() == frozenset()
    col.canvases[1].enabled = False
    assert col.sounds_off_here() == {"game"}


class Fake:
    def __init__(self, sid, value=0.5):
        self.id, self.value, self.kind, self.gain, self.settings = sid, value, "app_audio", 1.0, {}

    def read_block(self, n):
        return np.full((n, 2), self.value, dtype=np.float32)


def test_mixer_fades_a_sound_out_and_back_in():
    game, mic = Fake("game"), Fake("mic", 0.0)
    mixer = Mixer(report=lambda levels: None)
    mixer.set_sources([game, mic])
    assert mixer.tick()[0, 0] == pytest.approx(0.5)
    mixer.bloom_out = frozenset({"game"})
    first = mixer.tick()
    assert first[0, 0] == pytest.approx(0.5) and 0.0 < first[-1, 0] < 0.5  # a glide, no click
    blocks = int(BLOOM_FADE_S * SAMPLE_RATE / BLOCK) + 1
    for _ in range(blocks):
        out = mixer.tick()
    assert np.all(out == 0.0) and mixer._meters["game"][1] == -60.0
    mixer.bloom_out = frozenset()
    back = mixer.tick()
    assert back[0, 0] == pytest.approx(0.0) and back[-1, 0] > 0.0
    for _ in range(blocks):
        out = mixer.tick()
    assert out[0, 0] == pytest.approx(0.5) and mixer._bloom_gates == {}


# Build's Sound list ---------------------------------------------------------------------

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
    QCoreApplication.setApplicationName("SoundPerBloomTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_collection(), tmp_path / "scenes.json")
    w._set_mode("build")
    yield w
    w.close()
    QSettings().clear()


def _sound_rows(w):
    from sprigcast.app.library import ROLE_ID, ROLE_ROW

    for i in range(w.scene_tree.topLevelItemCount()):
        top = w.scene_tree.topLevelItem(i)
        rows = [top.child(j) for j in range(top.childCount()) if top.child(j).data(0, ROLE_ROW) == "sound"]
        if rows:
            return {r.data(0, ROLE_ID): r for r in rows}, [r.text(0) for r in rows]
    return {}, []


def _texts(menu):
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def test_sound_list_is_a_to_z_and_has_its_own_menu(window):
    from PySide6.QtWidgets import QMenu

    from sprigcast.app.library import ROLE_TAG as LIB_TAG

    w = window
    _, names = _sound_rows(w)
    assert names == ["Desktop", "Game", "Mic"]
    menu = QMenu(w)
    w._fill_sound_menu(menu, "game")
    assert _texts(menu) == ["Rename…", "Mute", "Plays in", "Volume and sound…", "Filters…", "Tracks and sync…",
                            "Remove"]
    plays_in = next(a.menu() for a in menu.actions() if a.text() == "Plays in")
    assert _texts(plays_in) == ["Every bloom", "Gaming", "Just Chatting", "Starting soon"]
    assert plays_in.actions()[0].isChecked()
    next(a for a in plays_in.actions() if a.text() == "Gaming").trigger()
    assert w.collection.source("game").settings["blooms"] == ["gaming"]
    rows, _ = _sound_rows(w)
    assert rows["game"].data(0, LIB_TAG) == "Gaming"  # shown lightly on the row
    menu = QMenu(w)
    w._fill_sound_menu(menu, "game")
    plays_in = next(a.menu() for a in menu.actions() if a.text() == "Plays in")
    next(a for a in plays_in.actions() if a.text() == "Just Chatting").trigger()
    assert w.collection.source("game").settings["blooms"] == ["gaming", "chat"]
    assert _sound_rows(w)[0]["game"].data(0, LIB_TAG) == "2 blooms"
    w._set_sound_blooms("game", [])  # Every bloom
    assert "blooms" not in w.collection.source("game").settings
    next(a for a in menu.actions() if a.text() == "Mute").trigger()
    assert w.collection.source("game").settings["muted"] is True
    assert _sound_rows(w)[0]["game"].data(0, LIB_TAG) == "muted"


def test_rename_keeps_a_to_z_and_remove_drops_a_sound(window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    w = window
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Alerts", True))
    w._rename_sound("mic")
    assert w.collection.source("mic").name == "Alerts"
    assert _sound_rows(w)[1] == ["Alerts", "Desktop", "Game"]
    w._remove_sound("game")
    assert "game" not in {s.id for s in w.collection.sources}
    assert _sound_rows(w)[1] == ["Alerts", "Desktop"]


def test_build_sound_note_says_where_it_plays(window):
    w = window
    w._set_sound_blooms("mic", ["gaming", "chat"])
    w._open_build_sound("mic")
    assert w.build_tune.blooms.text() == "Gaming and Just Chatting"
    w._set_sound_blooms("mic", [])
    assert w.build_tune.blooms.text() == "Every bloom"
