"""Adding plugin sources from + → Plugins (soak 2026-11-15)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def window(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    _app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastPluginAddTest")
    QSettings().clear()
    from sprigcast import plugin_store
    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.scene import Canvas, Scene, SceneCollection

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                          scenes=[Scene(id="a", name="A", canvas="horizontal"), Scene(id="p", name="P", canvas="horizontal", kind="part")],
                          active_scenes={"horizontal": "a"})
    show = next(p for p in plugin_store.catalog() if p.id == "slide_show")
    plugin_store.install(tmp_path / "plugins", show)
    w = MainWindow(col, tmp_path / "scenes.json")
    w.selected_scene_id = "a"
    yield w, col
    w.close()
    plugin_store.remove(tmp_path / "plugins", "slide_show")


def test_a_slide_show_fills_the_canvas(window, monkeypatch):
    from sprigcast.app import main_window

    w, col = window

    class Accept:
        def __init__(self, _title, values, _defaults, _parent):
            self._values = dict(values)

        def exec(self):
            return True

        def values(self):
            return self._values

    monkeypatch.setattr(main_window, "SettingsDialog", Accept)
    w._add_from_class("slideshow", "Slide show")
    config = next(s for s in col.sources if s.kind == "slideshow")
    item = next(i for s in col.scenes for i in s.items if i.source == config.id)
    assert (config.settings["width"], config.settings["height"]) == (1920, 1080)
    assert (item.x, item.y, item.scale) == (0, 0, 1)


def test_the_plugins_menu_is_not_rebuilt_while_it_opens(window):
    w, _col = window
    w.selected_scene_id = "p"  # editing a sprig
    w._fill_add_source_menu()  # the + menu, with Plugins filled as it's made
    before = list(w.more_menu.actions())
    w.more_menu.aboutToShow.emit()
    assert w.more_menu.actions() == before  # same entries under the pointer: the first click lands
    assert any(a.text() == "Slide show" for a in before)
