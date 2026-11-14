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
    assert what_goes(tw, col) == "horizontal · 1920×1080 · 60 fps · 6000 kbps"
    tw.enhanced, tw.enhanced_vertical = True, True
    assert what_goes(tw, col) == "Enhanced · horizontal + vertical"


def test_cards_toggle_edit_and_live(app):
    from sprigcast.app.destinations import DestinationsPanel

    col = _collection()
    toggled, edited = [], []
    panel = DestinationsPanel(lambda oid, on: toggled.append((oid, on)), edited.append, lambda ref: ref == "k/tw")
    panel.show_outputs(col)
    assert list(panel.cards) == ["tw", "tt"]
    tw, tt = panel.cards["tw"], panel.cards["tt"]
    assert tw.switch.isChecked() and tw.state.text() == "Ready"
    assert not tt.switch.isChecked() and tt.state.text() == "Needs a server URL"
    tt.switch.setChecked(True)
    assert toggled == [("tt", True)]
    panel.add_button.click()
    assert edited == [None]

    panel.set_live(True)
    assert not tw.switch.isEnabled() and not panel.add_button.isEnabled()
    panel.show_live({"tw": ("good", "6000 kbps")})
    assert tw.state.text() == "6000 kbps" and tw.property("level") == "good"
    panel.set_live(False)
    assert tw.switch.isEnabled()


def test_live_panel_scene_buttons(app):
    from sprigcast.app.live_panel import LivePanel

    col = _collection()
    picked = []
    panel = LivePanel(picked.append)
    panel.show_scenes(col)
    assert set(panel.buttons) == {"h1", "h2"}  # v1 is linked to h1: its tile switches both
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
        # Build has its own screen too: bloom pills, Inside, the canvas with its twin, the inspector.
        assert w.mode == "build" and w.build_stage.isVisible() and w.source_list.isVisible()
        assert not w.live_stage.isVisible() and not w.dock_bodies["Destinations"].isVisible()
        assert set(w.bloom_bar.pills) == {"h1", "h2"} and w.bloom_bar.pills["h1"].isChecked()
        assert w.previews["vertical"].twin and not w.previews["vertical"].editable
        assert w.previews["horizontal"].editable and not w.previews["horizontal"].twin
        w._set_mode("live")
        # Live has its own screen: the stage, bloom tiles and side column, and only the Chat panel by default.
        assert w.live_stage.isVisible() and w.live_panel.isVisible() and not w.build_stage.isVisible()
        assert not w.dock_bodies["Scenes"].isVisible() and not w.dock_bodies["Audio Mixer"].isVisible()
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
    finally:
        w.close()
        QSettings().clear()


def test_build_canvas_tabs_and_linking(app, tmp_path, monkeypatch):
    import keyring
    from PySide6.QtWidgets import QInputDialog

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
        assert "🔗 Main" in w.scene_tree.topLevelItem(0).child(0).text(0)
        # Re-link the vertical bloom to BRB from the vertical tab; switching BRB on now brings it along.
        w._link_scene("v1", "h2")
        assert col.scene("v1").linked_to == "h2"
        col.set_active("h2")
        assert col.active_scenes["vertical"] == "v1"
        w._link_scene("v1", None)
        assert col.scene("v1").linked_to is None
        # New blooms land on the tab you're on, and can be linked as they're made.
        answers = iter([("Gameplay", True)])
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: next(answers))
        monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("BRB", True))
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


def test_studio_mode_lines_up_then_goes(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("StudioTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = _collection()
    col.scenes.append(Scene(id="v2", name="BRB (vertical)", canvas="vertical", linked_to="h2"))
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w.show()
        w._set_mode("live")
        w.live_panel.studio.setChecked(True)
        assert w.live_panel.go.isVisible() and w.next_previews["horizontal"].isVisible()
        w.live_panel.buttons["h2"].click()  # lines up BRB and its linked vertical scene
        assert col.active_scenes == {"horizontal": "h1", "vertical": "v1"}  # nothing on air changed yet
        assert w.staged == {"horizontal": "h2", "vertical": "v2"} and w.engine.staged == w.staged
        assert w.live_panel.buttons["h2"].property("staged")
        w._on_hotkey("studio_go")
        assert col.active_scenes == {"horizontal": "h2", "vertical": "v2"} and w.staged == {}
        w.live_panel.studio.setChecked(False)
        assert not w.next_previews["horizontal"].isVisible()
        w.live_panel.buttons["h1"].click()  # studio off: switches straight away again
        assert col.active_scenes["horizontal"] == "h1"
        assert QSettings().value("live/studio", type=bool) is False
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
        assert not w.record_canvas_boxes["vertical"].isEnabled()
        assert w.canvas_choice_actions["horizontal"].isChecked()
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
        assert "not on air" in w.previews["horizontal"].subtitle and w.on_air_button.isVisibleTo(w)
        # Inside lists top-most first; dragging Moss above Facecam puts it in front.
        assert [w.source_list.item(i).data(ROLE_NAME) for i in range(2)] == ["Facecam", "Moss"]
        assert w.source_list.item(0).data(ROLE_CHILDREN) == []
        w._on_inside_moved(1, 0)
        assert [i.source for i in col.scene("h2").items] == ["b", "a"]
        assert w.source_list.currentRow() == 0 and w._item().source == "a"
        w._put_on_air()
        assert col.active_scenes["horizontal"] == "h2" and w.engine.staged == {}
        assert not w.on_air_button.isVisibleTo(w)
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
        assert col.outputs[1].enabled and side.place_rows["tt"][2].text() != "Off"
        side.place_rows["tw"][3].click()
        assert not col.outputs[0].enabled and side.place_rows["tw"][2].text() == "Off"
        # Edit opens the list on the first one; only + Add makes a new one.
        w._edit_destination(None)
        assert opened == ["tw"] and len(col.outputs) == 2  # opening settings adds nothing
        # + Add picks a service; one that needs a key opens Settings on the new place.
        from sprigcast.output import twitch_auth

        monkeypatch.setattr(twitch_auth, "connected_account", lambda: None)
        side.add_menu.actions()[1].trigger()  # YouTube
        assert len(col.outputs) == 3 and col.outputs[2].service == "youtube" and opened[-1] == col.outputs[2].id
        monkeypatch.setattr(twitch_auth, "connected_account", lambda: "someone")
        side.add_menu.actions()[0].trigger()  # Twitch with a connected account: ready, nothing to open
        assert col.outputs[3].twitch_account and len(opened) == 2
        # Edit works right here: shape, test mode and remove (twice) without opening Settings.
        side.manage.click()
        assert side.editing and side.manage.text() == "Done" and side.all_settings.isVisibleTo(side)
        tools = side.place_tools["tw"]
        tools["shape"].click()
        assert col.outputs[0].canvas == "vertical" and side.place_tools["tw"]["shape"].text() == "Vertical"
        side.place_tools["tw"]["test"].click()
        assert col.outputs[0].bandwidth_test
        assert "test" not in side.place_tools["tt"]  # TikTok has no test mode
        removed_keys = []
        monkeypatch.setattr("sprigcast.app.main_window.set_key", lambda ref, key: removed_keys.append(ref))
        remove = side.place_tools[col.outputs[2].id]["remove"]
        gone = col.outputs[2].id
        remove.click()
        assert remove.text() == "Sure?" and len(col.outputs) == 4
        remove.click()
        assert gone not in [o.id for o in col.outputs] and removed_keys == [f"sprigcast/{gone}"]
        assert len(opened) == 2
        col.outputs[:] = col.outputs[:2]
        w._live_destinations()
        side.manage.click()
        assert not side.editing
        side.set_live(True)
        switch = side.place_rows["tt"][3]
        assert not switch.isEnabled() and not side.add_place.isEnabled() and not side.manage.isEnabled()
        # Two Twitch destinations say which shape each one sends.
        col.outputs.append(Output(id="tw2", type="stream", service="twitch", canvas="vertical", key_ref="k/tw2"))
        w._live_destinations()
        assert side.place_rows["tw2"][1].text() == "Twitch · vertical"
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
        full = next(a for a in menu.actions() if a.text() == "Full screen on…").menu()
        labels = [a.text() for a in full.actions() if a.text()]
        assert labels[0].startswith("Monitor 1:") and "In a window" in labels
        assert len(menu.actions()) == 1  # Live: only the projector, no editing items
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
        assert titles == ["File", "Edit", "View", "Panels", "Tools", "Help"]
        panels = w.menuBar().actions()[3].menu()
        assert any("web panels" in a.text().lower() for a in panels.actions())
        lines = w.shortcut_lines()
        assert lines and len(lines) == len(set(lines))
        assert any("Ctrl+K" in str(line) for line in lines)
    finally:
        w.close()
        QSettings().clear()
