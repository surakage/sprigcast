"""Build made easier: shape tabs, Sound in the library, foldable inspector, smart + Add, undo,
plain problems and the starter kit."""

import os

import pytest

from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        sources=[SourceConfig(id="mic", kind="audio_input", name="Mic", settings={"device": "default"}),
                 SourceConfig(id="bg", kind="color", name="Moss", settings={"color": "#00ff00"})],
        scenes=[Scene(id="h1", name="Main", canvas="horizontal", items=[SceneItem(source="bg")]),
                Scene(id="v1", name="Main (vertical)", canvas="vertical", linked_to="h1")],
        active_scenes={"horizontal": "h1", "vertical": "v1"},
    )


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


def test_plain_problems_say_what_to_do():
    from sprigcast.app.names import plain_problem

    assert plain_problem("ok", "camera") is None and plain_problem("paused (not on screen)", "camera") is None
    text, fix = plain_problem("camera busy", "camera")
    assert "Another app is using this camera" in text and fix == "retry"
    assert plain_problem("window not found", "window_capture")[1] == "pick"
    assert plain_problem("device disconnected", "audio_input")[0].startswith("The mic was unplugged")
    assert plain_problem("waiting for first frame", "camera")[1] == ""


def test_starter_kit_makes_blooms_once_per_sound(tmp_path):
    from sprigcast.app.starter import StarterChoice, build_starter, is_fresh

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        sources=[SourceConfig(id="mic", kind="audio_input", name="Mic", settings={"device": "default"}),
                 SourceConfig(id="bg-moss", kind="color", name="Moss", settings={})],
        scenes=[Scene(id="starting", name="Starting soon", canvas="horizontal", items=[SceneItem(source="bg-moss")]),
                Scene(id="starting-v", name="Starting soon (vertical)", canvas="vertical")],
        active_scenes={"horizontal": "starting", "vertical": "starting-v"},
    )
    assert is_fresh(col)
    made = build_starter(col, StarterChoice(
        camera={"name": "Cam", "path": "p", "width": 1280, "height": 720, "fps": 30, "mirror": False},
        monitor=(1, 2560, 1440), mic="default"))
    shows = [col.scene(sid) for sid in made]
    assert [s.name for s in shows if s.canvas == "horizontal"] == [
        "Starting soon", "Just Chatting", "Gaming", "Be right back", "Ending"]
    assert len([s for s in shows if s.canvas == "vertical"]) == 5
    assert all(s.linked_to for s in shows if s.canvas == "vertical")
    assert "starting" not in {s.id for s in col.scenes} and "bg-moss" not in {c.id for c in col.sources}
    assert [p.name for p in col.parts()] == ["Webcam"]
    assert [c.kind for c in col.sources].count("audio_input") == 1  # the mic was already there
    assert [c.kind for c in col.sources].count("audio_output") == 1
    folders = col.folders_for("horizontal")
    assert [f.name for f in folders] == ["Main Scenes"] and folders[0].live
    assert col.active_scenes["horizontal"] == made[0]
    again = SceneCollection.from_json(col.model_dump_json())  # what it made is valid
    assert len(again.scenes) == len(col.scenes)
    assert not is_fresh(col)


def test_build_tabs_sound_sections_add_undo(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    from sprigcast.app import main_window as mw
    from sprigcast.app.library import ROLE_ID, ROLE_ROW, SOUND_ROOT, ROLE_FOLDER

    col, w = _window(tmp_path, monkeypatch, "BuildEasyTest")
    try:
        w.show()
        # One shape at a time: the other shape's canvas isn't on screen.
        assert w.canvas_tabs.isVisible()
        assert w.canvas_columns["horizontal"].isVisible() and not w.canvas_columns["vertical"].isVisible()
        w.canvas_tabs.setCurrentIndex(1)
        assert w.canvas_columns["vertical"].isVisible() and not w.canvas_columns["horizontal"].isVisible()
        # Sound is listed once, under its own heading, on both tabs.
        sound = w.scene_tree.topLevelItem(2)
        assert sound.data(0, ROLE_FOLDER) == SOUND_ROOT
        assert [sound.child(i).data(0, ROLE_ID) for i in range(sound.childCount())] == ["mic"]
        w.canvas_tabs.setCurrentIndex(0)
        sound = w.scene_tree.topLevelItem(2)
        assert sound.childCount() == 1 and sound.child(0).data(0, ROLE_ROW) == "sound"
        # Adding the same mic again doesn't double it; it opens its controls instead.
        w._add_audio("audio_input", "default")
        assert [c.kind for c in col.sources].count("audio_input") == 1
        assert w.build_stage.right_stack.currentIndex() == 1 and w.build_tune.sound_id == "mic"
        w.build_tune.mute.click()
        assert col.source("mic").settings["muted"] is True
        w._close_build_sound()
        assert w.build_stage.right_stack.currentIndex() == 0
        # The inspector folds: only Place starts open, and it remembers.
        header, body = w.prop_section_toggles["Look"]
        assert not body.isVisibleTo(w) and w.prop_section_toggles["Place"][1].isVisibleTo(w) is not None
        header.click()
        assert QSettings().value("inspector/open/Look", type=bool)
        # + Add lists real devices (here: none found, so it says so).
        monkeypatch.setattr(mw, "list_cameras", lambda: [])
        w._fill_add_source_menu()
        titles = [a.text() for a in w.add_source_menu.actions()]
        assert "Camera" in titles and "Screen" in titles and "Picture…" in titles
        # Undo and redo the whole collection.
        w._copy_bloom("h1")
        assert len(col.scenes_for("horizontal")) == 2
        w.undo()
        assert [s.id for s in col.scenes_for("horizontal")] == ["h1"]
        w.redo()
        assert len(col.scenes_for("horizontal")) == 2
        w.undo()
        w.undo()  # the mute is undone too
        assert not col.source("mic").settings.get("muted")
    finally:
        w.close()
        QSettings().clear()


def test_tabs_hide_with_one_shape_and_hints_fade(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    col, w = _window(tmp_path, monkeypatch, "BuildEasyOneShape")
    try:
        w.show()
        w.show_canvases("horizontal")
        assert not w.canvas_tabs.isVisible()
        assert not w.inside_tools.isVisible()  # nothing picked in Inside: no tool row
        w.source_list.setCurrentRow(0)
        assert w.inside_tools.isVisible()
        assert w.inside_hint.isVisible() and not w.learned
    finally:
        w.close()
    QSettings().setValue("build/sessions", 9)
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_collection(), tmp_path / "scenes2.json")
    try:
        w.show()
        assert w.learned and not w.inside_hint.isVisible() and not w.prop_empty.isVisible()
    finally:
        w.close()
        QSettings().clear()
