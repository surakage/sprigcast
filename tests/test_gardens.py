"""Gardens (scene collections) and paths (profiles): core/gardens.py."""

from importlib import resources

import pytest

from sprigcast.core.gardens import (
    PathSettings,
    Shelf,
    ShelfError,
    blank_garden,
    missing_files,
    replace_file,
)
from sprigcast.core.scene import Output, SceneCollection


def _starter() -> SceneCollection:
    text = resources.files("sprigcast.data").joinpath("default_scenes.json").read_text(encoding="utf-8")
    col = SceneCollection.from_json(text)
    col.wrap_blooms()
    return col


@pytest.fixture
def setup(tmp_path):
    col = _starter()
    col.save(tmp_path / "scenes.json")
    shelf = Shelf(tmp_path)
    shelf.sync_path(col)
    return tmp_path, shelf, col


def test_first_run_makes_one_of_each_from_the_existing_setup(setup):
    folder, shelf, col = setup
    assert [g.name for g in shelf.gardens()] == ["Main"]
    assert shelf.garden_file() == folder / "scenes.json"
    assert [p.name for p in shelf.paths()] == ["Main"]
    assert shelf.path_file().exists()
    again = Shelf(folder)
    assert again.index == shelf.index


def test_paths_switch_outputs_and_keep_each_ones_settings(setup):
    folder, shelf, col = setup
    col.outputs = [Output(id="out-1", type="stream", service="twitch", canvas="horizontal", enabled=True)]
    col.recording.bitrate_kbps = 9000
    record = shelf.new_path("Recording only", col)
    shelf.use_path(record.id, col)
    assert col.outputs == [] and col.recording.bitrate_kbps == 12000
    col.recording.bitrate_kbps = 40000
    shelf.use_path("main", col)  # by name, any case
    assert [o.service for o in col.outputs] == ["twitch"] and col.recording.bitrate_kbps == 9000
    shelf.use_path("Recording only", col)
    assert col.recording.bitrate_kbps == 40000
    assert shelf.taken_output_ids() == {"out-1"}


def test_duplicate_path_copies_the_live_settings(setup):
    folder, shelf, col = setup
    col.outputs = [Output(id="out-3", type="stream", service="youtube", canvas="vertical")]
    copy = shelf.new_path("Multistream", col, copy=shelf.path())
    assert [o.id for o in shelf.load_path(copy).outputs] == ["out-3"]


def test_names_are_unique_and_lists_are_a_to_z(setup):
    folder, shelf, col = setup
    shelf.new_garden("zoo", col)
    shelf.new_garden("Art", col)
    assert [g.name for g in shelf.gardens()] == ["Art", "Main", "zoo"]
    with pytest.raises(ShelfError):
        shelf.new_garden("art", col)
    with pytest.raises(ShelfError):
        shelf.rename("garden", "zoo", "  MAIN ")
    assert shelf.rename("garden", "zoo", "Zoo  day").name == "Zoo day"


def test_switching_garden_carries_the_path_and_canvases(setup):
    folder, shelf, col = setup
    col.outputs = [Output(id="out-1", type="stream", service="kick", canvas="vertical")]
    event = shelf.new_garden("Event", col)
    garden = shelf.load_garden(event)
    assert [s.name for s in garden.scenes if s.kind == "show"] == ["Bloom", "Bloom"]
    assert {s.id for s in garden.sources} >= set(col.global_device_ids())
    col.canvases[0].fps = 30
    entry, shown = shelf.use_garden("Event", col)
    assert entry.id == event.id and shelf.garden().id == event.id
    assert shown.canvases[0].fps == 30
    assert [o.service for o in shown.outputs] == ["kick"]


def test_copy_of_the_garden_on_screen_takes_it_as_it_is(setup):
    folder, shelf, col = setup
    col.scenes[0].name = "Renamed just now"
    copy = shelf.new_garden("Copy", col, copy=shelf.garden())
    assert shelf.load_garden(copy).scenes[0].name == "Renamed just now"


def test_remove_keeps_the_file_and_guards_the_one_in_use(setup):
    folder, shelf, col = setup
    with pytest.raises(ShelfError):
        shelf.remove("garden", "Main")  # the only one
    other = shelf.new_garden("Other", col)
    with pytest.raises(ShelfError):
        shelf.remove("garden", "Main")  # in use
    shelf.remove("garden", other.id)
    assert [g.name for g in shelf.gardens()] == ["Main"]
    assert list((folder / "removed").glob("*.json"))


def test_export_and_import(setup, tmp_path_factory):
    folder, shelf, col = setup
    out = tmp_path_factory.mktemp("out")
    shelf.export("garden", "Main", out / "Gaming.json", col)
    shelf.export("path", "Main", out / "Twitch only.json", col)
    garden = shelf.import_file("garden", out / "Gaming.json")
    path = shelf.import_file("path", out / "Twitch only.json")
    assert garden.name == "Gaming" and path.name == "Twitch only"
    assert shelf.import_file("garden", out / "Gaming.json").name == "Gaming 2"
    (out / "junk.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ShelfError):
        shelf.import_file("garden", out / "junk.json")


def test_garden_with_other_canvases_is_refused(setup):
    folder, shelf, col = setup
    odd = blank_garden(col)
    odd.canvases = odd.canvases[:1]
    odd.scenes = [s for s in odd.scenes if s.canvas == odd.canvases[0].id]
    odd.active_scenes = {odd.canvases[0].id: odd.scenes[0].id}
    odd.outputs = []
    odd.save(folder / "odd.json")
    entry = shelf.import_file("garden", folder / "odd.json")
    with pytest.raises(ShelfError):
        shelf.use_garden(entry.id, col)


def test_path_on_a_missing_canvas_falls_back():
    col = _starter()
    settings = PathSettings(outputs=[Output(id="o", type="stream", canvas="square")])
    settings.recording.canvases = ["square"]
    settings.apply_to(col)
    assert col.outputs[0].canvas == "horizontal" and col.recording.canvases == ["horizontal"]


def test_missing_files_and_locate(tmp_path):
    col = _starter()
    here = tmp_path / "here.png"
    here.write_bytes(b"x")
    gone = str(tmp_path / "gone.png")
    col.sources[0].kind = "image"
    col.sources[0].settings = {"path": gone}
    col.sources[1].kind = "image"
    col.sources[1].settings = {"path": "art:sloth-happy.png"}
    col.transition.stinger.path = gone
    col.transition.wipe_image = str(here)
    assert missing_files(col) == [gone]
    assert replace_file(col, gone, str(here)) == 2
    assert missing_files(col) == []


# The window: menus, switching, and the APIs -------------------------------------------------------


@pytest.fixture
def window(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("GardensTest")
    QSettings().clear()
    w = MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")
    yield w
    w.close()


def test_window_switches_gardens_and_paths(window, tmp_path):
    from sprigcast.app.control_server import WindowHost
    from sprigcast.control.api import ControlApi
    from sprigcast.control.native import NativeApi

    w = window
    starter_blooms = {s.name for s in w.collection.scenes if s.kind == "show"}
    assert "Main" in w.windowTitle()
    menus = [a.text() for a in w.menuBar().actions()]
    assert "Gardens" in menus and "Paths" in menus

    host = WindowHost(w)
    obs, native = ControlApi(host), NativeApi(host)
    obs.changes(), native.changes()
    assert native.run("garden.new", {"name": "Event"}) == {"garden": "Event"}
    assert w.scenes_path == tmp_path / "gardens" / "garden-event.json"
    assert {s.name for s in w.collection.scenes if s.kind == "show"} == {"Bloom"}
    assert "Event" in w.windowTitle()
    events = dict((e, d) for e, _, d in obs.changes())
    assert events["CurrentSceneCollectionChanged"] == {"sceneCollectionName": "Event"}
    assert ("garden.changed", {"garden": "Event"}) in native.changes()

    obs.handle("SetCurrentSceneCollection", {"sceneCollectionName": "Main"})
    assert w.scenes_path == tmp_path / "scenes.json"
    assert {s.name for s in w.collection.scenes if s.kind == "show"} == starter_blooms
    assert obs.handle("GetSceneCollectionList", {}) == {"currentSceneCollectionName": "Main",
                                                       "sceneCollections": ["Event", "Main"]}

    w.collection.recording.bitrate_kbps = 7777
    w._changed()
    obs.handle("CreateProfile", {"profileName": "Recording only"})
    assert w.collection.recording.bitrate_kbps == 12000
    assert obs.handle("GetProfileList", {})["currentProfileName"] == "Recording only"
    native.run("path.switch", {"path": "Main"})
    assert w.collection.recording.bitrate_kbps == 7777
    obs.handle("RemoveProfile", {"profileName": "Recording only"})
    assert native.run("paths.list", {}) == {"path": "Main", "paths": ["Main"]}
