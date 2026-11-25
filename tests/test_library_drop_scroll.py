"""User 2026-11-25: a drop sent the blooms, sprigs and leaves lists back to the top; F2 renames what's picked."""

from tests.test_build_easy import _window

from sprigcast.core.scene import Scene, SceneItem, SourceConfig


def _many(col):
    for i in range(40):
        col.sources.append(SourceConfig(id=f"c{i}", kind="color", name=f"Color {i}", settings={"color": "#112233"}))
        col.scenes.append(Scene(id=f"b{i}", name=f"Bloom {i}", canvas="horizontal"))
    col.scene("h1").items.extend(SceneItem(source=f"c{i}") for i in range(40))


def _settle(app):
    for _ in range(5):
        app.processEvents()


def test_the_lists_keep_their_place_when_rebuilt(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    col, w = _window(tmp_path, monkeypatch, "DropScroll")
    _many(col)
    w.resize(1280, 600)
    w.show()
    w._set_mode("build")
    w._refresh_scenes()
    w._refresh_sources()
    app = QApplication.instance()
    _settle(app)
    for view, rebuild in ((w.scene_tree, w._refresh_scenes), (w.source_list, w._refresh_sources)):
        bar = view.verticalScrollBar()
        assert bar.maximum() > 50, "the list should be long enough to scroll"
        bar.setValue(bar.maximum() // 2)
        was = bar.value()
        rebuild()
        _settle(app)
        assert bar.value() == was
    w.close()


def test_f2_renames_the_picked_bloom_sprig_or_leaf(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    from sprigcast.app import card as popups

    col, w = _window(tmp_path, monkeypatch, "F2Rename")
    w.show()
    w._set_mode("build")
    app = QApplication.instance()
    _settle(app)
    asked = []
    monkeypatch.setattr(popups, "ask_text", lambda parent, title, label, text="", *a, **k: (asked.append(label), ("", False))[1])

    def rename():
        w.item_actions["rename"].trigger()
        _settle(app)

    tree = w.scene_tree
    from sprigcast.app.main_window import ROLE_ROW

    rows = [tree.topLevelItem(0).child(i) for i in range(tree.topLevelItem(0).childCount())]
    bloom = next(r for r in rows if r.data(0, ROLE_ROW) == "bloom")
    tree.setFocus()
    tree.setCurrentItem(bloom)
    _settle(app)
    rename()
    assert asked == ["Bloom name"]  # the bloom's rename, not the leaf's
    w.source_list.setFocus()
    w.source_list.setCurrentRow(0)
    _settle(app)
    rename()
    assert asked[-1] == "Name"  # the leaf's rename
    w.close()
