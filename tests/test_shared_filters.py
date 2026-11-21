"""A placed sprig has a placement on each canvas. Its filters follow on the other canvas, however they were
changed (the Filters window, filter.set, filter.toggle, an OBS app), unless it keeps its own (user 2026-11-20)."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from sprigcast.control.native import ApiError, NativeApi
from sprigcast.core.scene import Scene, SceneItem, SourceConfig

from test_control_api import FakeHost, _collection  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _api():
    col = _collection()
    return col, NativeApi(FakeHost(col))


def _look(source):
    return [(f.kind, f.enabled, f.settings) for f in source.filters]


def test_a_sprigs_filters_follow_on_the_other_canvas():
    col, api = _api()
    h, v = col.source("frame-h"), col.source("frame-v")
    assert col.filter_twins(h) == [v] and col.filter_twins(v) == [h] and not col.filter_twins(col.source("cam"))
    api.run("filter.add", {"source": "frame-h", "kind": "color_correction", "settings": {"saturation": 0.5}})
    assert _look(v) == _look(h) and v.filters[0].id != h.filters[0].id  # its own copy, with an id of its own
    kept = v.filters[0].id
    api.run("filter.set", {"source": "frame-v", "filter": "color_correction", "on": False})  # either way round
    assert not h.filters[0].enabled and v.filters[0].id == kept
    api.run("filter.toggle", {"source": "frame-h", "filter": "color_correction"})
    api.run("filter.set", {"source": "frame-h", "filter": "color_correction", "settings": {"saturation": 2.0}})
    assert v.filters[0].enabled and v.filters[0].settings["saturation"] == 2.0 and v.filters[0].id == kept
    api.run("filter.add", {"source": "frame-h", "kind": "sharpen"})
    api.run("filter.move", {"source": "frame-h", "filter": "sharpen", "index": 0})
    assert [f.kind for f in v.filters] == ["sharpen", "color_correction"] and v.filters[1].id == kept
    api.run("filter.remove", {"source": "frame-h", "filter": "sharpen"})
    assert _look(v) == _look(h) and len(v.filters) == 1
    assert len({f.id for s in col.sources for f in s.filters}) == 2
    assert api.run("filters.list", {"source": "frame-h"})["same_on_both_canvases"] is True
    assert "same_on_both_canvases" not in api.run("filters.list", {"source": "Cam"})


def test_a_placement_can_keep_its_own_filters():
    col, api = _api()
    h, v = col.source("frame-h"), col.source("frame-v")
    assert api.run("filters.share", {"sprig": "Frame", "on": False}) == {"source": "Frame", "same_on_both_canvases": False}
    assert h.settings == {"scene": "p-frame", "own_filters": True} == v.settings
    api.run("filter.add", {"source": "frame-h", "kind": "sharpen"})
    assert not v.filters
    api.run("filter.set", {"sprig": "Frame", "filter": "sharpen", "on": False})  # the sprig by name: where it has one
    assert not h.filters[0].enabled and not v.filters
    api.run("filters.share", {"sprig": "Frame", "in": "Chatting", "on": True})  # back on: the other canvas gets this one's
    assert h.settings == {"scene": "p-frame"} == v.settings and _look(v) == _look(h)
    with pytest.raises(ApiError):
        api.run("filters.share", {"source": "Cam", "on": True})  # a source is one thing on every canvas already


def test_a_sprig_inside_a_sprig_follows_in_its_other_shape():
    col = _collection()
    col.scenes.append(Scene(id="p-frame-v", name="Frame", canvas="vertical", kind="part", shape_of="p-frame"))
    col.scenes.append(Scene(id="p-badge", name="Badge", canvas="horizontal", kind="part"))
    for sid, scene in (("badge-h", "p-frame"), ("badge-v", "p-frame-v"), ("badge-brb", "brb-h")):
        col.sources.append(SourceConfig(id=sid, kind="scene", name="Badge", settings={"scene": "p-badge"}))
        col.scene(scene).items.append(SceneItem(source=sid))
    api = NativeApi(FakeHost(col))
    api.run("filter.add", {"source": "badge-h", "kind": "sharpen"})
    assert col.source("badge-v").filters and not col.source("badge-brb").filters  # another bloom's stays its own


def test_a_barrel_roll_switched_on_rolls_on_both_canvases(tmp_path, app):
    from sprigcast import plugin_store
    from sprigcast.core.filters import video_effects

    from test_store_effects import _store

    plugins = tmp_path / "plugins"
    assert plugin_store.install(plugins, _store("barrel_roll")) is None
    try:
        col, api = _api()
        api.run("filter.add", {"source": "frame-h", "kind": "barrel_roll", "on": False})
        before = video_effects(col.source("frame-v").filters).spin
        api.run("filter.set", {"source": "frame-h", "filter": "barrel_roll", "on": True})
        after = video_effects(col.source("frame-v").filters).spin
        assert before[0] == 0 and after[0] == 3 and after[2] == before[2] + 1  # a new run: it rolls there too
    finally:
        plugin_store.remove(plugins, "barrel_roll")


def test_the_filters_window_ticks_both_and_has_the_option(app):
    from sprigcast.app.filters_dialog import FiltersDialog

    col, api = _api()
    h, v = col.source("frame-h"), col.source("frame-v")
    api.run("filter.add", {"source": "frame-h", "kind": "sharpen"})
    dialog = FiltersDialog(h, lambda **_: None, collection=col)
    plain = FiltersDialog(col.source("cam"), lambda **_: None, collection=col)
    try:
        assert not dialog.same.isHidden() and dialog.same.isChecked() and dialog.same.text() == "Same on both canvases"
        assert plain.same.isHidden()
        dialog.list.item(0).setCheckState(Qt.CheckState.Unchecked)
        assert not v.filters[0].enabled
        dialog.same.setChecked(False)
        dialog.list.item(0).setCheckState(Qt.CheckState.Checked)
        assert h.filters[0].enabled and not v.filters[0].enabled and v.settings.get("own_filters")
        dialog.same.setChecked(True)
        assert v.filters[0].enabled and "own_filters" not in v.settings
    finally:
        dialog.deleteLater()
        plain.deleteLater()


def test_apps_name_a_sprig_and_every_placement_follows():
    """filter.set {"sprig"} is the sprig wherever it's placed; with "in", only in that bloom (both canvases)."""
    col, api = _api()
    col.sources.append(SourceConfig(id="frame-brb", kind="scene", name="Frame", settings={"scene": "p-frame"}))
    col.scene("brb-h").items.append(SceneItem(source="frame-brb"))
    h, v, brb = col.source("frame-h"), col.source("frame-v"), col.source("frame-brb")
    api.run("filter.add", {"sprig": "Frame", "in": "Chatting", "kind": "sharpen"})
    assert h.filters and v.filters and not brb.filters
    api.run("filter.add", {"sprig": "Frame", "kind": "sharpen"})  # everywhere: the ones without it get it
    assert len(h.filters) == len(v.filters) == len(brb.filters) == 1
    h.filters[0].enabled = False  # out of step: a toggle still leaves them all the same
    assert api.run("filter.toggle", {"sprig": "Frame", "filter": "sharpen"})["on"] is True
    assert h.filters[0].enabled and v.filters[0].enabled and brb.filters[0].enabled
    api.run("filter.set", {"sprig": "Frame", "in": "BRB", "filter": "sharpen", "on": False})
    assert h.filters[0].enabled and not brb.filters[0].enabled
    assert api.run("filters.list", {"sprig": "Frame", "in": "BRB"})["filters"][0]["on"] is False
    api.run("filter.remove", {"sprig": "Frame", "filter": "sharpen"})
    assert not h.filters and not v.filters and not brb.filters
    for bad in ({"sprig": "Frame", "in": "Shorts", "filter": "sharpen", "on": True},
                {"sprig": "Frame", "filter": "sharpen", "on": True}, {"sprig": "Nope", "filter": "x", "on": True}):
        with pytest.raises(ApiError):
            api.run("filter.set", bad)
    with pytest.raises(ApiError):
        api.run("filter.set", {"filter": "sharpen", "on": True})  # neither source nor sprig


def test_a_new_filter_never_gets_an_id_one_had_before():
    """PC 2026-11-21: a new Backdrop came out as "filter-2", the id a removed Pixelate on another source had."""
    col, api = _api()
    first = api.run("filter.add", {"source": "Cam", "kind": "sharpen"})["id"]
    api.run("filter.remove", {"source": "Cam", "filter": first})
    second = api.run("filter.add", {"source": "Alert", "kind": "sharpen"})["id"]
    third = api.run("filter.add", {"sprig": "Frame", "kind": "sharpen"})["id"]
    ids = [first, second, third, *(f.id for s in col.sources for f in s.filters)]
    assert first != second and len(set(ids)) == 4 and all(i.startswith("filter-") for i in ids)  # the twin's own too
