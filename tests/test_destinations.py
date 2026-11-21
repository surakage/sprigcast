import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QSettings
from PySide6.QtWidgets import QApplication

from sprigcast.core.scene import Canvas, Output, Scene, SceneCollection


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        scenes=[
            Scene(id="h1", name="Main", canvas="horizontal"),
            Scene(id="h2", name="BRB", canvas="horizontal"),
            Scene(id="v1", name="Main (vertical)", canvas="vertical", linked_to="h1"),
        ],
        active_scenes={"horizontal": "h1", "vertical": "v1"},
        outputs=[
            Output(id="tw", type="stream", service="twitch", canvas="horizontal", key_ref="k/tw", enabled=True),
            Output(id="tt", type="stream", service="tiktok", canvas="vertical", key_ref="k/tt"),
        ],
    )


def test_readiness_and_summary():
    from sprigcast.app.destinations import problem, what_goes

    col = _collection()
    tw, tt = col.outputs
    assert problem(tw, col, lambda ref: True) is None
    assert problem(tw, col, lambda ref: False) == "Needs a stream key"
    tw.twitch_account = True
    assert problem(tw, col, lambda ref: False) is None  # the key comes from the account
    assert problem(tt, col, lambda ref: True) == "Needs a server URL"  # TikTok has no fixed server
    tt.server = "rtmp://example/live"
    col.canvases[1].enabled = False
    assert "vertical canvas is off" in problem(tt, col, lambda ref: True)
    assert what_goes(tw, col) == "Horizontal · 1920×1080 · 60 fps · 6000 kbps"
    tw.enhanced, tw.enhanced_vertical = True, True
    assert what_goes(tw, col) == "Enhanced · horizontal + vertical"


def test_live_panel_scene_buttons(app):
    from sprigcast.app.live_panel import LivePanel

    col = _collection()
    picked = []
    panel = LivePanel(picked.append)
    panel.show_scenes(col)
    assert set(panel.buttons) == {"h1", "h2", "v1"}  # a list per canvas: v1 (linked to h1) switches both
    assert panel.buttons["v1"].key == ""  # a linked twin has no number of its own
    panel.set_view("horizontal")
    assert set(panel.buttons) == {"h1", "h2"}  # only the canvas on the stage
    panel.set_view("both")
    assert panel.buttons["h1"].isChecked() and not panel.buttons["h2"].isChecked()
    assert "Main (vertical)" in panel.buttons["h1"].toolTip()
    panel.buttons["h2"].click()
    assert picked == ["h2"]
    col.set_active("h2")
    panel.show_scenes(col)
    assert panel.buttons["h2"].isChecked() and not panel.buttons["h1"].isChecked()


def test_main_window_modes(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("ModesTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = _collection()
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w.show()
        # Build has its own screen too: the library, the canvas with its twin, Inside and the inspector.
        assert w.mode == "build" and w.build_stage.isVisible()
        empty = w.source_list.count() == 0  # an empty bloom shows the napping sloth instead of the list
        assert w.source_list.isVisible() != empty and w.inside_empty.isVisible() == empty
        assert not w.live_stage.isVisible()
        assert w.library.isVisible()
        blooms = w.scene_tree.topLevelItem(0)
        rows = {blooms.child(i).data(0, 256): blooms.child(i) for i in range(blooms.childCount())}
        assert set(rows) == {"h1", "h2"} and rows["h1"].isSelected()
        assert w.build_bar.where.text() == "Main"
        assert w.previews["vertical"].twin and not w.previews["vertical"].editable
        assert w.previews["horizontal"].editable and not w.previews["horizontal"].twin
        w._set_mode("live")
        # Live has its own screen: the stage, bloom tiles and side column, and only the Chat panel by default.
        assert w.live_stage.isVisible() and w.live_panel.isVisible() and not w.build_stage.isVisible()
        assert not w.scene_tree.isVisible()
        assert w.panels.cards["live"].isVisible() and list(w.panels.cards["live"].buttons) == ["chat"]
        assert w.chat_panel.pages.currentWidget() is w.chat_panel.empty  # nothing connected: nothing to read
        assert w.chat_panel.active
        w.panels.set_shown("chat", False, "live")  # off every screen: chat stops reading
        assert not w.chat_panel.active
        w.panels.set_shown("chat", True, "live")
        assert w.chat_panel.active
        assert w.previews["horizontal"].isVisible() and w.previews["horizontal"].live_look
        assert not any(p.editable for p in w.previews.values())
        w.live_panel.buttons["h2"].click()
        assert col.active_scenes["horizontal"] == "h2"
        w._set_mode("build")
        assert w.build_stage.isVisible() and w.previews["horizontal"].editable
        assert w.previews["horizontal"].isVisible() and not w.previews["horizontal"].live_look
        assert QSettings().value("window/mode") == "build"
        # Start-up opens on Live with Destinations, even though it was left on Build (soak, 2026-11-19).
        w.start_in_live()
        assert w.mode == "live" and w.live_stage.isVisible() and not w.build_stage.isVisible()
    finally:
        w.close()
        QSettings().clear()


def test_build_canvas_tabs_and_linking(app, tmp_path, monkeypatch):
    import keyring
    from sprigcast.app import card as popups

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("CanvasTabsTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = _collection()
    w = MainWindow(col, tmp_path / "scenes.json")

    def listed():
        top = w.scene_tree.topLevelItem(0)
        return [top.child(i).data(0, Qt.ItemDataRole.UserRole) for i in range(top.childCount())]

    from PySide6.QtCore import Qt
    try:
        w.show()
        assert [a.text() for a in w.mode_actions.values()] == ["Live", "Build"]  # Live comes first
        # Build starts on the horizontal tab: only its preview and its blooms.
        assert w.build_canvas == "horizontal"
        assert w.previews["horizontal"].editable and w.previews["vertical"].twin  # the other shape: a small twin
        assert "v1" not in {w.scene_tree.topLevelItem(0).child(i).data(0, 256) for i in range(2)}
        assert not w.link_button.isVisible()
        w.canvas_tabs.setCurrentIndex(1)
        assert w.build_canvas == "vertical" and w.selected_scene_id == "v1"
        assert w.previews["vertical"].editable and w.previews["horizontal"].twin
        assert w.link_button.isVisible()
        w._swap_shape("horizontal")  # clicking the twin swaps back
        assert w.build_canvas == "horizontal" and w.previews["vertical"].twin
        w.canvas_tabs.setCurrentIndex(1)
        assert w.build_canvas == "vertical" and w.selected_scene_id == "v1"
        assert "⇄ Main" in w.scene_tree.topLevelItem(0).child(0).text(0)
        # Re-link the vertical bloom to BRB from the vertical tab; switching BRB on now brings it along.
        w._link_scene("v1", "h2")
        assert col.scene("v1").linked_to == "h2"
        col.set_active("h2")
        assert col.active_scenes["vertical"] == "v1"
        w._link_scene("v1", None)
        assert col.scene("v1").linked_to is None
        # New blooms land on the tab you're on, and can be linked as they're made.
        answers = iter([("Gameplay", True)])
        monkeypatch.setattr(popups, "ask_text", lambda *a, **k: next(answers))
        monkeypatch.setattr(popups, "ask_choice", lambda _w, _t, _l, items, *a, **k: items.index("BRB"))
        w._add_scene()
        new = col.scene(w.selected_scene_id)
        assert new.canvas == "vertical" and new.linked_to == "h2"
        # Live shows both canvases again.
        w._set_mode("live")
        assert w.previews["horizontal"].isVisible() and w.previews["vertical"].isVisible()
        assert not w.canvas_tabs_row.isVisible()
        assert QSettings().value("build/canvas") == "vertical"
    finally:
        w.close()
        QSettings().clear()


def test_live_has_no_studio_mode_and_a_bloom_click_switches_at_once(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("StudioTest")
    QSettings().clear()
    QSettings().setValue("live/studio", True)  # left over from an older SprigCast: ignored
    from sprigcast.app.main_window import MainWindow

    col = _collection()
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w.show()
        w._set_mode("live")
        assert not hasattr(w.live_panel, "studio") and not hasattr(w.live_panel, "go")
        assert "Studio" not in " ".join(a.text() for m in w.menuBar().actions() if m.menu()
                                        for a in m.menu().actions())
        w.live_panel.buttons["h2"].click()
        assert col.active_scenes["horizontal"] == "h2" and w.engine.staged == {}
    finally:
        w.close()
        QSettings().clear()


def test_engine_renders_the_staged_scene_to_its_own_frame():
    from sprigcast.render.engine import Engine

    class Fake:
        def __init__(self):
            self.rendered = []

        def render(self, canvas, scene, sizes, previous=None, progress=1.0):
            self.rendered.append((canvas.id, scene.id))

        def read(self, key):
            return b"x"

    col = _collection()
    engine = Engine(col)
    fake = Fake()
    canvas = col.canvases[0]
    engine._render_staged(fake, canvas, {}, 1.0)
    assert fake.rendered == []  # nothing lined up
    engine.staged = {"horizontal": "h1"}
    engine._render_staged(fake, canvas, {}, 1.0)
    assert fake.rendered == []  # already on air: no Next to show
    engine.staged = {"horizontal": "h2"}
    engine._render_staged(fake, canvas, {}, 1.0)
    assert fake.rendered == [("next:horizontal", "h2")]
    assert engine.frames["next:horizontal"].take().data == b"x"


def test_command_palette_lists_scenes_menus_and_switches(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("PaletteTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = _collection()
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w.show()
        palette = w._open_palette()
        groups = {c.group for c in palette.commands}
        assert {"Scene", "Action", "Mode", "Add", "Tools", "Panel", "File"} <= groups
        titles = [c.title for c in palette.commands]
        assert "Go live" in titles and "Transitions" in titles
        palette.search.setText("brb")
        assert palette.shown()[0].group == "Scene"
        palette._run(palette.list.currentItem())
        assert col.active_scenes["horizontal"] == "h2"
        palette = w._open_palette()
        palette.search.setText("go to live")
        palette._run(palette.list.currentItem())
        assert w.mode == "live"
    finally:
        w.close()
        QSettings().clear()


def test_show_one_canvas_at_a_time(app, tmp_path, monkeypatch):
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("OneCanvasTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = _collection()
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w.show()
        assert w.canvas_choice() == "both"
        w.canvas_tabs.setCurrentIndex(1)  # editing the vertical canvas
        assert w.show_canvases("horizontal")
        assert [c.enabled for c in col.canvases] == [True, False] and w.canvas_choice() == "horizontal"
        assert w.build_canvas == "horizontal" and not w.canvas_tabs.isTabVisible(1)
        assert w.canvas_columns["horizontal"].isVisible() and not w.canvas_columns["vertical"].isVisible()
        w._set_mode("live")
        assert not w.canvas_columns["vertical"].isVisible()
        assert "v1" not in w.live_panel.buttons and "h1" in w.live_panel.buttons
        assert w.show_canvases("vertical")
        assert w.canvas_columns["vertical"].isVisible() and not w.canvas_columns["horizontal"].isVisible()
        assert "v1" in w.live_panel.buttons and "h1" not in w.live_panel.buttons
        w._set_mode("build")
        assert w.build_canvas == "vertical"
        assert w.show_canvases("both")
        assert all(c.enabled for c in col.canvases) and w.canvas_tabs.isTabVisible(0)
    finally:
        w.close()
        QSettings().clear()


def test_build_edits_a_bloom_without_putting_it_on_air(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("BuildEditTest")
    QSettings().clear()
    from sprigcast.app.build_stage import ROLE_CHILDREN, ROLE_NAME
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import SceneItem, SourceConfig

    col = _collection()
    col.sources += [SourceConfig(id="a", kind="color", name="Moss", settings={"color": "#00ff00"}),
                    SourceConfig(id="b", kind="color", name="Facecam", settings={"color": "#ff0000"})]
    col.scene("h2").items += [SceneItem(source="a"), SceneItem(source="b")]
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w.show()
        w._pick_bloom("h2")  # a pill: open BRB for editing
        assert col.active_scenes["horizontal"] == "h1"  # Main stays on air
        assert w.engine.staged == {"horizontal": "h2"} and w.previews["horizontal"].scene.id == "h2"
        assert "not on air" in w.previews["horizontal"].subtitle  # Put on air: bloom right-click
        # Inside lists top-most first; dragging Moss above Facecam puts it in front.
        assert [w.source_list.item(i).data(ROLE_NAME) for i in range(2)] == ["Facecam", "Moss"]
        assert w.source_list.item(0).data(ROLE_CHILDREN) == []
        w._on_inside_moved(1, 0)
        assert [i.source for i in col.scene("h2").items] == ["b", "a"]
        assert w.source_list.currentRow() == 0 and w._item().source == "a"
        # Build never changes what's on air (user, 2026-11-19): no Put on air button, double-click or menu item.
        assert col.active_scenes["horizontal"] != "h2" and not hasattr(w, "on_air_button")
        w.scene_tree.itemDoubleClicked.emit(w.scene_tree.selectedItems()[0] if w.scene_tree.selectedItems()
                                            else w.scene_tree.topLevelItem(0), 0)
        assert col.active_scenes["horizontal"] != "h2"
        w._make_active()  # what Live's tile does
        assert col.active_scenes["horizontal"] == "h2" and w.engine.staged == {}
        w._set_mode("live")
        assert w.engine.staged == {}  # Live's Next is Studio mode's alone
    finally:
        w.close()
        QSettings().clear()


def test_inspector_sizes_and_zoom(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("InspectorTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import SceneItem, SourceConfig

    col = _collection()
    col.sources.append(SourceConfig(id="cam", kind="color", name="Facecam", settings={"color": "#ff0000"}))
    col.scene("h1").items.append(SceneItem(source="cam"))
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w.show()
        assert "1920×1080" in w.previews["horizontal"].title  # the canvas size from Settings
        w.source_sizes["cam"] = (640, 360)
        w._refresh_sources(keep_row=0)
        assert w.spin["w"].value() == 640 and w.spin["h"].value() == 360
        w.spin["w"].setValue(1280)  # keep shape: the height follows
        item = col.scene("h1").items[0]
        assert item.scale == 2.0 and item.scale_y is None and w.spin["h"].value() == 720
        w.keep_shape.setChecked(False)
        assert not w.previews["horizontal"].keep_aspect
        w.spin["h"].setValue(360)  # free shape: only the height changes
        assert item.scale == 2.0 and item.sy == 1.0
        w.item_actions["reset"].trigger()
        assert item.scale == 1.0 and item.scale_y is None
        preview = w.previews["horizontal"]
        preview.zoom_at(2.0)
        assert preview.zoom == 2.0 and "200%" not in preview.title
        preview.fit_view()
        assert preview.zoom == 1.0
    finally:
        w.close()
        QSettings().clear()


def test_inspector_numbers_sit_centred(app):
    from PySide6.QtWidgets import QDoubleSpinBox, QWidget, QVBoxLayout

    from sprigcast.app import theme

    holder = QWidget()
    holder.setStyleSheet(theme.SOFT_FIELD_STYLE)
    box = QDoubleSpinBox()
    QVBoxLayout(holder).addWidget(box)
    holder.resize(160, 60)
    holder.show()
    app.processEvents()
    field = box.lineEdit().geometry()
    assert field.x() == 0 and field.width() == box.width()  # no arrow space pushing the number left
    holder.close()
def test_where_it_goes_switches_and_edit_adds_nothing(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("PlacesTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = _collection()
    w = MainWindow(col, tmp_path / "scenes.json")
    opened = []
    monkeypatch.setattr(w, "_open_settings", lambda tab="General", output_id=None: opened.append(output_id))
    try:
        w._set_mode("live")
        side = w.live_side
        assert list(side.place_rows) == ["tw", "tt"]
        switch = side.place_rows["tt"][3]
        assert side.place_rows["tw"][3].isChecked() and not switch.isChecked()
        switch.click()  # turn TikTok on from Live
        assert col.outputs[1].enabled and not side.place_rows["tt"][2].text().endswith(">Off</span>")
        side.place_rows["tw"][3].click()
        note = side.place_rows["tw"][2].text()  # the shape, then how it's doing
        assert not col.outputs[0].enabled and note.startswith("Horizontal") and note.endswith(">Off</span>")
        assert side.place_rows["tw"][0].text() == "Tw"  # the platform's little tile
        # Edit opens the list on the first one; only + Add makes a new one.
        w._edit_destination(None)
        assert opened == ["tw"] and len(col.outputs) == 2  # opening settings adds nothing
        # + Add picks a service; one that needs a key opens Settings on the new place.
        from sprigcast.output import twitch_auth

        monkeypatch.setattr(twitch_auth, "connected_account", lambda: None)
        side.add_menu.actions()[1].trigger()  # YouTube
        assert len(col.outputs) == 2  # not from inside the menu's click: once the menu has closed
        QApplication.processEvents()
        assert len(col.outputs) == 3 and col.outputs[2].service == "youtube" and opened[-1] == col.outputs[2].id
        monkeypatch.setattr(twitch_auth, "connected_account", lambda: "someone")
        side.add_menu.actions()[0].trigger()  # Twitch with a connected account: ready, nothing to open
        QApplication.processEvents()
        assert col.outputs[3].twitch_account and len(opened) == 2
        # A place + Add made: Settings closed without saving takes it away again; saved without a key, it stays
        # but switched off; saved with its server and key, it stays on.
        answers = iter([False, True, True])
        monkeypatch.setattr(w, "_open_settings", lambda tab="General", output_id=None: next(answers))
        monkeypatch.setattr(w, "_has_key", lambda ref: False)
        w._edit_destination("new:facebook")
        assert len(col.outputs) == 4 and "facebook" not in [o.service for o in col.outputs]
        w._edit_destination("new:facebook")
        assert col.outputs[4].service == "facebook" and not col.outputs[4].enabled
        col.outputs.pop()
        monkeypatch.setattr(w, "_has_key", lambda ref: True)
        made = []
        real_new, real_has_key = w._new_destination, type(w)._has_key.__get__(w)
        monkeypatch.setattr(w, "_new_destination", lambda key: (
            made.append(real_new(key)), setattr(col.outputs[-1], "server", "rtmps://example.invalid/live"))[0] or made[-1])
        w._edit_destination("new:facebook")
        assert col.outputs[4].service == "facebook" and col.outputs[4].enabled
        col.outputs.pop()
        monkeypatch.setattr(w, "_new_destination", real_new)
        monkeypatch.setattr(w, "_has_key", real_has_key)
        monkeypatch.setattr(w, "_open_settings", lambda tab="General", output_id=None: opened.append(output_id))
        w._live_destinations()
        # The menu: the five everyday places, Custom RTMP, then Show all… with every other place behind it.
        assert [a.text() for a in side.add_menu.actions() if not a.isSeparator()] == [
            "Twitch", "YouTube", "Kick", "TikTok", "Facebook", "Custom RTMP", "Show all…"]
        from sprigcast.app import place_picker

        monkeypatch.setattr(place_picker, "pick_place", lambda parent=None: "soop")
        side.show_all_places.trigger()
        QApplication.processEvents()
        assert col.outputs[4].service == "soop" and opened[-1] == col.outputs[4].id
        col.outputs.pop()
        opened.pop()
        w._live_destinations()
        # Edit works right here: shape, test mode and remove (twice) without opening Settings.
        side.manage.click()
        assert side.editing and side.manage.text() == "Done" and side.all_settings.isVisibleTo(side.where_card)
        tools = side.place_tools["tw"]
        tools["shape"].click()
        assert col.outputs[0].canvas == "vertical" and side.place_tools["tw"]["shape"].property("shape") == "vertical"
        side.place_tools["tw"]["test"].click()
        assert col.outputs[0].bandwidth_test
        assert "test" not in side.place_tools["tt"]  # TikTok has no test mode
        removed_keys = []
        monkeypatch.setattr("sprigcast.app.main_window.set_key", lambda ref, key: removed_keys.append(ref))
        remove = side.place_tools[col.outputs[2].id]["remove"]
        gone = col.outputs[2].id
        remove.click()
        assert remove.text() == "Remove?" and len(col.outputs) == 4
        remove.click()
        assert gone not in [o.id for o in col.outputs] and removed_keys == [f"sprigcast/{gone}"]
        assert len(opened) == 2
        col.outputs[:] = col.outputs[:2]
        w._live_destinations()
        side.manage.click()
        assert not side.editing
        side.set_live(True)
        switch = side.place_rows["tt"][3]
        # Live: + Add and Edit wait for the stream to end, but each place's switch works (it ends or starts
        # just that place).
        assert switch.isEnabled() and not side.add_place.isEnabled() and not side.manage.isEnabled()
        assert "this place only" in switch.toolTip()
        side.set_live(False)
        # Two Twitch destinations say which shape each one sends.
        col.outputs.append(Output(id="tw2", type="stream", service="twitch", canvas="vertical", key_ref="k/tw2"))
        w._live_destinations()
        assert side.place_rows["tw2"][1].text() == "Twitch · Vertical"
    finally:
        w.close()
        QSettings().clear()


def test_stream_page_on_switch(app):
    from sprigcast.app.stream_dialog import StreamPage, output_label

    col = _collection()
    page = StreamPage(col)
    page.select_output("tt")
    assert not page.enabled.isChecked() and output_label(page.outputs[1]).endswith("(off)")
    page.enabled.setChecked(True)
    assert page.outputs[1].enabled and page.apply_to(col) and col.outputs[1].enabled


def test_projector_shows_a_canvas_full_size(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("ProjectorTest")
    QSettings().clear()
    from PySide6.QtGui import QGuiApplication

    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_collection(), tmp_path / "scenes.json")
    try:
        w.show()
        w._set_mode("live")
        menu = w._preview_menu("vertical", -1)
        full = next(a for a in menu.actions() if a.text() == "Projector").menu()
        labels = [a.text() for a in full.actions() if a.text()]
        assert labels[0].startswith("Full screen on Screen 1") and "In a window" in labels
        texts = [a.text() for a in menu.actions() if a.text()]  # Live: this canvas only, no editing items
        assert texts == ["Projector", "Safe zones", "Canvas settings…  1080×1920, 60 fps"]
        zones = next(a for a in menu.actions() if a.text() == "Safe zones").menu()
        next(a for a in zones.actions() if a.text() == "TikTok").trigger()
        assert w.previews["vertical"].live_safe_zones == "TikTok" and w.previews["vertical"].safe_zones == "All platforms"
        wide = [a.text() for a in w._preview_menu("horizontal", -1).actions() if a.text()]
        assert "Safe zones" not in wide  # only tall canvases get the apps' overlays
        shots = []
        w._screenshot = lambda *a, **k: shots.append(a)  # the header's round button takes it instead
        w._set_mode("build")  # Build's top bar has the round button; Live has Screenshot under its canvases
        w.screenshot_button.click()
        assert shots == [()]
        w._set_mode("live")
        assert w.screenshot_button.size() == w.palette_button.size() == w.settings_button.size()
        projector = w.open_projector("vertical", None)
        assert w.projectors == [projector] and "vertical" in w._watched_previews()
        w.showMinimized()
        assert "vertical" in w._watched_previews()  # keeps going while the main window is down
        w.showNormal()
        projector.resize(1080, 1920)
        assert w._preview_pixel_sizes()["vertical"][1] >= 1920
        projector.set_frame(b"\x00" * (4 * 8 * 8), 8, 8)
        screen = QGuiApplication.screens()[0]
        on_screen = w.open_projector("horizontal", screen)
        assert on_screen.isFullScreen()
        w.open_projector("vertical", screen)  # one per screen: replaces the horizontal one
        assert on_screen not in w.projectors and len(w.projectors) == 2
        for p in list(w.projectors):
            p.close()
        assert w.projectors == []
    finally:
        w.close()
        QSettings().clear()


def test_projector_shows_a_bloom_or_a_source_from_its_right_click_menu(app, tmp_path, monkeypatch):
    import keyring
    import types

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("ProjectorTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import SceneItem, SourceConfig

    col = _collection()
    col.sources.append(SourceConfig(id="cam", kind="color", name="Facecam"))
    col.scenes[1].items.append(SceneItem(source="cam"))
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w.show()
        w._set_mode("live")
        menu = w.live_panel.scene_menu("h2")  # a bloom tile that isn't on air
        texts = [a.text() for a in menu.actions() if a.text()]
        assert texts == ["Comes in with", "Save screenshot", "Projector", "Rename…", "Set a key…"]  # a click puts it on air
        into = next(a for a in menu.actions() if a.text() == "Comes in with").menu()
        next(a for a in into.actions() if a.text() == "Leaf").trigger()
        rule = w.collection.transition_rules[-1]
        assert (rule.from_scene, rule.to_scene, rule.transition.kind) == ("", "h2", "leaf")
        into = next(a for a in w.live_panel.scene_menu("h2").actions() if a.text() == "Comes in with").menu()
        assert next(a for a in into.actions() if a.text() == "Leaf").isChecked()
        next(a for a in into.actions() if a.text().startswith("Usual")).trigger()
        assert w.collection.transition_rules == []
        shots = []
        monkeypatch.setattr("sprigcast.app.screenshot.grab", lambda engine, ids: shots.append(dict(engine.views)) or {})
        w._bloom_screenshot("h2")  # drawn through a view for that moment only
        assert shots == [{"screenshot": ("horizontal", "bloom", "h2")}] and "screenshot" not in w.engine.views
        full = next(a for a in menu.actions() if a.text() == "Projector").menu()
        next(a for a in full.actions() if a.text() == "In a window").trigger()
        bloom = w.projectors[-1]
        assert bloom.view and w.engine.views[bloom.view] == ("horizontal", "bloom", "h2")
        assert "BRB" in bloom.windowTitle() and bloom.view not in w._watched_previews()
        bloom.tap(types.SimpleNamespace(data=b"\x00" * (4 * 4 * 4), width=4, height=4))
        w._poll_engine()  # the newest frame reaches the projector on the UI thread
        assert bloom.pending is None and bloom._image is not None
        source = w.open_projector("horizontal", None, "source", "cam")
        assert w.engine.views[source.view] == ("horizontal", "source", "cam") and "Facecam" in source.windowTitle()
        bloom.close()
        assert bloom.view not in w.engine.views and not w.engine._taps.get(f"view:{bloom.view}")
        source.close()
        assert w.engine.views == {} and w.projectors == []
    finally:
        w.close()
        QSettings().clear()


def test_projector_views_are_drawn_at_screen_rate_only():
    from sprigcast.core.scene import Canvas, Scene, SceneCollection
    from sprigcast.render.engine import Engine

    class Fake:
        def __init__(self):
            self.drawn = 0

        def render(self, *a, **k):
            self.drawn += 1

        def read(self, key):
            return b""

    engine = Engine(SceneCollection(canvases=[Canvas(id="h", width=16, height=9)],
                                    scenes=[Scene(id="a", name="A", canvas="h")], active_scenes={"h": "a"}))
    fake, got = Fake(), []
    engine.set_view("p", "h", "bloom", "a")
    taps = {"view:p": [(got.append, None)]}
    for i in range(6):  # 60 ticks a second: every other one is drawn
        engine._render_views(fake, {}, 10 + i / 60, taps)
    assert fake.drawn == 3 and len(got) == 3


def test_top_menus_keep_their_items_for_screen_readers(app, tmp_path, monkeypatch):
    """Opening a top menu must not delete and remake its items (a screen reader saw none in Gardens),
    and every item is readable through Qt's accessibility tree."""
    import keyring
    from PySide6.QtGui import QAccessible

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_collection(), tmp_path / "scenes.json")
    try:
        for top in w.menuBar().actions():
            menu = top.menu()
            menu.aboutToShow.emit()
            before = list(menu.actions())
            menu.aboutToShow.emit()  # opened again with nothing changed: the very same items
            assert menu.actions() == before, top.text()
            face = QAccessible.queryAccessibleInterface(menu)
            names = [face.child(i).text(QAccessible.Text.Name) for i in range(face.childCount())]
            wanted = [a.text().replace("&", "").partition("\t")[0] for a in before if not a.isSeparator()]
            assert wanted and all(any(n and n.replace("&", "").startswith(t) for n in names) for t in wanted), \
                (top.text(), names)
        gardens = w.menuBar().actions()[3].menu()
        remove = next(a for a in gardens.actions() if a.text() == "Remove")
        assert not remove.isEnabled()  # one garden
        w.gardens.shelf.add_garden("Second", _collection())
        gardens.aboutToShow.emit()  # the list changed: rebuilt, so Remove can be used now
        assert next(a for a in gardens.actions() if a.text() == "Remove").isEnabled()
    finally:
        w.close()


def test_menu_bar_order_and_shortcut_list(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("MenuTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_collection(), tmp_path / "scenes.json")
    try:
        titles = [a.text().replace("&", "") for a in w.menuBar().actions()]
        assert titles == ["File", "Edit", "Panels", "Gardens", "Paths", "Tools", "Help"]
        panels = w.menuBar().actions()[2].menu()
        assert any("web panels" in a.text().lower() for a in panels.actions())
        lines = w.shortcut_lines()
        assert lines and len(lines) == len(set(lines))
        assert any("Ctrl+K" in str(line) for line in lines)
        def texts(i):
            menu = w.menuBar().actions()[i].menu()
            menu.aboutToShow.emit()
            return [a.text() for a in menu.actions() if not a.isSeparator()]

        # Only what has no other home on screen (each removed item is still reachable elsewhere).
        assert texts(0) == ["Open recordings folder", "Repot as MP4…", "Export settings…", "Import settings…", "Quit"]
        assert texts(1) == ["Undo", "Redo"]
        assert texts(2)[-2:] == ["Web panels…", "Reset panels"]
        assert "Stats" not in texts(2) and "Projector" not in texts(2)
        assert texts(3) == ["New…", "Duplicate…", "Rename…", "Remove", "Import", "Export", "Missing files…",
                            "Show file"]
        assert texts(4) == ["New…", "Duplicate…", "Rename…", "Remove", "Import…", "Export…"]
        assert texts(5) == ["Get started…", "Stream doctor…", "Hotkeys…", "Plugin store…"]
        assert texts(6) == ["Keyboard shortcuts", "Report a problem…", "Export report", "Open logs folder",
                            "About SprigCast", "Support SprigCast"]
        palette = {c.title for c in w._palette_commands()}
        assert {"Optimize for this PC", "Starter kit", "Check for updates", "Stats", "Main", "Stream details"} <= palette
        shown = []
        monkeypatch.setattr("sprigcast.app.transitions_panel.PopUp.exec", lambda self: shown.append(self))
        help_menu = w.menuBar().actions()[6].menu()
        next(a for a in help_menu.actions() if a.text() == "Support SprigCast").trigger()
        assert shown and shown[0].windowTitle() == "Support SprigCast"
        for menu_action in w.menuBar().actions():  # no tick column on the left of any top menu
            assert not any(a.isCheckable() for a in menu_action.menu().actions())
    finally:
        w.close()
        QSettings().clear()


def test_support_pop_up_offers_me_or_either_charity(app):
    from sprigcast.app.support import TIP_URL, support_dialog

    opened = []
    dialog = support_dialog(open_url=opened.append)
    assert dialog.close_button.text() == "Close"
    assert set(dialog.way_buttons) == {"me", "trees", "sloths"}
    dialog.way_buttons["me"].click()
    dialog.way_buttons["trees"].click()
    dialog.way_buttons["sloths"].click()
    assert opened == [TIP_URL, "https://onetreeplanted.org/products/plant-trees",
                      "https://www.slothconservation.org/donate"]
    assert TIP_URL == "https://slothbloom.com/tip"


def test_file_menu_repots_a_recording_as_mp4(app, tmp_path, monkeypatch):
    import av
    import keyring
    import numpy as np

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    from sprigcast.app.main_window import MainWindow

    src = tmp_path / "rec.mkv"
    with av.open(str(src), "w") as out:
        stream = out.add_stream("libx264", rate=30)
        stream.width, stream.height, stream.pix_fmt = 64, 36, "yuv420p"
        for i in range(5):
            for packet in stream.encode(av.VideoFrame.from_ndarray(np.zeros((36, 64, 3), np.uint8), format="rgb24")):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    w = MainWindow(_collection(), tmp_path / "scenes.json")
    try:
        file_menu = w.menuBar().actions()[0].menu()
        assert "Repot as MP4…" in [a.text() for a in file_menu.actions()]
        w.repot([src]).join(30)
        for _ in range(20):
            app.processEvents()
        assert (tmp_path / "rec.mp4").exists() and src.exists()  # the MKV stays unless asked
        assert w.statusBar().currentMessage() == "Repotted rec.mp4"
    finally:
        w.close()


def test_clip_saves_the_shapes_chosen_in_settings(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("ClipTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = _collection()
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        assert [c.id for c in w._clip_canvases()] == ["horizontal", "vertical"]
        col.recording.clip_shapes = "vertical"
        assert [c.id for c in w._clip_canvases()] == ["vertical"]
        col.recording.clip_shapes = "horizontal"
        assert [c.id for c in w._clip_canvases()] == ["horizontal"]
        w.live_side.set_clip(True, 45)  # on and off show by colour; the label never changes
        assert w.live_side.clip.text() == "Clip" and w.live_side.clip.isChecked()
        assert "45 s" in w.live_side.clip.toolTip()
        w.live_side.set_clip(False, 45)
        assert w.live_side.clip.text() == "Clip" and not w.live_side.clip.isChecked()
        w.live_side.clip_saved(2)
        assert w.live_side.clip.text() == "Clip"
        w.live_side.set_recording(True)
        assert w.live_side.record.text() == "Record"
        w.live_side.set_recording(False)
        assert w.live_side.record.text() == "Record"
    finally:
        w.close()
        QSettings().clear()


def test_long_destination_list_scrolls_inside_its_card(app):
    from sprigcast.app.live_stage import PLACES_HEIGHT, Destination, LiveSide

    side = LiveSide(lambda: None, lambda: None, lambda: None, lambda _id: None)
    side.resize(300, 900)
    side.show()
    places = [Destination(id=f"p{i}", name=f"Place {i}", on=True, note="Ready") for i in range(10)]
    side.show_destinations(places, {})
    app.processEvents()
    assert len(side.place_rows) == 10
    assert side.places_scroll.maximumHeight() <= PLACES_HEIGHT  # Chat below keeps its room
    side.show_destinations(places[:1], {})
    app.processEvents()
    assert side.places_scroll.maximumHeight() < PLACES_HEIGHT  # a short list takes only what it needs
    side.close()


def test_destination_rows_restyle_only_what_changed(app, monkeypatch):
    """Live refreshes Destinations every second: restyling every row each time froze the window 532 ms."""
    from PySide6.QtWidgets import QWidget

    from sprigcast.app.live_stage import Destination, LiveSide

    side = LiveSide(lambda: None, lambda: None, lambda: None, lambda _id: None)
    side.show()
    places = [Destination(id="tw", name="Twitch", on=True, note="6000 kbps"),
              Destination(id="yt", name="YouTube", on=False, note="Off")]
    side.show_destinations(places, {"tw": "good"})
    styled = []
    real = QWidget.setStyleSheet
    monkeypatch.setattr(QWidget, "setStyleSheet", lambda self, sheet: (styled.append(self), real(self, sheet)))
    side.show_destinations(places, {"tw": "good"})
    assert styled == []  # nothing changed: nothing restyled or re-laid out
    note = side.place_rows["tw"][2]
    side.show_destinations([Destination(id="tw", name="Twitch", on=True, note="5800 kbps"), places[1]],
                           {"tw": "warning"})
    assert "5800 kbps" in note.text() and styled == []  # a new number is new text, not a new style
    side.show_destinations([Destination(id="tw", name="Twitch", on=False, note="Off"), places[1]], {})
    assert styled == [side.place_rows["tw"][1]]  # switched off: only its name changes colour
    side.close()


def test_places_move_up_down_and_by_dragging_and_keep_their_order(app, tmp_path, monkeypatch):
    """Keep both YouTube places together: Edit's Move up / Move down, or drag a row; one order everywhere."""
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import SceneCollection

    col = _collection()
    col.outputs = [Output(id="yt", type="stream", service="youtube", canvas="horizontal"),
                   Output(id="rec", type="recording", canvas="horizontal"),
                   Output(id="tw", type="stream", service="twitch", canvas="horizontal"),
                   Output(id="yt2", type="stream", service="youtube", canvas="vertical")]
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w._set_mode("live")
        side = w.live_side
        ids = lambda: [p.id for p in side._places]  # noqa: E731
        assert ids() == ["yt", "tw", "yt2"]
        w._place_action("yt2", "up")
        assert ids() == ["yt", "yt2", "tw"]
        assert [o.id for o in col.outputs] == ["yt", "rec", "yt2", "tw"]  # other outputs keep their spots
        w._place_action("yt", "down")
        assert ids() == ["yt2", "yt", "tw"]
        side.move_place("tw", 0)  # dragged to the top
        assert ids() == ["tw", "yt2", "yt"]
        side.move_place("tw", 3)  # and to the bottom (dropped after the last row)
        assert ids() == ["yt2", "yt", "tw"]
        assert [o.id for o in SceneCollection.load(tmp_path / "scenes.json").outputs if o.type == "stream"] \
            == ["yt2", "yt", "tw"]  # saved
        side.set_editing(True)
        assert not side.place_tools["yt2"]["up"].isEnabled() and side.place_tools["yt2"]["down"].isEnabled()
        assert side.place_tools["tw"]["up"].isEnabled() and not side.place_tools["tw"]["down"].isEnabled()
        side.place_tools["tw"]["up"].click()
        assert ids() == ["yt2", "tw", "yt"]
    finally:
        w.close()


def test_bloom_pills_show_their_key_and_numbers_switch(app):
    from sprigcast.app.hotkeys import SCENE_PREFIX
    from sprigcast.app.live_panel import LivePanel

    col = _collection()
    col.hotkeys[SCENE_PREFIX + "h2"] = "Ctrl+2"
    picked = []
    panel = LivePanel(picked.append)
    panel.show()
    panel.show_scenes(col)
    assert panel.buttons["h1"].key == "1"  # no hotkey of its own: its number
    assert panel.buttons["h2"].key == "Ctrl+2"  # its own hotkey wins
    assert panel.number_order == ["h1", "h2"]
    panel._number(2)
    assert picked == ["h2"]
    panel._number(5)  # nothing there
    assert picked == ["h2"]
    panel.close()


def test_encoder_room_from_the_measured_load():
    from sprigcast.app.destinations import encoder_room, room_line

    assert encoder_room(None, 2) is None
    assert encoder_room(30.0, 0) is None
    assert encoder_room(30.0, 2) == 3  # 15% each, up to 85%
    assert encoder_room(80.0, 2) == 0
    assert encoder_room(1.0, 1) == 16  # a near-zero reading counts as 5% each
    assert room_line(30.0, 2) == (True, "Encoder 30% busy · room for about 3 more")
    assert room_line(84.0, 4) == (False, "Encoder 84% busy · no room for more")


def test_a_view_of_the_bloom_on_air_shares_the_canvas_already_drawn():
    """Heavy run 2026-11-17: SprigCast Camera showed the vertical bloom on air, and the view drew all of it a second
    time every frame (5-20 ms). Drawn this tick already, the canvas's picture is used; anything else still draws."""
    from types import SimpleNamespace

    from sprigcast.core.scene import Canvas, Scene, SceneCollection
    from sprigcast.render.engine import Engine

    class Fake:
        def __init__(self):
            self.drawn = []

        def render(self, canvas, *a, **k):
            self.drawn.append(canvas.id)

        def read(self, key):
            return b"" + key.encode()

    engine = Engine(SceneCollection(canvases=[Canvas(id="v", width=16, height=9)],
                                    scenes=[Scene(id="a", name="A", canvas="v"), Scene(id="b", name="B", canvas="v")],
                                    active_scenes={"v": "a"}))
    read_from = []
    engine._output_frame = lambda comp, canvas, cb, size, now, made: read_from.append((canvas.id, id(made))) or \
        made.setdefault(size, SimpleNamespace(fmt="nv12"))
    fake, got, shared = Fake(), [], {}
    engine.set_view("camera", "v", "bloom", "a")
    taps = {"view:camera": [(got.append, (8, 4))]}
    engine._render_views(fake, {}, 10.0, taps, {"v": shared})
    assert fake.drawn == [] and read_from == [("v", id(shared))] and len(got) == 1
    engine._render_views(fake, {}, 10.1, taps, {})  # the canvas wasn't drawn this tick: the view draws it
    assert fake.drawn == ["view:camera"]
    engine.set_view("camera", "v", "bloom", "b")  # a bloom that isn't on air: drawn on its own
    engine._render_views(fake, {}, 10.2, taps, {"v": shared})
    assert fake.drawn == ["view:camera", "view:camera"]


def test_edit_rows_all_draw_and_take_clicks_in_a_short_list(app, tmp_path, monkeypatch):
    """PC test 2026-11-18 (#593): in Edit the list was ~140 px tall but only its top ~80 px drew (one row),
    a blank band hid the second row and its tools, and clicks there missed."""
    import keyring
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    from sprigcast.app.main_window import MainWindow

    col = _collection()
    col.outputs = [Output(id="yt", type="stream", service="youtube", canvas="horizontal"),
                   Output(id="tw", type="stream", service="twitch", canvas="horizontal"),
                   Output(id="yt2", type="stream", service="youtube", canvas="vertical")]
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w.resize(1280, 720)
        w.show()
        w._set_mode("live")
        side = w.live_side
        side.set_editing(True)
        for _ in range(5):
            app.processEvents()
        scroll = side.places_scroll
        content = scroll._content.sizeHint().height()
        assert scroll.height() < content  # short: the list scrolls
        assert scroll.viewportMargins().bottom() == 0 and scroll.viewport().height() == scroll.height()
        gear = side.place_tools["tw"]["settings"]  # the second row's tools
        spot = gear.mapTo(scroll.viewport(), gear.rect().center())
        if spot.y() < scroll.viewport().height():  # on screen: a click there lands on it
            assert scroll.viewport().childAt(spot) is gear or gear.isAncestorOf(scroll.viewport().childAt(spot))
        bar = scroll.verticalScrollBar()
        tops = scroll.row_tops()

        def wheel(dy: int) -> None:
            centre = QPointF(scroll.width() / 2, scroll.height() / 2)
            scroll.wheelEvent(QWheelEvent(centre, scroll.mapToGlobal(centre), QPoint(), QPoint(0, dy),
                                          Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                          Qt.ScrollPhase.NoScrollPhase, False))

        wheel(-120)  # one notch down: the next row's top (or the end)
        assert bar.value() == min(tops[1], bar.maximum())
        wheel(-60)  # half a notch (a touchpad): nothing yet
        assert bar.value() == min(tops[1], bar.maximum())
        wheel(120)
        wheel(120)
        assert bar.value() == 0
    finally:
        w.close()


def test_a_place_is_ended_and_started_again_while_the_rest_stay_live(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("PlaceLiveTest")
    QSettings().clear()
    from sprigcast.app import message as msgbox
    from sprigcast.app.main_window import MainWindow

    class St:
        def __init__(self, output_id, tracks=()):
            self.output_id, self.name, self.tracks = output_id, output_id, tracks

    class Session:
        elapsed = 5.0

        def __init__(self, ids):
            self.streamers = [("horizontal", St(i)) for i in ids]
            self.borrowed, self.added, self.dropped = [], [], []

        def find(self, output_id):
            return next((st for _, st in self.streamers if st.output_id == output_id), None)

        def drop(self, output_id):
            self.dropped.append(output_id)
            self.streamers = [(c, st) for c, st in self.streamers if st.output_id != output_id]
            return True

        def add(self, target, *a, **k):
            self.added.append(target.output_id)
            self.streamers.append(("horizontal", St(target.output_id)))

    col = _collection()
    w = MainWindow(col, tmp_path / "scenes.json")
    warned, ended = [], []
    monkeypatch.setattr(msgbox, "warning", lambda *a, **k: warned.append(a[-1]))
    monkeypatch.setattr(w, "_refresh_stream_status", lambda: w._live_destinations())
    monkeypatch.setattr(w, "_on_live_clicked", lambda: ended.append(True))
    try:
        w._set_mode("live")
        side = w.live_side
        w.streaming = session = Session(["tw", "tt"])
        side.set_live(True)
        w._live_destinations()
        tw, tt = side.place_rows["tw"][3], side.place_rows["tt"][3]
        assert tw.isChecked() and tt.isChecked() and tw.isEnabled()
        # Off: only that place ends; it shows off, the other stays on, and it's remembered as off.
        tw.click()
        assert session.dropped == ["tw"] and not side.place_rows["tw"][3].isChecked()
        assert side.place_rows["tt"][3].isChecked() and not col.outputs[0].enabled
        assert w._sending_ids() == {"tt"}
        # Settings now locks only the place still sending.
        from sprigcast.app.stream_dialog import StreamPage

        page = StreamPage(col)
        page.set_live(w._sending_ids())
        page.select_output("tt")
        assert not page.live_note.isHidden() and not page.remove_button.isEnabled() and not page.enabled.isEnabled()
        page._remove()
        assert [o.id for o in page.outputs] == ["tw", "tt"]  # can't be removed from under its stream
        page.select_output("tw")
        assert page.live_note.isHidden() and page.remove_button.isEnabled()
        page.bitrate.setValue(3210)
        page.bitrate.editingFinished.emit()
        page.select_output("tt")
        page.outputs[1].bitrate_kbps = 1  # even if something changed it, a sending place is saved as it went live
        assert page.apply_to(col) and col.outputs[1].bitrate_kbps != 1
        # On again: it starts with its settings as they are now. A problem puts the switch back and says why.
        monkeypatch.setattr(w, "_stream_target", lambda output, ask=True, quiet=False: (None, "Twitch: add your stream key in Stream…"))
        side.place_rows["tw"][3].click()
        QApplication.processEvents()
        assert warned == ["Twitch: add your stream key in Stream…"] and not side.place_rows["tw"][3].isChecked()
        assert not col.outputs[0].enabled and session.added == []
        monkeypatch.setattr(w, "_stream_target", lambda output, ask=True, quiet=False: (
            type("Target", (), {"name": output.id, "output_id": output.id})(), None))
        side.place_rows["tw"][3].click()
        assert session.added == ["tw"] and side.place_rows["tw"][3].isChecked() and col.outputs[0].enabled
        # The last place sending: its switch is End stream (asked first, if that's on), and goes back meanwhile.
        side.place_rows["tw"][3].click()
        assert w._sending_ids() == {"tt"}
        side.place_rows["tt"][3].click()
        assert ended == [True] and session.dropped == ["tw", "tw"] and side.place_rows["tt"][3].isChecked()
        # Twitch Enhanced lending its picture to others ends alone too: they keep that quality (StreamSession.drop).
        # It used to be refused, leaving no way to drop a failing Twitch but ending the stream (PC, 2026-11-21).
        shared = object()
        lender = St("tw", tracks=(type("T", (), {"encoder": shared})(),))
        session.streamers.append(("horizontal", lender))
        session.borrowed = [shared]
        w._live_destinations()
        side.place_rows["tw"][3].click()
        QApplication.processEvents()
        assert not any("Enhanced Broadcasting" in m for m in warned) and not side.place_rows["tw"][3].isChecked()
        assert session.dropped == ["tw", "tw", "tw"]
    finally:
        w.streaming = None
        w.close()
        QSettings().clear()
