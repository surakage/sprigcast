"""layout.get: where everything sits, read from the garden (StreamBridge tidies layouts with it, 2026-11-20)."""

import pytest

from sprigcast.control.native import ApiError, NativeApi
from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig

from test_control_api import FakeHost


class Host(FakeHost):
    def source_sizes(self):
        return {"cam": (400, 200), "logo": (100, 100)}


def _garden():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        sources=[
            SourceConfig(id="cam", kind="camera", name="Cam"),
            SourceConfig(id="logo", kind="image", name="Logo"),
            SourceConfig(id="chat", kind="browser", name="Chat", settings={"width": 300, "height": 600}),
            SourceConfig(id="ghost", kind="image", name="Ghost"),  # nothing knows its size yet
            SourceConfig(id="box-src", kind="scene", name="Box", settings={"scene": "p-box"}),
        ],
        scenes=[
            Scene(id="main-h", name="Main", canvas="horizontal", items=[
                SceneItem(source="cam", x=100, y=50, scale=2.0, crop=(10, 0, 30, 20)),
                SceneItem(source="box-src", x=960, y=0, scale=0.5),
                SceneItem(source="chat", x=1500, y=100, bounds="fit", bounds_size=(400, 400), locked=True),
                SceneItem(source="ghost", x=5, y=5, visible=False),
            ]),
            Scene(id="main-v", name="Main", canvas="vertical", linked_to="main-h",
                  items=[SceneItem(source="cam", x=0, y=0, scale=1.0)]),
            Scene(id="p-box", name="Box", canvas="horizontal", kind="part", items=[
                SceneItem(source="logo", x=200, y=400, scale=3.0),
                SceneItem(source="cam", x=0, y=0, rotation=90, align="top_left"),
            ]),
        ],
        active_scenes={"horizontal": "main-h", "vertical": "main-v"},
    )


def _api():
    return NativeApi(Host(_garden()))


def test_every_item_says_its_place_its_size_and_its_box():
    out = _api().run("layout.get", {"bloom": "Main", "shape": "horizontal"})
    assert out["bloom"] == "Main" and list(out["shapes"]) == ["horizontal"]
    shape = out["shapes"]["horizontal"]
    assert shape["canvas"] == {"width": 1920, "height": 1080}
    ghost, chat, box, cam = shape["items"]  # top of the stack first
    assert [i["name"] for i in shape["items"]] == ["Ghost", "Chat", "Box", "Cam"]
    # Cam: 400x200 cropped to 360x180, twice as big, at (100, 50).
    assert cam["kind"] == "camera" and cam["source_size"] == {"width": 400, "height": 200}
    assert cam["crop"] == {"left": 10, "top": 0, "right": 30, "bottom": 20}
    assert (cam["x"], cam["y"], cam["scale_x"], cam["scale_y"], cam["rotation"]) == (100, 50, 2.0, 2.0, 0)
    assert cam["size"] == {"width": 720, "height": 360}
    assert cam["box"] == {"x": 100, "y": 50, "width": 720, "height": 360}
    assert cam["corners"] == [[100, 50], [820, 50], [100, 410], [820, 410]]
    # Chat: no picture yet, so the page's own width and height; its bounds are its box.
    assert chat["source_size"] == {"width": 300, "height": 600} and chat["locked"] is True
    assert chat["bounds"] == {"mode": "fit", "width": 400, "height": 400, "align": "center"}
    assert chat["box"] == {"x": 1500, "y": 100, "width": 400, "height": 400}
    # Ghost: size not known, so no box (never a made-up one), and hidden.
    assert ghost["source_size"] is None and ghost["box"] is None and ghost["visible"] is False


def test_what_is_inside_a_sprig_is_carried_to_the_canvas():
    out = _api().run("layout.get", {"bloom": "Main", "shape": "horizontal"})
    box = out["shapes"]["horizontal"]["items"][2]
    assert box["sprig"] is True and box["kind"] == "sprig" and box["id"] == "p-box"
    assert box["source_size"] == {"width": 1920, "height": 1080}  # a sprig is as big as its canvas
    assert box["box"] == {"x": 960, "y": 0, "width": 960, "height": 540}
    turned, logo = box["items"]
    # Logo: 100x100 three times as big at (200, 400) in the sprig; the sprig is half size at (960, 0).
    assert (logo["x"], logo["y"]) == (200, 400) and logo["in"] == ["Box"]  # its own place stays in sprig pixels
    assert logo["box"] == {"x": 1060, "y": 200, "width": 150, "height": 150}
    # Cam turned 90° clockwise around its top-left corner: 400x200 becomes 200 wide and 400 tall, to the left.
    assert turned["rotation"] == 90
    assert turned["box"] == {"x": 860, "y": 0, "width": 100, "height": 200}


def test_flat_and_overlaps():
    api = _api()
    shape = api.run("layout.get", {"bloom": "Main", "shape": "horizontal", "flat": True, "overlaps": True})["shapes"]["horizontal"]
    assert [i["name"] for i in shape["items"]] == ["Ghost", "Chat", "Cam", "Logo", "Cam"]  # no sprigs, top first
    assert shape["overlaps"] == []  # the hidden Ghost over the Cam doesn't count
    api.col.scene("main-h").items[3].visible = True
    api.col.scene("main-h").items[2].x = 700  # the chat's box now starts inside the Cam's
    api.host.source_sizes = lambda: {"cam": (400, 200), "logo": (100, 100), "ghost": (50, 50)}
    pairs = api.run("layout.get", {"bloom": "Main", "shape": "horizontal", "overlaps": True})["shapes"]["horizontal"]["overlaps"]
    # The chat (700-1100 across, 100-500 down) now sits over the Cam, and over both things in the Box sprig.
    found = {(p["a_name"], p["b_name"], tuple(p["b_in"])): p["box"] for p in pairs}
    assert set(found) == {("Chat", "Cam", ()), ("Chat", "Cam", ("Box",)), ("Chat", "Logo", ("Box",))}
    assert found[("Chat", "Cam", ())] == {"x": 700, "y": 100, "width": 120, "height": 310}
    assert found[("Chat", "Logo", ("Box",))] == {"x": 1060, "y": 200, "width": 40, "height": 150}


def test_both_shapes_a_sprig_and_what_is_refused():
    api = _api()
    both = api.run("layout.get", {"bloom": "Main"})["shapes"]
    assert set(both) == {"horizontal", "vertical"} and both["vertical"]["canvas"] == {"width": 1080, "height": 1920}
    sprig = api.run("layout.get", {"sprig": "Box"})
    assert sprig["sprig"] == "Box" and [i["name"] for i in sprig["shapes"]["horizontal"]["items"]] == ["Cam", "Logo"]
    assert "in" not in sprig["shapes"]["horizontal"]["items"][0]
    with pytest.raises(ApiError) as err:
        api.run("layout.get", {"bloom": "Nope"})
    assert err.value.code == "not_found"
    assert "layout.get" in api.run("app.info", {})["features"]
