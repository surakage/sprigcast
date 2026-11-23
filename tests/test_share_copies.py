"""Garden check → Share: a page (or picture, or camera) set up twice the same way, the copy for the vertical
canvas, becomes one source; its filters become the vertical ones and its old name still reaches it there (user,
2026-11-23)."""

import pytest

from sprigcast.core.scene import Canvas, Filter, Scene, SceneCollection, SceneItem, SourceConfig

CLOCK = {"url": "http://127.0.0.1:8787/overlay/addons/thsv.local-clock", "width": 760, "height": 100}


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=64, height=36), Canvas(id="vertical", width=36, height=64)],
        sources=[
            SourceConfig(id="clock", kind="browser", name="[B] Local Clock", settings=dict(CLOCK)),
            SourceConfig(id="clock-v", kind="browser", name="[B] Local Clock (Vertical)",
                         settings={**CLOCK, "keep_running": True},
                         filters=[Filter(id="f-v", kind="blur", name="Soft", enabled=False)]),
            SourceConfig(id="ball", kind="browser", name="[B] Magic 8-Ball", settings={"url": "u", "width": 1920, "height": 1080}),
            SourceConfig(id="ball-v", kind="browser", name="[B] Magic 8-Ball (Vertical)",
                         settings={"url": "u", "width": 1080, "height": 1920}),
        ],
        scenes=[Scene(id="h", name="Chat", canvas="horizontal", items=[SceneItem(source="clock"), SceneItem(source="ball")]),
                Scene(id="v", name="Chat", canvas="vertical", linked_to="h",
                      items=[SceneItem(source="clock-v", x=5, y=7), SceneItem(source="ball-v")])],
        active_scenes={"horizontal": "h", "vertical": "v"},
    )


def test_only_the_same_page_at_the_same_size_is_offered():
    col = _collection()
    found = col.shareable_copies()
    assert [(c.id, o.id, canvas) for c, o, canvas in found] == [("clock-v", "clock", "vertical")]


def test_share_puts_the_original_in_the_copys_places():
    col = _collection()
    col.recording.source_record_hide = ["clock-v", "clock"]
    copy, original, canvas = col.shareable_copies()[0]
    col.share_copy(copy, original, canvas)
    assert all(s.id != "clock-v" for s in col.sources)
    placed = col.scene("v").items[0]
    assert placed.source == "clock" and (placed.x, placed.y) == (5, 7)  # where the copy was
    assert [f.id for f in original.canvas_filters["vertical"]] == ["f-v"]  # its filters, ids kept, vertical only
    assert original.filters == []
    assert original.canvas_names == {"[B] Local Clock (Vertical)": "vertical"}
    assert col.source_by_id("clock-v") is original  # apps that saved the copy's id find it
    assert col.named_for_canvas("[B] Local Clock (Vertical)") == (original, "vertical")
    assert original.settings["keep_running"] is True  # either stayed awake: so does the one
    assert col.recording.source_record_hide == ["clock"]
    assert col.shareable_copies() == []


def test_the_old_name_reaches_only_the_vertical_canvas():
    from sprigcast.control.api import ControlApi
    from tests.test_control_api import FakeHost, _ok

    col = _collection()
    copy, original, canvas = col.shareable_copies()[0]
    col.share_copy(copy, original, canvas)
    host = FakeHost(col)
    api = ControlApi(host)
    _ok(api, "SetSourceFilterEnabled", {"sourceName": "[B] Local Clock (Vertical)", "filterName": "Soft",
                                        "filterEnabled": True})
    assert original.canvas_filters["vertical"][0].enabled
    got = _ok(api, "GetSceneItemId", {"sceneName": "Chat", "sourceName": "[B] Local Clock"})
    h_item = col.scene("h").items[0]
    v_item = col.scene("v").items[0]
    _ok(api, "SetSceneItemEnabled", {"sceneName": "Chat", "sceneItemId": got["sceneItemId"], "sceneItemEnabled": False})
    assert not h_item.visible and v_item.visible  # two copies were shown and hidden apart: still so
    assert _ok(api, "GetInputSettings", {"inputName": "[B] Local Clock (Vertical)"})["inputSettings"]


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_garden_check_lists_copies_and_shares_one(app):
    from sprigcast.app.garden_check import GardenCheckPage, GardenSnapshot

    col = _collection()
    shared = []

    def copies():
        return [(c.id, c.name, o.name) for c, o, _ in col.shareable_copies()]

    def share(copy_id):
        c, o, canvas = next(t for t in col.shareable_copies() if t[0].id == copy_id)
        col.share_copy(c, o, canvas)
        shared.append(copy_id)

    page = GardenCheckPage(lambda: GardenSnapshot(), [], copies=copies, share=share)
    page.refresh()
    assert page._copies_card.isVisibleTo(page) and page._copies_box.count() == 2  # title and one copy
    page._share("clock-v")
    assert shared == ["clock-v"] and not page._copies_card.isVisibleTo(page)
