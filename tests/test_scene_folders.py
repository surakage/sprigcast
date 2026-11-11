import os

import pytest

from sprigcast.core.scene import FOLDER_COLOURS, Canvas, Scene, SceneCollection


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        scenes=[
            Scene(id="h1", name="Starting Soon", canvas="horizontal"),
            Scene(id="h2", name="Gameplay", canvas="horizontal"),
            Scene(id="h3", name="Alerts", canvas="horizontal"),
            Scene(id="v1", name="Starting Soon", canvas="vertical", linked_to="h1"),
        ],
        active_scenes={"horizontal": "h1", "vertical": "v1"},
    )


def test_folders_group_shows_and_survive_a_round_trip():
    col = _collection()
    main = col.add_folder("horizontal", "Main Scenes")
    browser = col.add_folder("horizontal", "Browser Scenes")
    assert (main.colour, browser.colour) == FOLDER_COLOURS[:2]  # each new folder gets the next colour
    col.scene("h1").folder = main.id
    col.scene("h2").folder = main.id
    col.scene("h3").folder = browser.id
    groups = col.grouped_scenes("horizontal")
    assert [(f.name if f else None, [s.id for s in shows]) for f, shows in groups] == [
        (None, []), ("Main Scenes", ["h1", "h2"]), ("Browser Scenes", ["h3"])]
    assert col.grouped_scenes("vertical") == [(None, [col.scene("v1")])]
    again = SceneCollection.from_json(col.model_dump_json())
    assert again.scene("h3").folder == browser.id and again.folder(main.id).name == "Main Scenes"
    col.remove_folder(main.id)
    assert col.scene("h1").folder is None and col.scene("h2").folder is None  # shows stay, listed loose
    assert [f.id for f in col.folders] == [browser.id]


def test_a_folder_on_another_canvas_or_missing_is_ignored():
    data = _collection().model_dump()
    data["folders"] = [{"id": "folder-1", "name": "Main", "canvas": "horizontal"}]
    data["scenes"][3]["folder"] = "folder-1"  # the vertical scene can't sit in a horizontal folder
    data["scenes"][0]["folder"] = "gone"
    col = SceneCollection.model_validate(data)
    assert col.scene("v1").folder is None and col.scene("h1").folder is None


def test_scenes_list_and_live_panel_show_folders(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication, QInputDialog

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("FoldersTest")
    QSettings().clear()
    from sprigcast.app.main_window import ROLE_FOLDER, ROLE_ID, MainWindow

    col = _collection()
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        names = iter([("Main Scenes", True), ("Just Chatting", True)])
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: next(names))
        w._add_folder()
        folder = col.folders[0]
        assert w.selected_folder_id == folder.id
        w._add_scene()  # lands in the folder that's selected
        new = col.scene(w.selected_scene_id)
        assert new.folder == folder.id and new.canvas == "horizontal"
        w._move_to_folder("h2", folder.id)
        top = w.scene_tree.topLevelItem(0)
        rows = [top.child(i) for i in range(top.childCount())]
        assert [r.data(0, ROLE_ID) for r in rows[:2]] == ["h1", "h3"]  # loose ones first
        holder = rows[2]
        assert holder.data(0, ROLE_FOLDER) == folder.id
        assert [holder.child(i).data(0, ROLE_ID) for i in range(holder.childCount())] == ["h2", new.id]
        w._move_scene(new.id, -1)
        assert [s.id for s in col.grouped_scenes("horizontal")[1][1]] == [new.id, "h2"]
        holder = w.scene_tree.topLevelItem(0).child(2)
        holder.setExpanded(False)
        assert col.folder(folder.id).collapsed
        w._recolour_folder(folder.id, FOLDER_COLOURS[2])
        assert col.folder(folder.id).colour == FOLDER_COLOURS[2]
        w.live_panel.show_scenes(col)
        assert set(w.live_panel.buttons) >= {"h1", "h2", "h3", new.id}
        w._remove_folder(folder.id)
        assert not col.folders and col.scene("h2").folder is None
    finally:
        w.close()
        QSettings().clear()
