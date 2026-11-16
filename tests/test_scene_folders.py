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
        # The first folder is the Live folder: Live lists only what's in it.
        assert col.folder(folder.id).live
        w.live_panel.show_scenes(col)
        assert set(w.live_panel.buttons) == {"h2", new.id, "v1"}  # vertical has no folders: all its blooms
        w._set_folder_live(folder.id, False)
        w.live_panel.show_scenes(col)
        assert set(w.live_panel.buttons) == {"h1", "h2", "h3", new.id, "v1"}  # a list per canvas
        w._remove_folder(folder.id)
        assert not col.folders and col.scene("h2").folder is None
    finally:
        w.close()
        QSettings().clear()


def test_live_lists_only_live_folders():
    col = _collection()
    main = col.add_folder("horizontal", "Main Scenes")
    other = col.add_folder("horizontal", "Browser Scenes")
    assert main.live and not other.live  # only the first folder starts as Live
    col.scene("h1").folder = main.id
    col.scene("h3").folder = other.id
    assert [(f.id, [s.id for s in shows]) for f, shows in col.live_scenes("horizontal")] == [(main.id, ["h1"])]
    col.scene("h2").folder = main.id  # adding a bloom to Main Scenes makes it a Live button
    assert [s.id for s in col.live_scenes("horizontal")[0][1]] == ["h1", "h2"]
    other.live = True
    assert [f.id for f, _ in col.live_scenes("horizontal")] == [main.id, other.id]


def test_dragging_blooms_reorders_and_files_them(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("DragTest")
    QSettings().clear()
    from sprigcast.app.main_window import ROLE_FOLDER, ROLE_ID, MainWindow

    col = _collection()
    folder = col.add_folder("horizontal", "Main Scenes")
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w._refresh_scenes()
        top = w.scene_tree.topLevelItem(0)
        rows = {top.child(i).data(0, ROLE_ID) or top.child(i).data(0, ROLE_FOLDER): top.child(i)
                for i in range(top.childCount())}
        # What a drag does to the rows: Alerts into the folder, Gameplay to the top.
        holder = rows[folder.id]
        holder.addChild(top.takeChild(top.indexOfChild(rows["h3"])))
        top.insertChild(0, top.takeChild(top.indexOfChild(rows["h2"])))
        w._scenes_dropped()
        assert [s.id for s in col.scenes_for("horizontal")] == ["h2", "h1", "h3"]
        assert col.scene("h3").folder == folder.id and col.scene("h2").folder is None
        # A drop onto a bloom (it would nest) is undone.
        top = w.scene_tree.topLevelItem(0)
        first, second = top.child(0), top.child(1)
        first.addChild(top.takeChild(top.indexOfChild(second)))
        w._scenes_dropped()
        assert [s.id for s in col.scenes_for("horizontal")] == ["h2", "h1", "h3"]
        assert w.scene_tree.topLevelItem(0).child(1).data(0, ROLE_ID) == "h1"  # list rebuilt as before
    finally:
        w.close()
        QSettings().clear()


def test_dragging_folders_reorders_them_and_sprigs_move_between_folders(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings, Qt
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("DragFolderTest")
    QSettings().clear()
    from sprigcast.app.library import PART_ROOT
    from sprigcast.app.main_window import ROLE_FOLDER, ROLE_ID, MainWindow
    from sprigcast.core.scene import Scene

    col = _collection()
    first = col.add_folder("horizontal", "Main Scenes")
    second = col.add_folder("horizontal", "Extras")
    nested = col.add_folder("horizontal", "Nested Scenes", holds="part")
    browser = col.add_folder("horizontal", "Browser Scenes", holds="part")
    col.scenes.append(Scene(id="p1", name="Webcam", canvas="horizontal", kind="part", folder=nested.id))
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w._refresh_scenes()

        def heading(key):
            return next(w.scene_tree.topLevelItem(i) for i in range(w.scene_tree.topLevelItemCount())
                        if w.scene_tree.topLevelItem(i).data(0, ROLE_FOLDER) == key)

        def child(holder, key):
            return next(holder.child(i) for i in range(holder.childCount())
                        if key in (holder.child(i).data(0, ROLE_ID), holder.child(i).data(0, ROLE_FOLDER)))

        top = heading("")
        assert child(top, first.id).flags() & Qt.ItemFlag.ItemIsDragEnabled
        top.insertChild(0, top.takeChild(top.indexOfChild(child(top, second.id))))  # Extras above Main
        w._scenes_dropped()
        assert [f.id for f in col.folders_for("horizontal")] == [second.id, first.id]
        sprigs = heading(PART_ROOT)
        from_folder = child(sprigs, nested.id)
        child(sprigs, browser.id).addChild(from_folder.takeChild(0))  # Webcam into Browser Scenes
        w._scenes_dropped()
        assert col.scene("p1").folder == browser.id
        sprigs = heading(PART_ROOT)
        holder = child(sprigs, browser.id)
        sprigs.addChild(holder.takeChild(0))  # and out of any folder
        w._scenes_dropped()
        assert col.scene("p1").folder is None
        sprigs = heading(PART_ROOT)
        child(sprigs, browser.id).addChild(sprigs.takeChild(sprigs.indexOfChild(child(sprigs, nested.id))))
        w._scenes_dropped()  # a folder in a folder is undone
        assert [f.id for f in col.part_folders()] == [nested.id, browser.id]
        top = heading("")
        heading(PART_ROOT).addChild(top.takeChild(top.indexOfChild(child(top, first.id))))
        w._scenes_dropped()  # a bloom folder among the sprigs is undone
        assert first.holds == "show" and [f.id for f in col.folders_for("horizontal")] == [second.id, first.id]
    finally:
        w.close()
        QSettings().clear()


def test_folder_colour_row_picks_a_colour():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import QPointF, QEvent, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QMenu

    QApplication.instance() or QApplication([])
    from sprigcast.app.card import ColourRow, colour_row
    from sprigcast.core.scene import FOLDER_COLOURS

    picked = []
    menu = QMenu()
    action = colour_row(menu, FOLDER_COLOURS[0], picked.append)
    row = action.defaultWidget()
    assert isinstance(row, ColourRow) and row.current == FOLDER_COLOURS[0]
    centre = row._rect(2).center()
    row.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(centre), QPointF(centre),
                                      Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                                      Qt.KeyboardModifier.NoModifier))
    assert picked == [FOLDER_COLOURS[2]] and row.current == FOLDER_COLOURS[2]
