"""Live sprig states: named looks of a sprig, switched from Live, with items fading in and out."""

import os

import pytest

from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig, SprigState


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36)],
        sources=[
            SourceConfig(id="frame", kind="color", name="Frame", settings={"color": "#ff0000ff", "width": 8, "height": 8}),
            SourceConfig(id="alert", kind="color", name="Alert", settings={"color": "#0000ffff", "width": 8, "height": 8}),
            SourceConfig(id="cam", kind="scene", name="Cam frame", settings={"scene": "p-cam"}),
        ],
        scenes=[
            Scene(id="show", name="Gaming", canvas="h", items=[SceneItem(source="cam")]),
            Scene(id="p-cam", name="Cam frame", canvas="h", kind="part",
                  items=[SceneItem(source="frame"), SceneItem(source="alert")],
                  states=[SprigState(name="Quiet", hide=["alert"]), SprigState(name="Alert showing")]),
        ],
        active_scenes={"h": "show"},
    )


def test_states_hide_items_and_round_trip():
    col = _collection()
    part = col.scene("p-cam")
    assert col.state_hidden(part) == set()  # no state: as built
    col.set_state("p-cam", "Quiet")
    assert col.state_hidden(part) == {"alert"}
    with pytest.raises(ValueError):
        col.set_state("p-cam", "Nope")
    again = SceneCollection.from_json(col.model_dump_json())
    assert again.scene("p-cam").state == "Quiet" and again.state_hidden(again.scene("p-cam")) == {"alert"}
    col.set_state("p-cam", None)
    assert col.state_hidden(part) == set()


class FakeCompositor:
    def __init__(self):
        self.parts, self.drawn = {}, []

    def render_part(self, key, size, scene, sizes, fades=None):
        self.drawn.append(([i.source for i in scene.items if i.visible], fades))
        return key

    def read(self, key):
        return b""


def test_engine_fades_between_states(monkeypatch):
    from sprigcast.render import engine as engine_mod
    from sprigcast.render.engine import Engine

    clock = [100.0]
    monkeypatch.setattr(engine_mod.time, "perf_counter", lambda: clock[0])
    col = _collection()
    col.set_state("p-cam", "Quiet")
    engine = Engine(col)
    fake = FakeCompositor()
    engine._render_parts(fake, {}, 0.0)
    assert fake.drawn[-1] == (["frame"], None)  # Quiet hides the alert
    new = col.model_copy(deep=True)
    new.set_state("p-cam", "Alert showing")
    engine._start_state_fades(engine._collection, new)
    engine._collection = new
    clock[0] += 0.15  # halfway through the fade
    engine._render_parts(fake, {}, 0.0)
    visible, fades = fake.drawn[-1]
    assert visible == ["frame", "alert"] and fades == {"alert": pytest.approx(0.5)}
    clock[0] += 1.0  # done
    engine._render_parts(fake, {}, 0.0)
    assert fake.drawn[-1] == (["frame", "alert"], None)
    # Being edited: every item shows, whatever the state.
    new.set_state("p-cam", "Quiet")
    engine.editing_part = "p-cam"
    engine._render_parts(fake, {}, 0.0)
    assert fake.drawn[-1] == (["frame", "alert"], None)


def test_hotkey_actions_list_states():
    from sprigcast.app.hotkeys import actions

    ids = [a for a, _ in actions(_collection())]
    assert "state:p-cam:" in ids and "state:p-cam:Quiet" in ids and "state:p-cam:Alert showing" in ids


moderngl = pytest.importorskip("moderngl")


def test_live_panel_and_dialog(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings, Qt
    from PySide6.QtWidgets import QApplication, QInputDialog

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("StatesTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow
    from sprigcast.app.states_dialog import StatesDialog

    col = _collection()
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        panel = w.live_panel
        panel.show_scenes(col)
        assert set(panel.state_buttons) == {("p-cam", None), ("p-cam", "Quiet"), ("p-cam", "Alert showing")}
        assert panel.state_buttons[("p-cam", None)].isChecked()
        panel.state_buttons[("p-cam", "Quiet")].click()
        assert col.scene("p-cam").state == "Quiet" and panel.state_buttons[("p-cam", "Quiet")].isChecked()
        w._on_hotkey("state:p-cam:")
        assert col.scene("p-cam").state is None
        # The dialog: add a state starting from the selected one, untick an item, save.
        dialog = StatesDialog(col, col.scene("p-cam"))
        assert [dialog.item_list.item(i).text() for i in range(2)] == ["Alert", "Frame"]
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Off", True))
        dialog._add()
        assert dialog.states[-1].name == "Off" and dialog.states[-1].hide == ["alert"]  # copied from Quiet
        dialog.item_list.item(1).setCheckState(Qt.CheckState.Unchecked)  # hide Frame too
        assert dialog.states[-1].hide == ["alert", "frame"]
        col.set_state("p-cam", "Quiet")
        dialog._fill_states(0)
        dialog._remove()  # Quiet goes; the sprig falls back to as built
        dialog.apply_to(col.scene("p-cam"))
        assert [s.name for s in col.scene("p-cam").states] == ["Alert showing", "Off"]
        assert col.scene("p-cam").state is None
    finally:
        w.close()
        QSettings().clear()


def test_gpu_fading_item_is_drawn_at_its_opacity():
    import numpy as np

    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    col = _collection()
    comp.upload("frame", np.full((8, 8, 4), (255, 0, 0, 255), dtype=np.uint8))
    comp.upload("alert", np.full((8, 8, 4), (0, 0, 255, 255), dtype=np.uint8))
    part = col.scene("p-cam").model_copy(deep=True)
    part.items[1].x = 20
    comp.render_part("p-cam", (64, 36), part, {"frame": (8, 8), "alert": (8, 8)}, {"alert": 0.5})
    img = np.frombuffer(comp.read("part:p-cam"), dtype=np.uint8).reshape(36, 64, 4)
    assert tuple(img[4, 4]) == (255, 0, 0, 255)  # not fading: solid
    r, g, b, a = (int(v) for v in img[4, 24])
    assert abs(a - 128) <= 2 and abs(b - 128) <= 2  # half way, premultiplied
    comp.release()
