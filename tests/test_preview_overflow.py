"""Settings → Preview: "Show what's outside the canvas" hatches what hangs past the canvas edge in Build."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.core.scene import Scene, SceneItem  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _preview(on: bool):
    from sprigcast.app.preview import Preview

    view = Preview("horizontal", "Horizontal", (1920, 1080))
    view.resize(800, 600)
    view.show_title = False
    view.show_zoom_bar = False
    view.zoom = 0.5  # zoomed out: room round the canvas
    view.sizes = {"cam": (800, 600)}
    view.set_scene(Scene(id="b", name="B", canvas="horizontal", items=[SceneItem(source="cam", x=1500, y=700)]), None)
    image = QImage(4, 4, QImage.Format.Format_RGBA8888)
    image.fill(QColor("#000000"))
    view._image = image
    view.show_overflow = on
    return view


def _grab_outside(view) -> set:
    """Colours just right of the canvas, level with the camera hanging off it."""
    picture = view.grab().toImage()
    rect = view.canvas_rect()
    y = int(view.to_widget(0, 1000).y())
    x0 = int(rect.right()) + 4
    return {picture.pixelColor(x, y).name() for x in range(x0, x0 + 40)}


def test_overflow_is_drawn_only_when_on(app):
    off, on = _preview(False), _preview(True)
    assert len(_grab_outside(off)) == 1  # just the panel colour
    assert len(_grab_outside(on)) > 1  # hatched where the camera hangs off


def test_the_setting_reaches_builds_canvases(app, tmp_path, monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("OverflowTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow
    from sprigcast.app.preview import OVERFLOW_KEY
    from sprigcast.core.scene import Canvas, SceneCollection

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                          scenes=[Scene(id="h1", name="Main", canvas="horizontal")])
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        assert not any(p.show_overflow for p in w.previews.values())
        QSettings().setValue(OVERFLOW_KEY, True)
        w._apply_preview_settings()
        assert all(p.show_overflow for p in w.previews.values())
    finally:
        w.close()
        QSettings().clear()


def test_settings_apply_reaches_build_and_the_open_windows(app, tmp_path, monkeypatch):
    """PC check 2026-11-19: the tick did nothing until a restart, and an open Settings window stayed in captures
    after "Hide SprigCast from screen capture" was applied."""
    import keyring

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("OverflowApplyTest")
    QSettings().clear()
    from PySide6.QtWidgets import QDialog

    from sprigcast.app import privacy
    from sprigcast.app.main_window import MainWindow
    from sprigcast.app.preview import OVERFLOW_KEY
    from sprigcast.app.projector import Projector
    from sprigcast.core.scene import Canvas, SceneCollection

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                          scenes=[Scene(id="h1", name="Main", canvas="horizontal")])
    w = MainWindow(col, tmp_path / "scenes.json")
    told = []
    monkeypatch.setattr(privacy, "supported", lambda: True)
    monkeypatch.setattr(privacy, "apply_to", lambda widget, on=None: told.append(widget))
    dialog = QDialog(w)
    try:
        w.show()
        dialog.show()
        QSettings().setValue(OVERFLOW_KEY, True)
        w._after_settings()
        assert all(p.show_overflow for p in w.previews.values())
        assert w in told and dialog in told
        assert not any(isinstance(x, Projector) for x in told)
    finally:
        dialog.close()
        w.close()
        QSettings().clear()
