"""App sound: which apps each type catches, and the starter kit's sound-by-type choices."""

import os

import pytest

from sprigcast.core.scene import Canvas, SceneCollection, SourceConfig
from sprigcast.sources.app_audio import (
    TYPES,
    AppAudioSource,
    describe,
    follows_game,
    root_processes,
    wanted_apps,
)


def test_presets_and_picks():
    assert "spotify.exe" in wanted_apps({"preset": "music"})
    assert wanted_apps({"preset": "music", "apps": ["Foo.exe"]}) == {"foo.exe"}  # picked by hand: just that
    assert follows_game({"preset": "games"}) and not follows_game({"preset": "music"})
    assert not follows_game({"preset": "games", "apps": ["VALORANT.exe"]})
    assert follows_game({"preset": "music", "auto_game": True})
    assert describe({"preset": "voice"}) == TYPES["voice"].hint
    assert describe({"preset": "voice", "apps": ["Discord.exe", "a.exe", "b.exe", "c.exe"]}) == "Discord, a, b…"


def test_one_capture_per_app_tree():
    procs = [(1, 0, "explorer.exe"), (10, 1, "Discord.exe"), (11, 10, "Discord.exe"), (12, 10, "Discord.exe"),
             (20, 1, "Spotify.exe"), (21, 20, "Spotify.exe"), (30, 1, "chrome.exe")]
    assert root_processes(procs, wanted_apps({"preset": "voice"})) == {10: "Discord.exe"}
    assert root_processes(procs, wanted_apps({"preset": "music"})) == {20: "Spotify.exe"}
    source = AppAudioSource("s", {"preset": "browser"})
    assert source.targets(procs) == {30: "chrome.exe"}
    assert source.read_block(4).shape == (4, 2)  # nothing caught: silence


def test_starter_makes_a_row_per_type_and_no_desktop():
    from sprigcast.app.starter import StarterChoice, build_starter

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)], sources=[], scenes=[])
    build_starter(col, StarterChoice(mic=None, blooms=["Ending"],
                                     app_sounds={"games": None, "music": None, "voice": ["Discord.exe"]}))
    rows = [c for c in col.sources if c.kind == "app_audio"]
    assert [c.name for c in rows] == ["Games", "Music", "Voice chat"]
    assert rows[2].settings["apps"] == ["Discord.exe"] and rows[2].settings["auto_game"] is False
    assert not [c for c in col.sources if c.kind == "audio_output"]  # nothing plays twice
    build_starter(col, StarterChoice(mic=None, blooms=["Ending"], replace_example=False,
                                     app_sounds={"music": None}))
    assert len([c for c in col.sources if c.kind == "app_audio"]) == 3  # Music was already there


def test_starter_dialog_sound_by_type():
    pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.starter import StarterDialog

    _app = QApplication.instance() or QApplication([])
    dlg = StarterDialog([], [], [], True, True, running_apps=["Discord.exe"])
    dlg.type_rows["browser"][0].setChecked(False)
    dlg.type_rows["voice"][1].setCurrentIndex(1)
    picks = dlg.choice(lambda d: {}).app_sounds
    assert "browser" not in picks and picks["voice"] == ["Discord.exe"] and picks["music"] is None
    dlg.whole.setChecked(True)
    choice = dlg.choice(lambda d: {})
    assert choice.app_sounds is None and choice.desktop == "default"
    dlg.deleteLater()


def test_app_sound_is_a_sound_everywhere():
    config = SourceConfig(id="m", kind="app_audio", name="Music", settings={"preset": "music"})
    from sprigcast.core.scene import SOUND_KINDS

    assert config.kind in SOUND_KINDS
