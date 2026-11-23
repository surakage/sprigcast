"""One source on both canvases with each canvas's own picture filters ("Same on both canvases" off): the source
runs once, only its drawing differs, and triggers can name one canvas (user, 2026-11-23)."""

from types import SimpleNamespace

import pytest

from sprigcast.core.scene import Canvas, Filter, Scene, SceneCollection, SceneItem, SourceConfig, item_key


def _collection():
    blur = Filter(id="f-blur", kind="blur", name="Blur", enabled=True, settings={"size": 8})
    look = Filter(id="f-cc", kind="color_correction", name="Tone", enabled=True, settings={"brightness": 0.2})
    cut = Filter(id="f-bg", kind="background_effect", name="Backdrop", enabled=False)
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=64, height=36), Canvas(id="vertical", width=36, height=64)],
        sources=[SourceConfig(id="cam", kind="color", name="Cam", filters=[look, blur, cut]),
                 SourceConfig(id="logo", kind="color", name="Logo")],
        scenes=[Scene(id="h", name="Chat", canvas="horizontal", items=[SceneItem(source="cam"), SceneItem(source="logo")]),
                Scene(id="v", name="Chat", canvas="vertical", linked_to="h", items=[SceneItem(source="cam")])],
        active_scenes={"horizontal": "h", "vertical": "v"},
    )


def test_a_canvas_gets_its_own_copy_and_the_source_keeps_one():
    col = _collection()
    cam = col.source("cam")
    assert col.canvases_showing(cam) == ["horizontal", "vertical"] and col.canvases_showing(col.source("logo")) == ["horizontal"]
    assert col.filters_for(cam, "vertical") is cam.filters  # the same on both, as before
    col.set_canvas_filters(cam, "vertical", True)
    own = cam.canvas_filters["vertical"]
    assert [f.kind for f in own] == ["color_correction", "blur"]  # Backdrop changes the source: it stays shared
    assert {f.id for f in own}.isdisjoint({f.id for f in cam.filters})  # new ids: apps tell them apart
    own[1].enabled = False
    assert [f.kind for f in col.filters_for(cam, "vertical")] == ["background_effect", "color_correction", "blur"]
    assert cam.filters[1].enabled  # horizontal untouched
    col.set_canvas_filters(cam, "vertical", False)
    assert cam.canvas_filters == {}


def test_the_engine_draws_each_canvas_with_its_own_filters():
    from sprigcast.core.filters import video_effects
    from sprigcast.render.engine import Engine

    col = _collection()
    cam = col.source("cam")
    col.set_canvas_filters(cam, "vertical", True)
    cam.canvas_filters["vertical"] = [f for f in cam.canvas_filters["vertical"] if f.kind == "blur"]
    engine = Engine(col)
    engine._sources = {"cam": SimpleNamespace(), "logo": SimpleNamespace()}
    compositor = SimpleNamespace()
    engine._sync_filters(compositor, {s.id: s for s in col.sources})
    assert compositor.split_sources == {"cam"}
    vertical = compositor.effects[item_key("v", "cam")]
    assert vertical == video_effects(col.filters_for(cam, "vertical")) and vertical.brightness == 0.0
    horizontal = compositor.effects["cam"]  # the main canvas draws with the source's own
    assert horizontal.brightness == pytest.approx(0.2) and horizontal.blur > 0
    assert "logo" not in compositor.split_sources and item_key("h", "logo") not in compositor.effects


def test_the_native_snapshot_takes_the_canvas_list():
    from sprigcast.core.filters import video_effects
    from sprigcast.render import native_snapshot

    col = _collection()
    fx_h = video_effects(col.source("cam").filters)
    fx_v = video_effects([Filter(id="x", kind="blur", name="Blur", enabled=True, settings={"size": 3})])
    effects = {"cam": fx_h, item_key("v", "cam"): fx_v}
    draws = [SimpleNamespace(scene=col.scene(sid), key=sid, size=(64, 36), fades=None) for sid in ("h", "v")]
    built = native_snapshot.build(draws, effects, split=frozenset({"cam"}))
    s = built.struct
    items = [s.items[i] for i in range(s.item_count)]
    assert items[0].effects != items[2].effects and items[1].effects == -1  # horizontal, logo (none), vertical
    assert s.effects[items[2].effects].blur == pytest.approx(fx_v.blur)
    plain = native_snapshot.build(draws, effects).struct  # not split: both draw the source's own
    assert plain.items[0].effects == plain.items[2].effects


def test_a_trigger_can_name_one_canvas():
    from tests.test_control_api import _api, _fails, _ok
    from sprigcast.control.protocol import Status

    api, host = _api()
    col = host.collection
    cam = SourceConfig(id="cam2", kind="color", name="Facecam",
                       filters=[Filter(id="f-1", kind="blur", name="Blur", enabled=False, settings={"size": 4})])
    col.sources.append(cam)
    col.set_canvas_filters(cam, "vertical", True)
    _ok(api, "SetSourceFilterEnabled", {"sourceName": "Facecam (Vertical)", "filterName": "Blur", "filterEnabled": True})
    assert cam.canvas_filters["vertical"][0].enabled and not cam.filters[0].enabled  # vertical only
    _ok(api, "SetSourceFilterEnabled", {"sourceName": "Facecam", "filterName": "Blur", "filterEnabled": True})
    assert cam.filters[0].enabled
    listed = _ok(api, "GetSourceFilterList", {"sourceName": "Facecam (vertical)"})["filters"]
    assert [f["filterName"] for f in listed] == ["Blur"]
    col.set_canvas_filters(cam, "vertical", False)  # same on both again: the vertical name reaches the shared list
    _ok(api, "SetSourceFilterEnabled", {"sourceName": "Facecam (Vertical)", "filterName": "Blur", "filterEnabled": False})
    assert not cam.filters[0].enabled
    assert _fails(api, "GetSourceFilterList", {"sourceName": "Facecam (Sideways)"}) == Status.RESOURCE_NOT_FOUND


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_filters_window_edits_each_canvas(app):
    from sprigcast.app.filters_dialog import FiltersDialog

    col = _collection()
    cam = col.source("cam")
    saves = []
    dialog = FiltersDialog(cam, lambda save=False: saves.append(save), None, "video", collection=col)
    assert dialog.same.isVisibleTo(dialog) and dialog.same.isChecked()
    assert not dialog.canvas_switch.isVisibleTo(dialog)
    dialog.same.setChecked(False)
    assert "vertical" in cam.canvas_filters and dialog.canvas_switch.isVisibleTo(dialog)
    dialog._pick_canvas("vertical")
    assert [f.kind for f in dialog._shown()] == ["color_correction", "blur"]
    dialog._remove()  # the picked one (Tone) from vertical only
    assert [f.kind for f in cam.canvas_filters["vertical"]] == ["blur"] and len(cam.filters) == 3
    dialog._pick_canvas("horizontal")
    assert len(dialog._shown()) == 3
    dialog.same.setChecked(True)
    assert cam.canvas_filters == {} and saves
    logo = FiltersDialog(col.source("logo"), lambda save=False: None, None, "video", collection=col)
    assert not logo.same.isVisibleTo(logo)  # on one canvas only: nothing to choose
