"""Per-placement tweaks: one placement of a sprig hides an item; the sprig and its other placements don't."""

import os

import numpy as np
import pytest

from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36)],
        sources=[
            SourceConfig(id="red", kind="color", name="Red", settings={"color": "#ff0000ff", "width": 8, "height": 8}),
            SourceConfig(id="blue", kind="color", name="Blue", settings={"color": "#0000ffff", "width": 8, "height": 8}),
            SourceConfig(id="cam", kind="scene", name="Cam frame", settings={"scene": "p-cam"}),
            SourceConfig(id="cam2", kind="scene", name="Cam frame", settings={"scene": "p-cam", "hide": ["blue"]}),
        ],
        scenes=[
            Scene(id="show-a", name="Gaming", canvas="h", items=[SceneItem(source="cam")]),
            Scene(id="show-b", name="Chatting", canvas="h", items=[SceneItem(source="cam2")]),
            Scene(id="p-cam", name="Cam frame", canvas="h", kind="part",
                  items=[SceneItem(source="red"), SceneItem(source="blue", x=20)]),
        ],
        active_scenes={"h": "show-a"},
    )


def test_placement_view_hides_only_its_items():
    col = _collection()
    part = col.scene("p-cam")
    assert col.hidden_here(col.source("cam")) == set()
    assert col.placement_view(col.source("cam"), part) is part  # untweaked: the layout itself
    view = col.placement_view(col.source("cam2"), part)
    assert [i.visible for i in view.items] == [True, False]
    assert all(i.visible for i in part.items)  # the sprig itself is unchanged


class FakeCompositor:
    def __init__(self):
        self.parts, self.drawn, self.fades = {}, [], []

    def render_part(self, key, size, scene, sizes, fades=None):
        self.drawn.append((key, [i.source for i in scene.items if i.visible]))
        self.fades.append(fades)
        return key  # stands in for the texture

    def read(self, key):
        return b""


def test_engine_renders_a_copy_for_the_tweaked_placement():
    from sprigcast.render.engine import Engine

    engine = Engine(_collection())
    fake = FakeCompositor()
    engine._render_parts(fake, {}, 0.0)
    assert fake.drawn == [("p-cam", ["red", "blue"]), ("p-cam~cam2", ["red"])]
    assert fake.parts == {("cam", "h"): "p-cam", ("cam2", "h"): "p-cam~cam2"}


moderngl = pytest.importorskip("moderngl")


def test_main_window_tweak_menu(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication, QMenu

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("TweaksTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = _collection()
    col.source("cam2").settings = {"scene": "p-cam"}
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        menu = QMenu()
        w._fill_tweaks_menu(menu, "cam2")
        names = [a.text() for a in menu.actions() if a.isCheckable()]
        assert names == ["Blue", "Red"]  # top-most first
        blue = menu.actions()[0]
        blue.setChecked(False)  # hide Blue in this bloom only
        assert col.source("cam2").settings["hide"] == ["blue"]
        assert col.hidden_here(col.source("cam")) == set()
        assert "1 hidden here" in w._source_label("cam2")
        w._reset_tweaks("cam2")
        assert "hide" not in col.source("cam2").settings
    finally:
        w.close()
        QSettings().clear()


def test_gpu_tweaked_placement_shows_without_hidden_item():
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    col = _collection()
    sizes = {"red": (8, 8), "blue": (8, 8)}
    comp.upload("red", np.full((8, 8, 4), (255, 0, 0, 255), dtype=np.uint8))
    comp.upload("blue", np.full((8, 8, 4), (0, 0, 255, 255), dtype=np.uint8))
    part = col.scene("p-cam")
    full = comp.render_part("p-cam", (64, 36), part, sizes)
    tweaked = comp.render_part("p-cam~cam2", (64, 36), col.placement_view(col.source("cam2"), part), sizes)
    comp.parts = {("cam", "h"): full, ("cam2", "h"): tweaked}
    for show, blue_visible in (("show-a", True), ("show-b", False)):
        comp.render(col.canvas("h"), col.scene(show), {**sizes, "cam": (64, 36), "cam2": (64, 36)})
        img = np.frombuffer(comp.read("h"), dtype=np.uint8).reshape(36, 64, 4)
        assert tuple(img[4, 4]) == (255, 0, 0, 255)
        assert (tuple(img[4, 24]) == (0, 0, 255, 255)) is blue_visible
    comp.release()
