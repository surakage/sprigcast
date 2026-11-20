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
    from sprigcast.app import card as popups

    from sprigcast.app.library import PART_ROOT, ROLE_FOLDER, ROLE_ID, ROLE_ON_AIR, ROLE_ROW, ROLE_TAG, ROLE_UNUSED

    col, w = _window(tmp_path, monkeypatch, "LibraryTest")
    try:
        w.show()
        blooms, sprigs = w.scene_tree.topLevelItem(0), w.scene_tree.topLevelItem(1)
        assert blooms.data(0, ROLE_ROW) == sprigs.data(0, ROLE_ROW) == "heading"
        assert sprigs.data(0, ROLE_FOLDER) == PART_ROOT
        assert [blooms.child(i).data(0, ROLE_ID) for i in range(blooms.childCount())] == ["h1", "h2"]
        assert blooms.child(0).data(0, ROLE_ON_AIR) and not blooms.child(1).data(0, ROLE_ON_AIR)
        assert [sprigs.child(i).data(0, ROLE_ID) for i in range(sprigs.childCount())] == ["p1", "p2"]
        # No "in 1" / "unused" words (user, 2026-11-19): an unused sprig is dimmed, the tooltip says it.
        assert not sprigs.child(0).data(0, ROLE_TAG) and not sprigs.child(1).data(0, ROLE_TAG)
        assert not sprigs.child(0).data(0, ROLE_UNUSED) and sprigs.child(1).data(0, ROLE_UNUSED)
        assert "not in any" in sprigs.child(1).toolTip(0)
        # A sprig folder, made from the Sprigs + menu, then a new sprig lands in it.
        monkeypatch.setattr(popups, "ask_text", lambda *a, **k: ("Browser Scenes", True))
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
        assert "in 1 bloom: " in w.part_uses_note.text() and '>Just Chatting</a>' in w.part_uses_note.text()
        assert w.part_back.text() == "← Just Chatting"
        assert w.build_bar.where.text().endswith("Webcam frame")
        w._close_part()
        assert w.selected_scene_id == "h2" and w.center.currentIndex() == 0
    finally:
        w.close()
        QSettings().clear()


def test_sprig_drops_copies_icons_and_links(tmp_path, monkeypatch):
    from PySide6.QtCore import QMimeData, QSettings
    from sprigcast.app import card as popups

    from sprigcast.app.build_stage import ROLE_KIND, ROLE_TAG
    from sprigcast.app.library import SPRIG_MIME

    col, w = _window(tmp_path, monkeypatch, "LibraryDropTest")
    try:
        w.show()
        w._pick_bloom("h1")
        # The library hands out the sprig's id when one is dragged...
        blooms, sprigs = w.scene_tree.topLevelItem(0), w.scene_tree.topLevelItem(1)
        data = w.scene_tree.mimeData([sprigs.child(1)])
        assert bytes(data.data(SPRIG_MIME)).decode() == "p2"
        assert not w.scene_tree.mimeData([blooms.child(0)]).hasFormat(SPRIG_MIME)
        # ...and dropping it on the canvas puts it in the bloom being edited.
        w.previews["horizontal"].sprig_dropped.emit("horizontal", "p2")
        placed = [col.source(i.source) for i in col.scene("h1").items]
        assert [c.settings.get("scene") for c in placed] == ["p2"]
        assert w.source_list.item(0).data(ROLE_KIND) == "empty"  # p2 holds nothing yet
        assert not w.source_list.item(0).data(ROLE_TAG)  # no type tags: you know what you added
        mime = QMimeData()
        mime.setData(SPRIG_MIME, b"p1")
        assert mime.hasFormat(SPRIG_MIME)
        w._sprig_dropped("", "nope")  # unknown ids are ignored
        assert len(col.scene("h1").items) == 1
        # Picking a sprig in Inside offers to open it and links the other blooms it's in.
        w._pick_bloom("h2")
        w.source_list.setCurrentRow(0)
        assert w.prop_sprig.isVisibleTo(w)
        assert "Only in this bloom" in w.prop_used_in.text()
        w.previews["horizontal"].sprig_dropped.emit("horizontal", "p2")
        w._pick_bloom("h1")
        w.source_list.setCurrentRow(0)
        assert 'href="h2"' in w.prop_used_in.text()
        w._jump_to_bloom("h2")
        assert w.selected_scene_id == "h2"
        # Copy a bloom: same things inside, but its sprig placements are its own.
        w._copy_bloom("h2")
        copy = col.scene(w.selected_scene_id)
        assert copy.name == "Just Chatting copy" and copy.id != "h2"
        assert len(copy.items) == len(col.scene("h2").items)
        assert {i.source for i in copy.items}.isdisjoint({i.source for i in col.scene("h2").items})
        assert [s.id for s in col.scenes].index(copy.id) == [s.id for s in col.scenes].index("h2") + 1
        # Make sprig from Inside's selection.
        monkeypatch.setattr(popups, "ask_text", lambda *a, **k: ("Pair", True))
        w.source_list.selectAll()
        w._turn_into_part()
        assert any(p.name == "Pair" for p in col.parts())
    finally:
        w.close()
        QSettings().clear()


def test_bloom_pictures_survive_a_restart(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtGui import QImage

    from sprigcast.app.library import ROLE_THUMB

    col, w = _window(tmp_path, monkeypatch, "LibraryThumbTest")
    try:
        image = QImage(32, 18, QImage.Format.Format_RGB32)
        image.fill(0x336699)
        w.live_panel.set_thumb("h2", image)
        w.scene_tree.set_thumb("h2", image)
        assert w.scene_tree.topLevelItem(0).child(1).data(0, ROLE_THUMB) == image
        w._save_thumbs()
        assert (tmp_path / "thumbs" / "h2.png").exists()
        w.live_panel.thumbs.clear()
        w._load_thumbs()
        assert w.live_panel.thumbs["h2"].width() == 32
    finally:
        w.close()
        QSettings().clear()


def test_a_click_on_a_row_that_just_went_does_nothing(tmp_path, monkeypatch):
    """The list is made again while the garden changes; a click landing on a row as it goes hands over no row
    at all (AttributeError: 'NoneType' object has no attribute 'data', PC 2026-11-19)."""
    _col, w = _window(tmp_path, monkeypatch, "LibraryGoneRow")
    try:
        w.scene_tree._toggle_folder(None, 0)
    finally:
        w.close()


def test_a_press_waits_for_the_release_so_a_drag_never_opens_the_row(tmp_path, monkeypatch):
    """Pressing a sprig to drag it onto the canvas opened it first, so the drop went into the sprig itself
    ("'T1' already contains this scene", PC 2026-11-20). A row now opens on release; a drag leaves it shut."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.library import ROLE_ID

    _col, w = _window(tmp_path, monkeypatch, "LibraryPressWaits")
    try:
        w.show()
        w._pick_bloom("h2")
        tree = w.scene_tree

        def row(scene_id):
            stack = [tree.invisibleRootItem()]
            while stack:
                item = stack.pop()
                if item.data(0, ROLE_ID) == scene_id:
                    return tree.visualItemRect(item).center()
                stack += [item.child(i) for i in range(item.childCount())]
            raise AssertionError(scene_id)

        QTest.mousePress(tree.viewport(), Qt.MouseButton.LeftButton, pos=row("p1"))
        assert w.selected_scene_id == "h2"  # held: could still be a drag
        QTest.mouseRelease(tree.viewport(), Qt.MouseButton.LeftButton, pos=row("p1"))
        assert w.selected_scene_id == "p1"  # a click: open it

        w._pick_bloom("h2")
        QTest.mousePress(tree.viewport(), Qt.MouseButton.LeftButton, pos=row("p2"))
        assert w.selected_scene_id == "h2"
        tree.holding = False  # what startDrag does once the drop is over
        tree.drag_ended.emit()
        QApplication.processEvents()
        assert w.selected_scene_id == "h2"
        assert [i.data(0, ROLE_ID) for i in tree.selectedItems()] == ["h2"]
    finally:
        w.close()


def test_new_text_starts_with_the_text_box_ready_to_type():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from sprigcast.app.main_window import source_class
    from sprigcast.app.settings_dialog import SettingsDialog

    cls = source_class("text")
    defaults = dict(cls.defaults)
    dialog = SettingsDialog("New Text", defaults, defaults, None, cls=cls)
    dialog.show()
    app.processEvents()
    assert dialog.focusWidget() is dialog.fields["text"]
    dialog.close()
