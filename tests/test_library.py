"""Build's library: blooms and sprigs in folders on the left (app/library.py)."""

import os

import pytest

from sprigcast.core.scene import PART_KIND, Canvas, Scene, SceneCollection, SceneItem, SourceConfig


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        sources=[SourceConfig(id="s-cam", kind=PART_KIND, name="Webcam frame", settings={"scene": "p1"})],
        scenes=[
            Scene(id="h1", name="Starting soon", canvas="horizontal"),
            Scene(id="h2", name="Just Chatting", canvas="horizontal", items=[SceneItem(source="s-cam")]),
            Scene(id="p1", name="Webcam frame", canvas="horizontal", kind="part"),
            Scene(id="p2", name="Alerts", canvas="horizontal", kind="part"),
            Scene(id="p2v", name="Alerts", canvas="vertical", kind="part", shape_of="p2"),
        ],
        active_scenes={"horizontal": "h1"},
    )


def test_sprig_folders_group_parts_apart_from_bloom_folders():
    col = _collection()
    main = col.add_folder("horizontal", "Main Scenes")
    browser = col.add_folder("horizontal", "Browser Scenes", holds="part")
    assert main.live and not browser.live and browser.holds == "part"
    assert col.folders_for("horizontal") == [main] and col.part_folders() == [browser]
    col.scene("p2").folder = browser.id
    assert [(f.name if f else None, [p.id for p in parts]) for f, parts in col.grouped_parts()] == [
        (None, ["p1"]), ("Browser Scenes", ["p2"])]
    # A bloom in a sprig folder, a sprig in a bloom folder, or another shape of a sprig is listed loose.
    data = col.model_dump()
    for scene in data["scenes"]:
        scene["folder"] = {"h1": browser.id, "p1": main.id, "p2v": browser.id}.get(scene["id"], scene["folder"])
    again = SceneCollection.model_validate(data)
    assert again.scene("h1").folder is None and again.scene("p1").folder is None
    assert again.scene("p2v").folder is None and again.scene("p2").folder == browser.id
    again.remove_folder(browser.id)
    assert again.scene("p2").folder is None


def _window(tmp_path, monkeypatch, name):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName(name)
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = _collection()
    return col, MainWindow(col, tmp_path / "scenes.json")


def test_library_lists_blooms_then_sprigs_and_files_sprigs_by_drag(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QInputDialog

    from sprigcast.app.library import PART_ROOT, ROLE_FOLDER, ROLE_ID, ROLE_ON_AIR, ROLE_ROW, ROLE_TAG

    col, w = _window(tmp_path, monkeypatch, "LibraryTest")
    try:
        w.show()
        blooms, sprigs = w.scene_tree.topLevelItem(0), w.scene_tree.topLevelItem(1)
        assert blooms.data(0, ROLE_ROW) == sprigs.data(0, ROLE_ROW) == "heading"
        assert sprigs.data(0, ROLE_FOLDER) == PART_ROOT
        assert [blooms.child(i).data(0, ROLE_ID) for i in range(blooms.childCount())] == ["h1", "h2"]
        assert blooms.child(0).data(0, ROLE_ON_AIR) and not blooms.child(1).data(0, ROLE_ON_AIR)
        assert [sprigs.child(i).data(0, ROLE_ID) for i in range(sprigs.childCount())] == ["p1", "p2"]
        assert sprigs.child(0).data(0, ROLE_TAG) == "in 1" and sprigs.child(1).data(0, ROLE_TAG) == "unused"
        # A sprig folder, made from the Sprigs + menu, then a new sprig lands in it.
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Browser Scenes", True))
        w._add_folder(holds="part")
        folder = col.part_folders()[0]
        assert w.selected_folder_id == folder.id and not col.folders_for("horizontal")
        sprigs = w.scene_tree.topLevelItem(1)
        holder = next(sprigs.child(i) for i in range(sprigs.childCount())
                      if sprigs.child(i).data(0, ROLE_FOLDER) == folder.id)
        # What a drag does to the rows: Alerts into the folder.
        alerts = next(sprigs.child(i) for i in range(sprigs.childCount()) if sprigs.child(i).data(0, ROLE_ID) == "p2")
        holder.addChild(sprigs.takeChild(sprigs.indexOfChild(alerts)))
        w._scenes_dropped()
        assert col.scene("p2").folder == folder.id and col.scene("p1").folder is None
        # A bloom dragged among the sprigs is put back.
        blooms, sprigs = w.scene_tree.topLevelItem(0), w.scene_tree.topLevelItem(1)
        sprigs.addChild(blooms.takeChild(0))
        w._scenes_dropped()
        assert col.scene("h1").folder is None
        assert w.scene_tree.topLevelItem(0).child(0).data(0, ROLE_ID) == "h1"
        # Search hides what doesn't match and opens folders that hold a match.
        col.folder(folder.id).collapsed = True
        w.library.search.setText("aler")
        blooms, sprigs = w.scene_tree.topLevelItem(0), w.scene_tree.topLevelItem(1)
        assert all(blooms.child(i).isHidden() for i in range(blooms.childCount()))
        holder = next(sprigs.child(i) for i in range(sprigs.childCount())
                      if sprigs.child(i).data(0, ROLE_FOLDER) == folder.id)
        assert not holder.isHidden() and holder.isExpanded() and col.folder(folder.id).collapsed
        w.library.search.clear()
        assert not w.scene_tree.topLevelItem(0).child(0).isHidden()
    finally:
        w.close()
        QSettings().clear()


def test_opening_a_sprig_says_where_it_shows_and_goes_back(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    col, w = _window(tmp_path, monkeypatch, "LibrarySprigTest")
    try:
        w.show()
        w._pick_bloom("h2")
        w.selected_scene_id = "p1"
        w._refresh_scenes()
        w._show_part_editor()
        assert w.center.currentIndex() == 1
        assert "in 1 bloom: Just Chatting" in w.part_uses_note.text()
        assert w.part_back.text() == "← Just Chatting"
        assert w.build_bar.where.text().endswith("Webcam frame")
        w._close_part()
        assert w.selected_scene_id == "h2" and w.center.currentIndex() == 0
    finally:
        w.close()
        QSettings().clear()
