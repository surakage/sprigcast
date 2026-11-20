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

    assert use_types([{"name": "Friends", "color": "teal"}, {"name": "Rest", "color": "gray"}])
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
    c.audio.sound_types = [{"name": "Only", "color": "gray"}]
    c.audio.push_mode = "talk"
    d = DuckingDialog(c)
    d.apply_to(c)
    assert c.audio.track_names[0] == "All" and c.audio.push_mode == "talk"
    assert c.audio.sound_types == [{"name": "Only", "color": "gray"}]


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


# Colors: every theme and color preset ----------------------------------------------------------------------

def _lab(rgb):
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    x, y, z = ((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047, 0.2126 * r + 0.7152 * g + 0.0722 * b,
               (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    return 116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))


# Machado et al. (2009), full strength: how a color looks with each common kind of color blindness.
SEEN = {
    "normal": None,
    "deuteranopia": ((0.367322, 0.860646, -0.227968), (0.280085, 0.672501, 0.047413), (-0.01182, 0.04294, 0.968881)),
    "protanopia": ((0.152286, 1.052583, -0.204868), (0.114503, 0.786281, 0.099216), (-0.003882, -0.048116, 1.051998)),
    "tritanopia": ((1.255528, -0.076749, -0.178779), (-0.078411, 0.930809, 0.147602), (0.004733, 0.691367, 0.3039)),
}


def _seen(color, kind):
    rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    matrix = SEEN[kind]
    if matrix is None:
        return rgb

    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def unlin(c):
        c = min(1.0, max(0.0, c))
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055

    lin_rgb = [lin(c) for c in rgb]
    return [unlin(sum(m * c for m, c in zip(row, lin_rgb, strict=True))) for row in matrix]


def _closest(colors, kind):
    import itertools
    import math

    return min(math.dist(_lab(_seen(a, kind)), _lab(_seen(b, kind))) for a, b in itertools.combinations(colors, 2))


def test_type_colors_read_on_every_theme_and_preset():
    from sprigcast.app import theme

    for preset in theme.COLOR_PRESETS:
        colors = theme.type_colors(preset)
        assert set(colors) == set(theme.TYPE_COLOR_SETS["default"])
        assert set(theme.type_color_names(preset)) == set(colors)
        for name in theme.THEME_LOOKS:
            surfaces = theme.palette(name, preset)
            for key, color in colors.items():
                for surface in ("PANEL", "CARD", "SECTION", "RAISED", "CHROME"):
                    assert theme.contrast(color, surfaces[surface]) >= 3.0, (preset, name, key, surface)


def test_no_type_is_lives_pink():
    from sprigcast.app import theme

    for preset in theme.COLOR_PRESETS:
        for color in theme.type_colors(preset).values():
            assert _closest([color, theme.LIVE], "normal") > 25, (preset, color)


def test_type_colors_are_told_apart():
    from sprigcast.app import theme

    assert _closest(theme.type_colors("default").values(), "normal") > 20
    for kind in SEEN:  # the color-blind set, with each kind of color blindness
        assert _closest(theme.type_colors("colorblind").values(), kind) > 14, kind
    # The five a garden starts with stay further apart (the closest: Okabe-Ito's orange and yellow).
    starting = [theme.type_colors("colorblind")[t["color"]] for t in SOUND_TYPES]
    for kind in SEEN:
        assert _closest(starting, kind) > 16, kind


def test_color_swatches_say_their_color():
    d = _dialog()
    from sprigcast.app import theme

    tips = [b.toolTip() for b in d.colors.buttons()]
    assert tips == list(theme.type_color_names().values())


def test_double_click_a_mic_or_desktop_sound_picks_its_device(window, monkeypatch):
    """It said "'audio_input' sources have no settings to edit" (user 2026-11-20)."""
    from sprigcast.core.scene import SceneItem

    w = window
    asked = []

    def pick(kind, current, title="", primary="OK"):
        asked.append((kind, current, title, primary))
        return "Microphone (Voicemod)"

    monkeypatch.setattr(w, "_pick_audio_device", pick)
    monkeypatch.setattr(w, "_item", lambda: SceneItem(source="mic"))
    w._edit_source_settings()
    assert asked == [("audio_input", "default", "Mic", "Save")]
    assert w.collection.source("mic").settings["device"] == "Microphone (Voicemod)"
    assert w.collection.source("mic").name == "Microphone"  # the made-up name follows the device
    w.collection.source("desk").name = "[AOC] Game"
    monkeypatch.setattr(w, "_item", lambda: SceneItem(source="desk"))
    w._edit_source_settings()
    assert w.collection.source("desk").settings["device"] == "Microphone (Voicemod)"
    assert w.collection.source("desk").name == "[AOC] Game"  # a name you chose stays


def test_volume_and_sound_is_left_out_while_that_sound_is_open(window):
    from PySide6.QtWidgets import QMenu

    w = window

    def texts():
        menu = QMenu(w)
        w._fill_sound_menu(menu, "game")
        return [a.text() for a in menu.actions()]

    assert "Volume and sound…" in texts()
    w._select_sound("game")
    assert w.build_stage.right_stack.currentIndex() == 1
    assert "Volume and sound…" not in texts()
