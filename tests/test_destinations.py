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
    assert set(panel.buttons) == {"h1", "h2", "v1"}
    assert panel.buttons["h1"].isChecked() and not panel.buttons["h2"].isChecked()
    assert "⇄" in panel.buttons["v1"].text()  # linked
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
        assert w.mode == "build" and w.docks["Sources"].isVisible() and not w.docks["Live"].isVisible()
        assert w.docks["Destinations"].isVisible()
        w._set_mode("live")
        assert w.docks["Live"].isVisible() and not w.docks["Sources"].isVisible()
        assert not any(p.editable for p in w.previews.values())
        w.live_panel.buttons["h2"].click()
        assert col.active_scenes["horizontal"] == "h2"
        w._set_mode("build")
        assert w.docks["Sources"].isVisible() and all(p.editable for p in w.previews.values())
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
        assert w.previews["horizontal"].isVisible() and not w.previews["vertical"].isVisible()
        assert "v1" not in {w.scene_tree.topLevelItem(0).child(i).data(0, 256) for i in range(2)}
        assert not w.link_button.isVisible()
        w.canvas_tabs.setCurrentIndex(1)
        assert w.build_canvas == "vertical" and w.selected_scene_id == "v1"
        assert w.previews["vertical"].isVisible() and not w.previews["horizontal"].isVisible()
        assert w.link_button.isVisible()
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
