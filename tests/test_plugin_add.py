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
        def __init__(self, _title, values, _defaults, _parent, cls=None, collection=None):
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


def test_at_rest_the_show_lets_go_of_what_it_held(window, monkeypatch):
    """2-hour soak: an hour after stopping, the app sat at ~415 MB against ~200 MB fresh."""
    from sprigcast.core import pacing, painted

    w, _col = window
    trims = []
    monkeypatch.setattr(pacing, "trim_working_set_later", lambda done=None: done(trims.append(1) or True))
    painted._ART[("x", 1, 1)] = object()
    w.recording = object()  # still recording: nothing is let go
    w._rest_if_idle()
    assert not trims and painted._ART
    w.recording = None
    swept = []
    monkeypatch.setattr(pacing, "settle", lambda: swept.append(1) or 0.0)
    w._rest_if_idle()
    assert trims == [1] and not painted._ART
    assert not swept  # on screen: the full look through Python's objects waits (191 ms, 22 late frames)
    w.showMinimized()
    w._sweep_if_due()
    assert swept == [1]
    w._sweep_if_due()
    assert swept == [1]  # once per rest


def test_stopping_several_outputs_rests_once(window, monkeypatch):
    """The soak's stop ran the at-rest clean-up three times in 0.4 s, once per output."""
    w, _col = window
    rests = []
    monkeypatch.setattr(w, "_rest_if_idle", lambda: rests.append(1))
    w.REST_AFTER_MS = 10
    for _ in range(3):  # recording, stream and clips stop together
        w._rest_soon()
    from PySide6.QtCore import QCoreApplication, QElapsedTimer

    clock = QElapsedTimer()
    clock.start()
    while clock.elapsed() < 200:
        QCoreApplication.processEvents()
    assert rests == [1]


def test_one_hitch_in_a_second_does_not_read_busy(window):
    from types import SimpleNamespace

    w, _col = window
    w._late_before = 0
    w._show_render_health(SimpleNamespace(fps=60, dropped_ticks=1, render_ms=3.0))
    assert "Smooth" in w.status.text()
    w._show_render_health(SimpleNamespace(fps=60, dropped_ticks=3, render_ms=3.0))
    assert "Busy" in w.status.text()


def test_started_minimized_the_window_is_drawn_once_first(window, caplog):
    import logging
    import time

    from PySide6.QtWidgets import QApplication

    window, _col = window
    with caplog.at_level(logging.INFO, logger="sprigcast.app.main_window"):
        window.show_minimized_drawn(settle_ms=50)
        assert window.isVisible() and window.windowOpacity() == 0.0  # shown see-through, so it gets drawn
        deadline = time.monotonic() + 3
        while "drawn once first" not in caplog.text and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
    assert "started minimized: drawn once first" in caplog.text
    assert window.windowOpacity() == 1.0


def test_a_plugin_installed_before_the_add_menu_opened_logs_no_error(window, caplog):
    """PC test 2026-11-16: every plugin install logged "no attribute 'more_menu'" (the + menu isn't built yet)."""
    w, _col = window
    if hasattr(w, "more_menu"):
        del w.more_menu
    w._fill_more_menu()  # what an install or update calls
    w.selected_scene_id = "p"  # editing a sprig
    w._fill_add_source_menu()  # opening + later builds the Plugins menu, filled
    assert any(a.text() == "Slide show" for a in w.more_menu.actions())
    w._fill_more_menu()
    assert "more_menu" not in caplog.text
