"""Gardens (scene collections) and paths (profiles): core/gardens.py."""

from importlib import resources

import pytest

from sprigcast.core.gardens import (
    PathSettings,
    Shelf,
    ShelfError,
    blank_garden,
    file_name,
    missing_files,
    replace_file,
)
from sprigcast.core.scene import Canvas, Output, SceneCollection, SceneItem


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


def test_garden_from_other_canvases_is_fitted(setup):
    folder, shelf, col = setup
    odd = blank_garden(col)
    odd.canvases = [Canvas(id="main", width=2560, height=1440)]
    for scene in odd.scenes:
        scene.canvas, scene.linked_to = "main", None
        scene.items = [SceneItem(source=odd.sources[0].id, x=100, y=200, scale=2.0)]
    odd.scenes = odd.scenes[:1]
    odd.active_scenes = {"main": odd.scenes[0].id}
    odd.outputs = []
    (folder / "odd.json").write_text(odd.model_dump_json(), encoding="utf-8")
    entry = shelf.import_file("garden", folder / "odd.json")
    _, shown = shelf.use_garden(entry.id, col)
    assert [c.id for c in shown.canvases] == ["horizontal", "vertical"]
    wide = shown.scene(shown.active_scenes["horizontal"])
    sprig = shown.scene(shown.source(wide.items[0].source).settings["scene"])
    assert sprig.items[0].x == 75 and sprig.items[0].scale == 1.5  # 2560 wide -> 1920 wide
    assert wide.items[0].scale == 1.0
    assert shown.active_scenes["vertical"]  # an empty bloom for the canvas it didn't have


def test_lost_index_finds_the_files_again(setup):
    folder, shelf, col = setup
    shelf.new_garden("Gaming", col)
    shelf.new_path("Twitch only", col)
    (folder / "gardens.json").unlink()
    again = Shelf(folder)
    assert {g.name for g in again.gardens()} == {"Main", "Gaming"}
    assert {p.name for p in again.paths()} >= {"Twitch only"}


def test_export_names_and_folder_are_safe(setup, tmp_path_factory):
    folder, shelf, col = setup
    assert file_name('My: "show"/<1>') == "My show1"
    assert file_name("con") == "SprigCast con"
    with pytest.raises(ShelfError):
        shelf.export("garden", "Main", folder / "gardens" / "x.json", col)
    col.outputs = [Output(id="out-1", type="stream", service="twitch", canvas="horizontal", key_ref="sprigcast/out-1")]
    out = tmp_path_factory.mktemp("exports") / "Twitch.json"
    shelf.export("path", "Main", out, col)
    text = out.read_text(encoding="utf-8")
    assert "sprigcast/out-1" in text and "live_" not in text  # the key's name only: keys stay in Windows


def test_import_reads_a_file_with_a_bom(setup, tmp_path_factory):
    folder, shelf, col = setup
    out = tmp_path_factory.mktemp("bom") / "Saved.json"
    out.write_bytes(b"\xef\xbb\xbf" + col.model_dump_json().encode())
    assert shelf.import_file("garden", out).name == "Saved"


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


def test_window_imports_an_obs_scene_collection(window, tmp_path):
    import json

    from test_convert import obs_file

    w = window
    file = tmp_path / "Streaming.json"
    file.write_text(json.dumps(obs_file()), encoding="utf-8")
    assert w.gardens.import_garden(file, show_notes=False) is None
    assert w.gardens.shelf.garden().name == "Streaming"
    assert {s.name for s in w.collection.scenes if s.kind == "show"} == {"Just Chatting", "BRB"}
    assert w.gardens.last_notes
    (tmp_path / "junk.json").write_text("[]", encoding="utf-8")
    assert "isn't a scene collection" in w.gardens.import_garden(tmp_path / "junk.json")


def test_find_missing_files_in_a_folder(tmp_path):
    from sprigcast.core.gardens import find_in_folder

    (tmp_path / "Overlays" / "old").mkdir(parents=True)
    (tmp_path / "Overlays" / "frame.png").write_bytes(b"x")
    (tmp_path / "Other").mkdir()
    (tmp_path / "Other" / "frame.png").write_bytes(b"x")
    found = find_in_folder(["C:\\Stream\\Overlays\\frame.png", "D:/gone/nowhere.webm"], tmp_path)
    assert found == {"C:\\Stream\\Overlays\\frame.png": str(tmp_path / "Overlays" / "frame.png")}


def test_window_chip_and_switch_hotkeys(window, tmp_path):
    from sprigcast.app import hotkeys

    w = window
    g = w.gardens
    assert g.chip.text() == "Main  ·  Main"
    event = g.shelf.new_garden("Event", w.collection)
    twitch = g.shelf.new_path("Twitch only", w.collection)
    actions = dict(hotkeys.actions(w.collection))
    assert actions[f"garden:{event.id}"] == "Garden: Event" and actions[f"path:{twitch.id}"] == "Path: Twitch only"
    w.collection.hotkeys[f"garden:{event.id}"] = "Ctrl+F9"
    w._on_hotkey(f"path:{twitch.id}")
    assert g.shelf.path().name == "Twitch only" and g.chip.text() == "Main  ·  Twitch only"
    w._on_hotkey(f"garden:{event.id}")
    assert g.shelf.garden().name == "Event"
    assert w.collection.hotkeys[f"garden:{event.id}"] == "Ctrl+F9"  # came along to the other garden


def test_window_imports_a_garden_pack(window, tmp_path, monkeypatch):
    from sprigcast.core import pack

    w = window
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"png")
    col = w.collection.model_copy(deep=True)
    col.sources[0].kind, col.sources[0].settings = "image", {"path": str(logo)}
    target = tmp_path / f"Party{pack.PACK_SUFFIX}"
    pack.export_pack(col, target)
    monkeypatch.setattr(w.gardens, "_media", lambda: tmp_path / "Media")
    assert w.gardens.import_garden(target, show_notes=False) is None
    assert w.gardens.shelf.garden().name == "Party"
    assert w.collection.sources[0].settings["path"] == str((tmp_path / "Media" / "Party" / "1" / "logo.png").resolve())
    from PySide6.QtWidgets import QMenu

    garden_menu = QMenu()
    w.gardens.fill(garden_menu, "garden")
    texts = [a.text() for a in garden_menu.actions()]
    assert texts[0] == "New…" and not any(t.startswith("Party") for t in texts)  # switching is the header chip
    assert ("Garden", "Party", True) in [c[:3] for c in w.gardens.switch_choices()]  # and the palette
    export = next(a.menu() for a in garden_menu.actions() if a.text() == "Export")
    assert [a.text() for a in export.actions()] == ["Just the garden…", "With its files…"]
    assert not any(a.isCheckable() for a in garden_menu.actions())


def test_set_aside_files_are_not_gardens(setup):
    folder, shelf, col = setup
    entry = shelf.new_garden("Gaming", col)
    file = shelf.garden_file(entry)
    file.with_suffix(".broken.json").write_text("{", encoding="utf-8")
    file.with_suffix(".before-sprigs.json").write_text("{}", encoding="utf-8")
    shelf.index_path.unlink()  # a lost list is rebuilt from the files: the set-aside copies stay out of it
    again = Shelf(folder)
    assert sorted(g.name for g in again.gardens()) == ["Gaming", "Main"]


def test_window_import_while_live_asks_first(window, tmp_path, monkeypatch):
    import json

    from PySide6.QtWidgets import QMessageBox
    from test_convert import obs_file

    w = window
    file = tmp_path / "Streaming.json"
    file.write_text(json.dumps(obs_file()), encoding="utf-8")
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a) or QMessageBox.StandardButton.No)
    w.streaming = object()
    assert w.gardens.import_garden(file, show_notes=False) is None
    assert asked and w.gardens.shelf.garden().name == "Main"  # added, but the show on air stays
    assert "Streaming" in [g.name for g in w.gardens.shelf.gardens()]
    w.streaming = None


def test_window_waits_for_packing_and_open_windows(window, monkeypatch):
    from PySide6.QtWidgets import QApplication

    w = window
    w.gardens.do("garden", "new", "Other")
    w.gardens.working = True
    assert w.gardens.do("garden", "switch", "Other") == "Wait for the pack to finish."
    assert w.gardens.switch("garden", "Other") == "Wait for the pack to finish."
    w.gardens.working = False
    monkeypatch.setattr(QApplication, "activeModalWidget", staticmethod(lambda: w))  # e.g. Settings is open
    assert w.gardens.do("garden", "switch", "Other") == "Close the open window first."
    w.gardens.hotkey(f"garden:{w.gardens.shelf.find('garden', 'Other').id}")
    assert w.gardens.shelf.garden().name == "Main"


def test_background_work_hands_back_its_result_and_errors(window):
    from sprigcast.app.gardens_menu import _Progress

    assert _Progress(window, "x").run(lambda p: (p(1, 2), 42)[1]) == 42
    with pytest.raises(ValueError):
        _Progress(window, "x").run(lambda p: (_ for _ in ()).throw(ValueError("no")))


def test_missing_files_window_looks_on_a_worker_and_lists_them(tmp_path):
    """Looked for on the window's thread, opening it held the window 110 ms on the PC while live."""
    import time

    from PySide6.QtWidgets import QApplication, QLabel, QWidget

    from sprigcast.app.gardens_menu import MissingFiles

    app = QApplication.instance() or QApplication([])
    col = _starter()
    gone = str(tmp_path / "gone.png")
    col.sources[0].kind = "image"
    col.sources[0].settings = {"path": gone}
    host = QWidget()
    host.collection = col
    box = MissingFiles(host)
    box.looked.result(timeout=10)
    end = time.monotonic() + 5
    names = []
    while "gone.png" not in names and time.monotonic() < end:
        app.processEvents()
        names = [label.text() for label in box.rows.findChildren(QLabel)]
    assert "gone.png" in names and box.look.isVisibleTo(box)
