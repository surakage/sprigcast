"""+ Add: A to Z like OBS, with Game capture, Slide show, Mirror and the sounds as their own entries."""

import os
import tempfile
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _window(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    _app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastAddMenuTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        sources=[SourceConfig(id="bg", kind="color", name="Moss", settings={"color": "#224433ff"})],
        scenes=[Scene(id="a", name="A", canvas="horizontal", items=[SceneItem(source="bg")]),
                Scene(id="b", name="B", canvas="horizontal"),
                Scene(id="p", name="Frame", canvas="horizontal", kind="part")],
        active_scenes={"horizontal": "a"},
    )
    w = MainWindow(col, tmp_path / "scenes.json")
    return col, w


def test_add_menu_is_a_to_z_with_the_new_entries(tmp_path, monkeypatch):
    col, w = _window(tmp_path, monkeypatch)
    try:
        w.selected_scene_id = "p"  # sources are added inside a sprig
        w._fill_add_source_menu()
        labels = [a.text() for a in w.add_source_menu.actions() if a.text() and not a.isSeparator()]
        assert labels[0] == "Sprig" and labels[-1] == "Plugins"
        middle = labels[1:-1]
        assert middle == sorted(middle, key=str.lower)
        for name in ("Video capture", "Game capture", "Mirror", "Microphone", "Desktop sound",
                     "App sound"):
            assert name in middle
        assert "Camera" not in middle and not any(t.startswith("Sound (") for t in middle)
    finally:
        w.close()


def test_mirror_and_game_capture(tmp_path, monkeypatch):
    col, w = _window(tmp_path, monkeypatch)
    try:
        w._pick_bloom("b")
        w._add_mirror("bg")
        assert [i.source for i in col.scene("b").items] == ["bg"]  # the same leaf, no new source
        assert len(col.sources) == 1
        w._add_game()
        game = next(c for c in col.sources if c.kind == "game_capture")
        assert game.settings["mode"] == "any" and game.settings["cursor"] is False
        sound = next(c for c in col.sources if c.settings.get("capture_of") == game.id)
        assert sound.kind == "app_audio" and sound.settings["auto_game"] is True
    finally:
        w.close()


def test_game_capture_keeps_the_last_game_and_follows_a_new_one(monkeypatch):
    from sprigcast.sources import app_audio, capture

    src = capture.GameCaptureSource("g", {"mode": "any"})
    front = [(11, 1, "game1.exe")]
    monkeypatch.setattr(app_audio, "fullscreen_window", lambda: front[0])
    monkeypatch.setattr(capture, "_window_alive", lambda hwnd: True)
    assert src._target() == {"window_hwnd": 11}
    front[0] = None  # alt-tabbed to the desktop: keep the game
    assert src._target() == {"window_hwnd": 11} and not src._should_retarget()
    front[0] = (22, 2, "game2.exe")
    src._checked = 0.0
    assert src._should_retarget()
    assert src._target() == {"window_hwnd": 22}


def test_bloom_add_menu_offers_sprigs_only_and_sources_go_into_its_sprig(tmp_path, monkeypatch):
    col, w = _window(tmp_path, monkeypatch)
    try:
        w.selected_scene_id = "b"
        assert w._empty_hint(col.scene("b"))[0] == "Empty bloom"
        w._fill_add_source_menu()
        labels = [a.text() for a in w.add_source_menu.actions() if a.text() and not a.isSeparator()]
        assert labels == ["Frame", "New sprig…"]
        from sprigcast.core.scene import SourceConfig

        w._add_item(SourceConfig(id="t", kind="text", name="Title", settings={"text": "hi"}))
        show = col.scene("b")
        sprig = col.bloom_sprig(show)
        assert sprig is not None and sprig.name == "B" and len(show.items) == 1
        assert [i.source for i in sprig.items] == ["t"] and w._scene() is sprig  # opened to edit
        assert w._empty_hint(show) is None and w._empty_hint(sprig) is None
        w.selected_scene_id = "b"
        w._add_item(SourceConfig(id="t2", kind="text", name="Two", settings={"text": "2"}))
        assert [i.source for i in sprig.items] == ["t", "t2"] and len(show.items) == 1  # the same sprig
    finally:
        w.close()
