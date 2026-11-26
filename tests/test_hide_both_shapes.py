"""Hide and Show on a leaf change it in every shape of its bloom or sprig, like the Control API (PC 2026-11-26)."""

import os

import pytest

pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_hide_and_the_eye_change_the_leaf_on_both_canvases(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings, Qt
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    _app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastHideTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        sources=[SourceConfig(id="cam", kind="color", name="Webcam", settings={"color": "#224433ff"}),
                 SourceConfig(id="ps-1", kind="scene", name="Show", settings={"scene": "p"}),
                 SourceConfig(id="ps-2", kind="scene", name="Show", settings={"scene": "p"})],
        scenes=[Scene(id="a", name="Show", canvas="horizontal", items=[SceneItem(source="ps-1")]),
                Scene(id="av", name="Show", canvas="vertical", linked_to="a", items=[SceneItem(source="ps-2")]),
                Scene(id="p", name="Show", canvas="horizontal", kind="part", items=[SceneItem(source="cam")]),
                Scene(id="pv", name="Show", canvas="vertical", kind="part", shape_of="p",
                      items=[SceneItem(source="cam", x=100)])],
        active_scenes={"horizontal": "a", "vertical": "av"},
    )
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w.selected_scene_id = "p"
        w._refresh_sources()
        w.source_list.setCurrentRow(0)
        w.item_actions["hide"].trigger()
        c = w.collection
        assert not c.scene("p").items[0].visible and not c.scene("pv").items[0].visible
        w._refresh_sources()
        w.source_list.item(0).setCheckState(Qt.CheckState.Checked)  # the row's eye
        assert c.scene("p").items[0].visible and c.scene("pv").items[0].visible
        w.selected_scene_id = "a"  # a bloom's sprig: its vertical twin's placement too
        w._refresh_sources()
        w.source_list.setCurrentRow(0)
        w.item_actions["hide"].trigger()
        assert not c.scene("a").items[0].visible and not c.scene("av").items[0].visible
    finally:
        w.close()


def test_hide_reaches_a_vertical_bloom_that_shows_its_own_sprig(tmp_path, monkeypatch):
    """PC 2026-11-26 (build 45735049801): the vertical bloom shows a sprig of its own with its own Webcam, so
    Hide still left it showing there. Found as the Control API finds it: in each linked bloom's sprig, by name."""
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    _app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastHideTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        sources=[SourceConfig(id="cam", kind="color", name="Webcam", settings={"color": "#224433ff"}),
                 SourceConfig(id="cam-v", kind="color", name="Webcam", settings={"color": "#224433ff"}),
                 SourceConfig(id="other", kind="color", name="Frame", settings={"color": "#000000ff"}),
                 SourceConfig(id="ps-1", kind="scene", name="Show", settings={"scene": "p"}),
                 SourceConfig(id="ps-2", kind="scene", name="Show", settings={"scene": "q"})],
        scenes=[Scene(id="a", name="Show", canvas="horizontal", items=[SceneItem(source="ps-1")]),
                Scene(id="av", name="Show", canvas="vertical", linked_to="a", items=[SceneItem(source="ps-2")]),
                Scene(id="p", name="Show", canvas="horizontal", kind="part", items=[SceneItem(source="cam")]),
                Scene(id="q", name="Show", canvas="vertical", kind="part",
                      items=[SceneItem(source="other"), SceneItem(source="cam-v")])],
        active_scenes={"horizontal": "a", "vertical": "av"},
    )
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        c = w.collection
        w._open_bloom_sprig(c.scene("a"))  # Build: the bloom's leaves
        w._refresh_sources()
        w.source_list.setCurrentRow(0)
        w.item_actions["hide"].trigger()
        assert not c.scene("p").items[0].visible
        assert [i.visible for i in c.scene("q").items] == [True, False]  # its Webcam, not the Frame
        w.item_actions["hide"].trigger()  # Show
        assert c.scene("p").items[0].visible and c.scene("q").items[1].visible
    finally:
        w.close()
