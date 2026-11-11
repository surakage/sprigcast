"""Shows and parts: the model rules and the GPU drawing a part inside scenes on both canvases."""

import numpy as np
import pytest

from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36), Canvas(id="v", width=36, height=64)],
        sources=[
            SourceConfig(id="red", kind="color", name="Red", settings={"color": "#ff0000ff", "width": 8, "height": 8}),
            SourceConfig(id="cam", kind="scene", name="Cam frame", settings={"scene": "p-cam"}),
            SourceConfig(id="cam2", kind="scene", name="Cam frame", settings={"scene": "p-cam"}),
            SourceConfig(id="inner", kind="scene", name="Badge", settings={"scene": "p-badge"}),
        ],
        scenes=[
            Scene(id="show-h", name="Gaming", canvas="h", items=[SceneItem(source="cam")]),
            Scene(id="show-v", name="Gaming (v)", canvas="v", items=[SceneItem(source="cam2", scale=0.5)]),
            Scene(id="p-cam", name="Cam frame", canvas="h", kind="part",
                  items=[SceneItem(source="red", x=0, y=0), SceneItem(source="inner")]),
            Scene(id="p-badge", name="Badge", canvas="h", kind="part"),
        ],
        active_scenes={"h": "show-h", "v": "show-v"},
    )


def test_shows_and_parts_are_separate():
    col = _collection()
    assert [s.id for s in col.scenes_for("h")] == ["show-h"]
    assert {p.id for p in col.parts()} == {"p-cam", "p-badge"}
    assert {s.id for s in col.part_uses("p-cam")} == {"show-h", "show-v"}
    assert [p.id for p in col.part_order()] == ["p-badge", "p-cam"]  # inner part first
    col.set_active("p-cam")
    assert col.active_scenes["h"] == "show-h"  # a part never goes on air by itself


def test_cycles_are_detected():
    col = _collection()
    badge = col.scene("p-badge")
    assert col.contains(col.scene("p-cam"), "p-badge")
    assert not col.contains(badge, "p-cam")
    col.sources.append(SourceConfig(id="loop", kind="scene", name="Cam frame", settings={"scene": "p-cam"}))
    badge.items.append(SceneItem(source="loop"))
    assert col.contains(badge, "p-cam") and col.contains(col.scene("p-cam"), "p-cam")
    col.part_order()  # must not recurse forever


def test_part_cannot_be_active():
    with pytest.raises(ValueError):
        SceneCollection(
            canvases=[Canvas(id="h", width=64, height=36)],
            scenes=[Scene(id="p", name="P", canvas="h", kind="part")],
            active_scenes={"h": "p"},
        )


moderngl = pytest.importorskip("moderngl")


@pytest.fixture(scope="module")
def compositor():
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    yield comp
    comp.release()


def test_part_drawn_inside_scenes_on_both_canvases(compositor):
    col = _collection()
    part = col.scene("p-cam")
    compositor.upload("red", np.full((8, 8, 4), (255, 0, 0, 255), dtype=np.uint8))
    sizes = {"red": (8, 8), "cam": (64, 36), "cam2": (64, 36), "inner": (64, 36)}
    compositor.parts = {}
    tex = compositor.render_part("p-cam", (64, 36), part, sizes)
    compositor.parts = {("cam", "h"): tex, ("cam2", "v"): tex}
    h, v = col.canvas("h"), col.canvas("v")
    compositor.render(h, col.scene("show-h"), sizes)
    img = np.frombuffer(compositor.read("h"), dtype=np.uint8).reshape(36, 64, 4)
    assert tuple(img[4, 4][:3]) == (255, 0, 0)  # the part's red square
    assert tuple(img[20, 40][:3]) == (0, 0, 0)  # transparent part area shows the canvas behind
    compositor.render(v, col.scene("show-v"), sizes)
    img = np.frombuffer(compositor.read("v"), dtype=np.uint8).reshape(64, 36, 4)
    assert tuple(img[2, 2][:3]) == (255, 0, 0)  # same part at half size on the vertical canvas
    assert tuple(img[6, 6][:3]) == (0, 0, 0)
    compositor.parts = {}


def test_half_transparent_part_keeps_its_color(compositor):
    col = _collection()
    compositor.upload("red", np.full((8, 8, 4), (255, 0, 0, 128), dtype=np.uint8))
    sizes = {"red": (8, 8), "cam": (64, 36), "inner": (64, 36)}
    compositor.parts = {}
    tex = compositor.render_part("p-cam", (64, 36), col.scene("p-cam"), sizes)
    compositor.parts = {("cam", "h"): tex}
    compositor.render(col.canvas("h"), col.scene("show-h"), sizes)
    img = np.frombuffer(compositor.read("h"), dtype=np.uint8).reshape(36, 64, 4)
    assert abs(int(img[4, 4][0]) - 128) <= 3  # 50% red over black, not darkened twice
    compositor.parts = {}


def test_engine_renders_parts_once_and_feeds_the_part_editor(compositor):
    from sprigcast.render.engine import Engine

    col = _collection()
    engine = Engine(col)
    engine._sync_sources(compositor)
    assert engine._sources["cam"].size == (64, 36)  # the part's canvas size
    calls = []
    original = compositor.render_part
    compositor.render_part = lambda *a, **k: calls.append(a[0]) or original(*a, **k)
    try:
        engine.editing_part = "p-cam"
        engine._tick(compositor, 1.0)
    finally:
        compositor.render_part = original
    assert calls == ["p-badge", "p-cam"]  # each once, inner first, though p-cam is used on both canvases
    frame = engine.frames["h"].take()
    img = np.frombuffer(frame.data, dtype=np.uint8).reshape(36, 64, 4)
    assert img[4, 4][0] > 200 and img[4, 4][1] < 50  # red from inside the part
    part_frame = engine.part_frames.take()
    assert part_frame is not None and part_frame.canvas_id == "part:p-cam"
    for source in engine._sources.values():
        source.stop()
    compositor.parts = {}


def test_part_shapes_model():
    col = _collection()
    part = col.scene("p-cam")
    col.sources.append(SourceConfig(id="box", kind="color", name="Box", settings={"width": 16, "height": 9}))
    part.items.append(SceneItem(source="box", x=48, y=0))  # top-right corner of the 64x36 layout
    shape = col.add_shape(part, "v", "p-cam-v", sizes={"box": (16, 9), "red": (8, 8)})
    assert shape.shape_of == "p-cam" and shape.kind == "part" and shape.canvas == "v"
    assert col.add_shape(part, "v", "again") is shape  # one layout per canvas
    assert col.parts() == [part, col.scene("p-badge")] and shape in col.part_scenes()
    assert col.shape_for(part, "v") is shape and col.shape_for(part, "h") is part
    assert col.shape_for(col.scene("p-badge"), "v").id == "p-badge"  # no vertical layout: main one
    box = next(i for i in shape.items if i.source == "box")
    assert box.scale == pytest.approx(36 / 64)  # the smaller ratio
    assert box.x + 16 * box.scale == pytest.approx(36) and box.y == pytest.approx(0)  # still top-right
    nested = [i for i in shape.items if col.source(i.source).kind == "scene"]
    assert nested and nested[0].source != "inner"  # the nested part got its own placement
    assert col.main_part(shape) is part and col.shapes(shape) == [part, shape]
    # a part used by both of another part's shapes counts that part once
    assert col.part_uses("p-badge") == [part]
    # cycles through a shape: the badge can't take the cam frame, since the frame's vertical layout holds it
    assert col.contains(col.scene("p-badge"), "p-cam") is False
    assert col.contains(shape, "p-badge") and col.contains(part, "p-cam-v")
    # the vertical show's placement was fitted for the 64x36 layout; now it fits the 36x64 one
    placement = col.scene("show-v").items[0]
    assert placement.scale == pytest.approx(0.5 / (36 / 64))
    SceneCollection.model_validate(col.model_dump())  # saves and loads
    col.remove_shape(shape)
    assert col.shapes(part) == [part] and placement.scale == pytest.approx(0.5)
    assert not any(s.settings.get("scene") == "p-badge" and s.id != "inner" for s in col.sources)


def test_each_canvas_gets_its_own_layout(compositor):
    from sprigcast.render.engine import Engine

    col = _collection()
    col.scene("p-cam").items = [SceneItem(source="red", x=0, y=0)]
    col.add_shape(col.scene("p-cam"), "v", "p-cam-v")
    col.scene("p-cam-v").items = [SceneItem(source="red", x=28, y=56)]  # bottom-right in the vertical layout
    col.scene("show-v").items = [SceneItem(source="cam2", scale=0.5)]
    engine = Engine(col)
    engine._sync_sources(compositor)
    assert engine._sources["cam"].size == (64, 36) and engine._sources["cam2"].size == (36, 64)
    calls = []
    original = compositor.render_part
    compositor.render_part = lambda *a, **k: calls.append(a[0]) or original(*a, **k)
    try:
        engine._tick(compositor, 1.0)
    finally:
        compositor.render_part = original
    assert sorted(calls) == ["p-cam", "p-cam-v"]
    v = np.frombuffer(engine.frames["v"].take().data, dtype=np.uint8).reshape(64, 36, 4)
    # show-v draws the vertical layout at half scale: red at (14..18, 28..32), nothing at the top-left
    assert v[30, 16][0] > 200 and v[2, 2][0] < 50
    h = np.frombuffer(engine.frames["h"].take().data, dtype=np.uint8).reshape(36, 64, 4)
    assert h[4, 4][0] > 200
    for source in engine._sources.values():
        source.stop()
    compositor.parts = {}


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_main_window_parts(app, tmp_path, monkeypatch):
    """Make a part, place it in a show on each canvas, open it in the part editor, then remove it."""
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("PartsTest")
    QSettings().clear()
    answers = iter([("horizontal", True), ("Webcam frame", True)])
    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    col = load_collection(tmp_path / "scenes.json")
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w.show()
        w._add_part()
        part = w._scene()
        assert part.kind == "part" and part.name == "Webcam frame"
        w._show_part_editor()
        assert w.center.currentIndex() == 1 and w.engine.editing_part == part.id
        shows = [col.active_scenes[c.id] for c in col.canvases]
        for show_id in shows:
            w.selected_scene_id = show_id
            w._refresh_sources()
            w._show_part_editor()
            assert w.center.currentIndex() == 0
            w._add_part_source(part.id)
        assert {s.id for s in col.part_uses(part.id)} == set(shows)
        # give the part a vertical layout from the part editor, then remove it again
        w.selected_scene_id = part.id
        w._refresh_scenes()
        w._show_part_editor()
        assert w.shape_buttons["vertical"].text() == "+ Vertical layout" and not w.remove_shape_button.isVisibleTo(w)
        w._edit_shape("vertical")
        shape = w._scene()
        assert shape.shape_of == part.id and shape.canvas == "vertical" and w.engine.editing_part == shape.id
        assert w.shape_buttons["vertical"].isChecked() and w.remove_shape_button.isVisibleTo(w)
        assert "horizontal + vertical" in w.scene_tree.topLevelItem(w.scene_tree.topLevelItemCount() - 1).child(0).text(0)
        w._edit_shape("horizontal")
        assert w._scene() is part
        w._edit_shape("vertical")
        w._remove_shape()
        assert w._scene() is part and col.shapes(part) == [part]
        w._edit_shape("vertical")  # keep one for the removal below
        # Live lists shows only, never the part
        w._set_mode("live")
        assert part.id not in w.live_panel.buttons and all(s in w.live_panel.buttons for s in shows)
        w._set_mode("build")
        w._remove_part(part)
        assert not any(s.kind == "part" for s in col.scenes)
        assert not any(col.part_of(src) for src in col.sources)
        assert all(it.source in {src.id for src in col.sources} for s in col.scenes for it in s.items)
    finally:
        w.close()
        QSettings().clear()
