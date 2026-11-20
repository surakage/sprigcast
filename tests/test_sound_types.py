"""Sound types: five to start, and you can add, rename, recolor and remove them and put sounds in them."""

import os

import pytest

from sprigcast.core.scene import SOUND_TYPES, AudioSettings
from tests.test_sound_per_bloom import _collection

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def _default_types():
    pytest.importorskip("PySide6.QtWidgets")
    from sprigcast.app.sound_card import use_types

    use_types(list(SOUND_TYPES))
    yield
    use_types(list(SOUND_TYPES))


@pytest.fixture
def window(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("SoundTypesTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_collection(), tmp_path / "scenes.json")
    w._set_mode("build")
    yield w
    w.close()
    QSettings().clear()


def test_a_garden_starts_with_five_types():
    assert [t["name"] for t in AudioSettings().sound_types] == ["Voice", "Game", "Alerts", "Music", "Other"]


def test_use_types_and_a_missing_type():
    from sprigcast.app.sound_card import GROUP_COLOURS, GROUPS, TYPE_COLORS, type_of, use_types

    assert use_types([{"name": "Friends", "color": "teal"}, {"name": "Rest", "color": "grey"}])
    assert GROUPS == ["Friends", "Rest"]
    assert GROUP_COLOURS["Friends"] == TYPE_COLORS["teal"]
    assert type_of("Game") == "Rest"  # its type is gone: it shows under the last one
    assert not use_types([])  # a garden with none keeps what's shown


def _dialog(editing=None):
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.sound_type_dialog import SoundTypeDialog

    QApplication.instance() or QApplication([])
    sounds = [("desk", "Desktop", "Game"), ("game", "Game", "Game"), ("mic", "Mic", "Voice")]
    return SoundTypeDialog([dict(t) for t in SOUND_TYPES], sounds, editing)


def test_new_type_takes_the_ticked_sounds():
    d = _dialog()
    assert not d.ok.isEnabled()  # no name yet
    d.name.setText("game")
    assert not d.ok.isEnabled()  # a type has that name
    d.name.setText("  Background  ")
    assert d.ok.isEnabled()
    d.ticks["desk"].setChecked(True)
    types, moves = d.result_types()
    assert types[-1] == {"name": "Background", "color": "teal"}  # the first color no type has
    assert moves == {"desk": "Background"}


def test_edit_renames_and_unticked_sounds_leave():
    d = _dialog("Game")
    assert d.ticks["desk"].isChecked() and d.ticks["game"].isChecked() and not d.ticks["mic"].isChecked()
    d.name.setText("Games")
    d.ticks["desk"].setChecked(False)
    types, moves = d.result_types()
    assert [t["name"] for t in types] == ["Voice", "Games", "Alerts", "Music", "Other"]
    assert moves == {"game": "Games", "desk": "Other"}


def test_remove_type_moves_its_sounds_to_the_last():
    d = _dialog("Game")
    d._remove()
    types, moves = d.result_types()
    assert "Game" not in [t["name"] for t in types]
    assert moves == {"desk": "Other", "game": "Other"}


def test_type_pill_lists_types_and_new_type(window):
    w = window
    strips = w.sound_strips
    texts = [a.text() for a in strips.kind_menu.actions() if not a.isSeparator()]
    assert texts == ["All types", "Voice (1)", "Game (2)", "Alerts (0)", "Music (0)", "Other (0)", "New type…"]
    strips.kind_actions["Game"].trigger()
    assert sorted(sid for sid, s in strips.strips.items() if s.isVisibleTo(strips)) == ["desk", "game"]
    assert "Edit Game…" in [a.text() for a in strips.kind_menu.actions()]


def test_new_type_from_a_sound_menu_and_rename(window, monkeypatch):
    from PySide6.QtWidgets import QMenu

    from sprigcast.app import main_window as mw

    w = window

    def answer(dialog):
        dialog.name.setText("Background")
        return True

    monkeypatch.setattr(mw, "modal", answer)
    w._edit_sound_type(None, "game")  # New type… from the Game sound's right-click menu ticks it
    assert w.collection.audio.sound_types[-1]["name"] == "Background"
    assert w.collection.source("game").settings["group"] == "Background"
    assert w.collection.source("desk").settings["group"] == "Game"  # every sound's type is kept by name
    menu = QMenu(w)
    w._fill_sound_menu(menu, "game")
    kinds = next(a.menu() for a in menu.actions() if a.text() == "Type")
    assert [a.text() for a in kinds.actions() if a.isChecked()] == ["Background"]

    w.sound_strips.kind_actions["Background"].trigger()

    def rename(dialog):
        dialog.name.setText("Music bed")
        return True

    monkeypatch.setattr(mw, "modal", rename)
    w._edit_sound_type("Background")
    assert w.collection.source("game").settings["group"] == "Music bed"
    assert w.sound_strips.type_shown == "Music bed"  # the filter follows the rename
    assert w.sound_strips.strips["game"].group == "Music bed"


def test_ducking_save_keeps_the_other_audio_settings():
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.ducking_dialog import DuckingDialog

    QApplication.instance() or QApplication([])
    c = _collection()
    c.audio.track_names = ["All", "VOD", "Voice", "4", "5"]
    c.audio.sound_types = [{"name": "Only", "color": "grey"}]
    c.audio.push_mode = "talk"
    d = DuckingDialog(c)
    d.apply_to(c)
    assert c.audio.track_names[0] == "All" and c.audio.push_mode == "talk"
    assert c.audio.sound_types == [{"name": "Only", "color": "grey"}]


def test_find_a_sound_and_this_bloom(window):
    w = window
    strips = w.sound_strips

    def shown():
        return sorted(sid for sid, s in strips.strips.items() if s.isVisibleTo(strips))

    strips.search.setText("mi")
    assert shown() == ["mic"]
    strips.search.setText("nothing like it")
    assert shown() == [] and strips.none_found.isVisibleTo(strips)
    strips.search.setText("")
    strips.set_here({"mic", "desk"})
    strips.chips["here"].click()
    assert shown() == ["desk", "mic"]
    strips.chips["all"].click()
    assert shown() == ["desk", "game", "mic"]
