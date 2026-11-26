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
        # User, 2026-11-20: professional names, and each entry adds one leaf (double-click picks what it shows)
        # Sounds are added from the Sound card's + (user, 2026-11-20), so they're not listed here
        assert middle == ["Android Screen", "Browser", "Chat", "Color", "Display Capture", "Game Capture", "Image", "Mirror", "Phone Camera", "Text",
                          "Video Capture", "Video File", "Window Capture"]
        submenus = [a.text() for a in w.add_source_menu.actions() if a.menu() is not None]
        assert submenus == labels  # every kind opens to New… and what you have of it
        assert all(not a.icon().isNull() for a in w.add_source_menu.actions() if a.menu() is not None)
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


def test_add_puts_in_a_blank_leaf_and_double_click_picks_what_it_shows(tmp_path, monkeypatch):
    """User, 2026-11-20: "one leaf that you add then you double click to add the source"."""
    col, w = _window(tmp_path, monkeypatch)
    try:
        w.selected_scene_id = "p"
        w._refresh_scenes()
        w._fill_add_source_menu()
        for label in ("Window Capture", "Display Capture", "Video Capture", "Image", "Video File", "Browser"):
            kind = next(k for n, k in __import__("sprigcast.app.main_window", fromlist=["x"]).BLANK_KINDS.items()
                        if n == label)
            w._add_blank(kind, label)
        added = {c.name: c for c in col.sources if c.settings.get("blank")}
        assert set(added) == {"Window Capture", "Display Capture", "Video Capture", "Image", "Video File", "Browser"}
        assert all(any(i.source == c.id for i in col.scene("p").items) for c in added.values())  # leaves now
        assert added["Window Capture"].settings["title"] == "" and added["Video Capture"].settings["name"] == ""

        # Double-click the display capture: pick monitor 2, and it's named after it
        display = added["Display Capture"]
        w.selected_scene_id = "p"
        index = next(n for n, i in enumerate(col.scene("p").items) if i.source == display.id)
        monkeypatch.setattr(w, "_item", lambda: col.scene("p").items[index])
        monkeypatch.setattr(w, "_display_dialog", lambda settings: {"monitor": 2, "cursor": True})
        w._edit_source_settings()
        assert display.name == "Monitor 2" and "blank" not in display.settings
    finally:
        w.close()


def test_each_kind_opens_to_new_and_what_you_have(tmp_path, monkeypatch):
    """User, 2026-11-20 (a video of the menu they liked): + Add → a kind → New… or one you already have.
    New… puts the leaf in and opens its picker at once."""
    from PySide6.QtWidgets import QApplication, QMenu

    col, w = _window(tmp_path, monkeypatch)
    try:
        w.selected_scene_id = "p"
        w._refresh_scenes()
        w._fill_add_source_menu()
        color = next(a.menu() for a in w.add_source_menu.actions() if a.text() == "Color")
        color.aboutToShow.emit()
        names = [a.text() for a in color.actions() if not a.isSeparator()]
        assert names == ["New…", "Moss"]  # Moss is in bloom A, not in this sprig
        color.actions()[-1].trigger()
        assert [i.source for i in col.scene("p").items] == ["bg"]  # the same leaf again
        assert len(col.sources) == 1

        opened = []
        monkeypatch.setattr(w, "_display_dialog", lambda settings: opened.append(1) or {"monitor": 2, "cursor": True})
        display = next(a.menu() for a in w.add_source_menu.actions() if a.text() == "Display Capture")
        display.aboutToShow.emit()
        assert [a.text() for a in display.actions()] == ["New…"]
        display.actions()[0].trigger()
        QApplication.processEvents()
        added = next(c for c in col.sources if c.kind == "display_capture")
        assert opened == [1] and added.name == "Monitor 2" and "blank" not in added.settings
        assert isinstance(display, QMenu)

        # Cancel in New…'s pop-up leaves nothing behind (PC test of #796)
        monkeypatch.setattr(w, "_window_dialog", lambda settings, had_sound: None)
        count = len(col.sources), len(col.scene("p").items)
        w._add_new("window_capture", "Window Capture")
        QApplication.processEvents()
        assert (len(col.sources), len(col.scene("p").items)) == count
        from sprigcast.app.capture_dialog import DisplayCaptureDialog

        dialog = DisplayCaptureDialog({"monitor": 1}, [])
        assert dialog.screen.accessibleName() == "Screen" and dialog.cursor.accessibleName() == "Mouse pointer"
    finally:
        w.close()


def test_a_sound_inspector_changes_its_source(tmp_path, monkeypatch):
    """User, 2026-11-20: sounds stay in Sound; "add the ability to change the source in its inspector, so I
    can change the mic to a different"."""
    from sprigcast.app import media_dialog
    from sprigcast.core.scene import SourceConfig

    col, w = _window(tmp_path, monkeypatch)
    try:
        col.sources.append(SourceConfig(id="mic", kind="audio_input", name="Mic",
                                        settings={"device": "default", "volume_db": -6.0, "muted": False}))
        col.sources.append(SourceConfig(id="song", kind="media", name="Rain",
                                        settings={"path": "C:/Music/Rain.mp3", "volume_db": 0.0, "muted": False}))
        mic, song = col.source("mic"), col.source("song")
        assert w._sound_source_text(mic) == "Default (follows Windows)" and w._sound_source_text(song) == "Rain.mp3"
        monkeypatch.setattr(w, "_pick_audio_device", lambda kind, current, title, primary: "Headset Mic (USB)")
        w._change_sound_source("mic")
        assert mic.settings["device"] == "Headset Mic (USB)" and mic.settings["volume_db"] == -6.0  # the rest stays
        assert "Headset" in w._sound_source_text(mic)

        shown = []

        def fake_modal(dialog):
            shown.append((dialog.only, dialog.path.placeholderText()))
            dialog.path.setText("C:/Music/Storm_Loop.wav")
            return True

        monkeypatch.setattr("sprigcast.app.main_window.modal", fake_modal)
        w._change_sound_source("song")
        assert shown == [("sound", "Pick a sound file")]  # only sound files
        assert song.settings["path"] == "C:/Music/Storm_Loop.wav" and song.name == "Storm Loop"
        assert media_dialog.MediaDialog("Video File", {}, only="video").path.placeholderText() == "Pick a video"

        from sprigcast.app.sound_card import TunePage

        page = TunePage(lambda *a: None, lambda sid: None, lambda: None, lambda sid: None)
        page.show_sound(w._sound_item(mic))
        assert page.apps_card.title() == "Source" and not page.apps_card.isHidden()
    finally:
        w.close()


def test_a_sprigs_icon_follows_its_leaves(tmp_path, monkeypatch):
    """User's PC, 2026-11-21: a leaf was added to an empty sprig and its icon in Sprigs still showed the +."""
    from sprigcast.app.main_window import ROLE_ID, ROLE_SOURCE_KIND
    from sprigcast.core.scene import SourceConfig

    col, w = _window(tmp_path, monkeypatch)
    try:
        w.selected_scene_id = "p"
        w._refresh_scenes()

        def icon_kind():
            stack = [w.scene_tree.topLevelItem(i) for i in range(w.scene_tree.topLevelItemCount())]
            while stack:
                row = stack.pop()
                stack.extend(row.child(i) for i in range(row.childCount()))
                if row.data(0, ROLE_ID) == "p":
                    return row.data(0, ROLE_SOURCE_KIND)

        assert icon_kind() == "empty"
        w._add_item(SourceConfig(id="cam", kind="camera", name="Facecam", settings={"name": "Facecam", "path": ""}))
        assert icon_kind() == "camera"
        w.source_list.setCurrentRow(0)
        w._remove_item()
        assert icon_kind() == "empty"
    finally:
        w.close()
