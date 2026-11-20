"""Live's layout: the Both / Horizontal / Vertical switch, the bloom lists, the header and the short Transitions card."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture
def app():
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("LiveLayoutTest")
    QSettings().clear()
    yield app
    QSettings().clear()


def _window(tmp_path, monkeypatch):
    import keyring

    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    return MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")


def test_view_switch_shows_one_canvas_bigger_and_the_blooms_follow(app, tmp_path, monkeypatch):
    w = _window(tmp_path, monkeypatch)
    try:
        w.show()
        w._set_mode("live")
        side = w.live_side
        assert side.view_buttons["both"].isChecked() and side.view_group.isVisibleTo(side.stage_bar)
        assert all(w.canvas_columns[c].isVisibleTo(w.splitter) for c in ("horizontal", "vertical"))
        canvases = {w.collection.scene(sid).canvas for sid in w.live_panel.buttons}
        assert canvases == {"horizontal", "vertical"}  # a list per canvas, side by side
        side.view_buttons["vertical"].click()
        assert QSettings().value("live/canvas_view") == "vertical"
        assert not w.canvas_columns["horizontal"].isVisibleTo(w.splitter)
        assert w.canvas_columns["vertical"].isVisibleTo(w.splitter)
        assert {w.collection.scene(sid).canvas for sid in w.live_panel.buttons} == {"vertical"}
        assert side.view_buttons["vertical"].isChecked() and not side.view_buttons["both"].isChecked()
        w._set_mode("build")  # Build keeps its own tab: the Live view doesn't hide what you edit
        assert w.canvas_columns[w.build_canvas].isVisibleTo(w.splitter)
        w._set_mode("live")
        w.collection.canvas("vertical").enabled = False  # one canvas streaming: no choice to make
        w._apply_canvas_view()
        assert not side.view_group.isVisibleTo(side.stage_bar)
        assert w.canvas_columns["horizontal"].isVisibleTo(w.splitter)
    finally:
        w.close()


def test_header_holds_go_live_record_and_the_garden_check(app, tmp_path, monkeypatch):
    w = _window(tmp_path, monkeypatch)
    try:
        w.show()
        side = w.live_side
        assert side.live.text() == "Go live" and not side.header_box.isVisible()  # Live only: Build's top bar is for building
        side.set_live(True)
        assert side.live.text() == "End stream"
        side.set_live(False)
        w._set_mode("live")
        app.processEvents()  # the top bar lays its widgets out on the next pass
        assert side.header_box.isVisible()
        assert not w.screenshot_button.isVisible()  # Live has Screenshot under the canvases
        shots = []
        monkeypatch.setattr(w, "_screenshot", lambda *a, **k: shots.append(1))
        side.screenshot.click()
        assert shots == [1]
    finally:
        w.close()


def test_transitions_card_is_short_and_says_how_many_rules(app):
    from sprigcast.app.transitions_panel import TransitionsPanel
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, Transition, TransitionRule

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                          scenes=[Scene(id="a", name="A", canvas="horizontal")])
    panel = TransitionsPanel(col.transition, lambda: None, collection=col, compact=True)
    panel.resize(300, 300)
    panel.show()
    assert panel.rules_count.text() == "None"
    assert panel.form.labelForField(panel.length).text() == "Duration"
    assert panel.kind_pick.text() == "Fade"  # no "Type" label, no ▾ text: a drawn chevron
    col.transition_rules = [TransitionRule(to_scene="a"), TransitionRule(from_scene="a", transition=Transition())]
    panel.show_rule_count()
    assert panel.rules_count.text() == "2 active"
    before = panel.length.value()
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    top = QPointF(panel.length.width() - 10, 4)
    panel.length.mousePressEvent(QMouseEvent(QMouseEvent.Type.MouseButtonPress, top, panel.length.mapToGlobal(QPoint(0, 0)),
                                             Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                             Qt.KeyboardModifier.NoModifier))
    assert panel.length.value() == before + panel.length.singleStep()  # the up chevron
    panel.close()


def test_a_long_bloom_name_takes_two_lines_in_a_narrow_list(app):
    from sprigcast.app.live_panel import BloomTile

    tile = BloomTile("Just Chatting with a very long bloom name")
    tile.wrap = True
    tile.show()
    tile.resize(140, BloomTile.HEIGHT)
    assert tile.height() == BloomTile.TALL  # two lines instead of "Just…"
    first, second = tile._lines(tile._text_width())
    assert first.startswith("Just") and second
    tile.resize(600, tile.height())
    assert tile.height() == BloomTile.HEIGHT  # room again: back to one line
    short = BloomTile("Gaming")
    short.wrap = True
    short.show()
    short.resize(140, BloomTile.HEIGHT)
    assert short.height() == BloomTile.HEIGHT
    tile.close()
    short.close()


def test_edit_drags_blooms_into_order_without_switching(app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from sprigcast.core.scene import Scene

    w = _window(tmp_path, monkeypatch)
    try:
        col = w.collection
        for name in ("Gameplay", "Just chatting"):
            col.scenes.append(Scene(id=name.lower().replace(" ", "-"), name=name, canvas="horizontal"))
        w.show()
        w._set_mode("live")
        panel = w.live_panel
        panel.show_scenes(col)
        box = next(b for b, ids in panel._groups.items() if "gameplay" in ids)
        before = list(panel._groups[box])
        on_air = dict(col.active_scenes)
        panel.edit_button.click()
        assert panel.editing and panel.edit_button.text() == "Done"
        QTest.mouseClick(panel.buttons["gameplay"], Qt.MouseButton.LeftButton)  # Edit: a click switches nothing
        app.processEvents()
        assert col.active_scenes == on_air
        panel.move_bloom(box, "just-chatting", 0)  # drag it to the top
        QTest.qWait(20)
        horizontal = [s.id for s in col.scenes_for("horizontal")]
        assert horizontal[0] == "just-chatting" and sorted(horizontal) == sorted(before)
        assert col.active_scenes == on_air  # reordering never changes what's on air
        assert panel.number_order[0] == "just-chatting"  # 1 follows the new order
        assert panel.buttons["just-chatting"].key == "1"
        assert panel.editing  # still editing after the lists rebuilt
        panel.edit_button.click()
        assert not panel.editing and panel.edit_button.text() == "Edit"
        assert panel.drop_index(next(iter(panel._groups)), -5) == 0
    finally:
        w.close()
