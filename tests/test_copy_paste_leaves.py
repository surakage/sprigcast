"""Copy and paste leaves from one sprig to another (user 2026-11-26), with their place, crop and effects."""

import os

import pytest

pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _window(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    _app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastCopyPasteTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import Canvas, Filter, Scene, SceneCollection, SceneItem, SourceConfig

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        sources=[
            SourceConfig(id="cam", kind="camera", name="Facecam", filters=[Filter(id="f1", kind="gain", name="Gain")]),
            SourceConfig(id="text-1", kind="text", name="Title", settings={"text": "Hi"},
                         filters=[Filter(id="f2", kind="color_correction", name="Tone")]),
            SourceConfig(id="part-src-1", kind="scene", name="Corner", settings={"scene": "p2"}),
            SourceConfig(id="part-src-2", kind="scene", name="Main", settings={"scene": "p1"}),
        ],
        scenes=[Scene(id="a", name="Main", canvas="horizontal", items=[SceneItem(source="part-src-2")]),
                Scene(id="p1", name="Main", canvas="horizontal", kind="part",
                      items=[SceneItem(source="cam", x=1400, y=700, scale=0.4, crop=(10, 0, 10, 0)),
                             SceneItem(source="text-1", x=40, y=40),
                             SceneItem(source="part-src-1", x=5, y=6)]),
                Scene(id="p2", name="Corner", canvas="horizontal", kind="part")],
        active_scenes={"horizontal": "a"},
    )
    w = MainWindow(col, tmp_path / "scenes.json")
    return col, w


def _select(w, scene_id, *sources):
    w.selected_scene_id = scene_id
    w._refresh_sources()
    scene = w._scene()
    w.source_list.clearSelection()
    for n, item in enumerate(scene.items):
        if item.source in sources:
            row = len(scene.items) - 1 - n
            w.source_list.setCurrentRow(row)
            w.source_list.item(row).setSelected(True)


def test_a_copied_leaf_pastes_into_another_sprig_where_it_was(tmp_path, monkeypatch):
    col, w = _window(tmp_path, monkeypatch)
    try:
        _select(w, "p1", "cam", "text-1")
        w.item_actions["copy"].trigger()
        assert len(w._leaf_clipboard) == 2
        _select(w, "p2")
        count = len(col.sources)
        w.item_actions["paste"].trigger()
        p2 = col.scene("p2")
        assert [i.source for i in p2.items] == ["cam", "text-1"]  # the same leaves (they run once), stacked the same
        cam = p2.items[0]
        assert (cam.x, cam.y, cam.scale, cam.crop) == (1400, 700, 0.4, (10, 0, 10, 0))
        assert len(col.sources) == count
        assert cam is not col.scene("p1").items[0]  # its own placement: moving it here doesn't move the first
        w.undo()
        assert not col.scene("p2").items if w.collection is col else not w.collection.scene("p2").items
    finally:
        w.close()


def test_paste_as_its_own_copy_makes_a_separate_text_but_shares_the_camera(tmp_path, monkeypatch):
    col, w = _window(tmp_path, monkeypatch)
    try:
        _select(w, "p1", "cam", "text-1")
        w.item_actions["copy"].trigger()
        _select(w, "p2")
        w._paste_leaves(own=True)
        col = w.collection
        sources = [i.source for i in col.scene("p2").items]
        assert sources[0] == "cam"  # a camera opened twice breaks: always shared
        text = col.source(sources[1])
        assert text.id != "text-1" and text.name == "Title 2" and text.kind == "text"
        assert [f.kind for f in text.filters] == ["color_correction"]
        text.settings["text"] = "Bye"
        text.filters.clear()
        assert col.source("text-1").settings["text"] == "Hi" and col.source("text-1").filters  # on its own
    finally:
        w.close()


def test_a_sprig_never_pastes_into_itself_and_a_removed_leaf_comes_back(tmp_path, monkeypatch):
    from sprigcast.app import main_window

    told = []
    monkeypatch.setattr(main_window.msgbox, "information", lambda *a, **k: told.append(a[2]))
    col, w = _window(tmp_path, monkeypatch)
    try:
        _select(w, "p1", "part-src-1")  # the Corner sprig, placed in Main
        w.item_actions["copy"].trigger()
        _select(w, "p2")
        w.item_actions["paste"].trigger()
        assert not w.collection.scene("p2").items and told  # Corner can't go inside Corner
        _select(w, "p1", "text-1")
        w.item_actions["copy"].trigger()
        w._remove_item()
        assert all(s.id != "text-1" for s in w.collection.sources)
        _select(w, "p2")
        w.item_actions["paste"].trigger()
        assert w.collection.source("text-1").settings["text"] == "Hi"
        assert [i.source for i in w.collection.scene("p2").items] == ["text-1"]
    finally:
        w.close()


def test_the_leaves_menu_offers_copy_and_paste(tmp_path, monkeypatch):
    col, w = _window(tmp_path, monkeypatch)
    try:
        _select(w, "p1", "text-1")
        texts = [a.text() for a in w._source_menu(True).actions()]
        assert "Copy" in texts and "Paste" not in texts and "Duplicate" not in texts
        assert not w.item_actions["paste"].isEnabled()
        w.item_actions["copy"].trigger()
        texts = [a.text() for a in w._source_menu(False).actions()]
        assert "Paste" in texts and "Duplicate" in texts
        assert w.item_actions["paste"].isEnabled()
    finally:
        w.close()


def test_a_sprig_pastes_with_its_own_placement_and_a_bloom_takes_leaves_into_its_sprig(tmp_path, monkeypatch):
    col, w = _window(tmp_path, monkeypatch)
    try:
        _select(w, "p1", "part-src-1", "cam")
        w.item_actions["copy"].trigger()
        _select(w, "a")  # a bloom: leaves go into the sprig it shows
        w.item_actions["paste"].trigger()
        col = w.collection
        assert [i.source for i in col.scene("a").items] == ["part-src-2"]
        p1 = col.scene("p1").items
        assert p1[3].source == "cam"
        placement = col.source(p1[4].source)
        assert placement.id != "part-src-1" and placement.kind == "scene" and placement.settings["scene"] == "p2"
    finally:
        w.close()
