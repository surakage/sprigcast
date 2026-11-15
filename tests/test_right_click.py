import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QPointF, QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu  # noqa: E402

from sprigcast.core.scene import Canvas, Output, Scene, SceneCollection, SceneItem, SourceConfig  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def _texts(menu: QMenu) -> list[str]:
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        sources=[SourceConfig(id="bg", kind="color", name="Background")],
        scenes=[Scene(id="h1", name="Main", canvas="horizontal", items=[SceneItem(source="bg")]),
                Scene(id="h2", name="BRB", canvas="horizontal")],
        active_scenes={"horizontal": "h1"},
        outputs=[Output(id="tw", type="stream", service="twitch", canvas="horizontal", key_ref="k/tw")],
    )


def test_right_click_in_a_preview_selects_and_asks_for_a_menu(app):
    from PySide6.QtGui import QMouseEvent

    from sprigcast.app.preview import Preview

    preview = Preview("horizontal", "Horizontal", (1920, 1080))
    preview.resize(984, 564)
    preview.scene = Scene(id="h1", name="Main", canvas="horizontal", items=[SceneItem(source="bg")])
    preview.sizes = {"bg": (1920, 1080)}
    selected, menus = [], []
    preview.selected.connect(lambda cid, i: selected.append(i))
    preview.menu_requested.connect(lambda cid, i, where: menus.append(i))
    inside = preview.to_widget(100, 100)
    event = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(inside.x(), inside.y()), QPointF(0, 0),
                        Qt.MouseButton.RightButton, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier)
    preview.mousePressEvent(event)
    assert selected == [0] and menus == [0] and preview._drag is None  # selected, nothing dragged
    preview.editable = False  # Live mode: previews only show, and right-click offers projectors
    preview.mousePressEvent(event)
    assert menus == [0, -1] and selected == [0]


def test_live_scene_and_destination_menus(app):
    from sprigcast.app.live_panel import LivePanel

    shown, edited = [], []
    panel = LivePanel(shown.append, on_edit=edited.append)
    menu = panel.scene_menu("h2")
    assert _texts(menu) == ["Show now", "Edit in Build"]
    menu.actions()[0].trigger()
    menu.actions()[1].trigger()
    assert shown == ["h2"] and edited == ["h2"]
    panel.studio.setChecked(True)
    assert _texts(panel.scene_menu("h2"))[0] == "Line up next"


def test_main_window_menus(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("RightClickTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_collection(), tmp_path / "scenes.json")
    try:
        w._on_preview_selected("horizontal", 0)
        on_item = _texts(w._preview_menu("horizontal", 0))
        assert "Settings…" in on_item and "Remove" in on_item and "Size and place" in on_item
        menu = w._preview_menu("horizontal", 0)
        size_menu = next(a.menu() for a in menu.actions() if a.text() == "Size and place")
        fit = next(a for a in size_menu.actions() if a.text() == "Fit to canvas")
        assert fit.shortcut().toString() == "Ctrl+F"  # the shortcut shows in the menu
        shape_menu = next(a.menu() for a in menu.actions() if a.text() == "Shape")
        assert "Circle" in _texts(shape_menu) and "Color look" in on_item
        next(a for a in shape_menu.actions() if a.text() == "Circle").trigger()
        bg = w.collection.source("bg")
        assert [(f.kind, f.settings["shape"]) for f in bg.filters] == [("shape", "circle")]
        menu = w._preview_menu("horizontal", 0)
        shape_menu = next(a.menu() for a in menu.actions() if a.text() == "Shape")
        assert next(a for a in shape_menu.actions() if a.text() == "Circle").isChecked()
        next(a for a in shape_menu.actions() if a.text() == "Rectangle (none)").trigger()
        assert bg.filters == []
        assert _texts(w._preview_menu("horizontal", -1)) == ["Add", "Pop out", "Canvas size and frame rate…"]
        w._set_mode("live")
        w._edit_in_build("h2")
        assert w.mode == "build" and w.selected_scene_id == "h2"
    finally:
        w.close()
        QSettings().clear()


def test_window_capture_sound_menu(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("RightClickTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = _collection()
    col.sources.append(SourceConfig(id="win", kind="window_capture", name="Game",
                                    settings={"title": "Game", "process": "game.exe"}))
    col.scenes[0].items = [SceneItem(source="win")]
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w._on_preview_selected("horizontal", 0)
        menu = w._preview_menu("horizontal", 0)
        sound = next(a.menu() for a in menu.actions() if a.text() == "Sound")
        assert _texts(sound)[:2] == ["No sound of its own", "The app's own sound"]
        next(a for a in sound.actions() if a.text() == "The app's own sound").trigger()
        made = next(s for s in w.collection.sources if s.settings.get("capture_of") == "win")
        assert made.kind == "app_audio" and made.settings["apps"] == ["game.exe"]
        sound = next(a.menu() for a in w._preview_menu("horizontal", 0).actions() if a.text() == "Sound")
        assert next(a for a in sound.actions() if a.text() == "The app's own sound").isChecked()
    finally:
        w.close()
        QSettings().clear()
