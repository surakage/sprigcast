import json
from importlib import resources

import pytest
from pydantic import ValidationError

from sprigcast.core.scene import SceneCollection, SceneItem


def default_collection() -> SceneCollection:
    text = resources.files("sprigcast.data").joinpath("default_scenes.json").read_text(encoding="utf-8")
    return SceneCollection.from_json(text)


def test_default_scenes_load_with_both_canvases():
    c = default_collection()
    assert {canvas.id for canvas in c.canvases} == {"horizontal", "vertical"}
    assert c.active_scene("vertical").linked_to == "starting"


def test_save_and_load_roundtrip(tmp_path):
    c = default_collection()
    path = tmp_path / "scenes.json"
    c.save(path)
    assert SceneCollection.load(path) == c
    assert json.loads(path.read_text())["version"] == 2


def test_linked_scenes_switch_together_both_ways():
    c = default_collection()
    c.set_active("brb")
    assert c.active_scenes == {"horizontal": "brb", "vertical": "brb-v"}
    c.set_active("starting-v")
    assert c.active_scenes == {"horizontal": "starting", "vertical": "starting-v"}


def test_unknown_source_reference_is_rejected():
    data = default_collection().model_dump(mode="json")
    data["scenes"][0]["items"].append({"source": "does-not-exist"})
    with pytest.raises(ValidationError):
        SceneCollection.model_validate(data)


def test_output_must_use_a_known_canvas():
    data = default_collection().model_dump(mode="json")
    data["outputs"][0]["canvas"] = "square"
    with pytest.raises(ValidationError):
        SceneCollection.model_validate(data)


def test_quad_applies_position_scale_and_crop():
    item = SceneItem(source="s", x=100, y=50, scale=0.5, crop=(10, 0, 10, 20))
    quad = item.quad((200, 100))  # cropped to 180 x 80, scaled to 90 x 40
    xs, ys = [p[0] for p in quad], [p[1] for p in quad]
    assert (min(xs), max(xs), min(ys), max(ys)) == pytest.approx((100, 190, 50, 90))
    assert quad[0][2:] == pytest.approx((10 / 200, 0))
    assert quad[3][2:] == pytest.approx((190 / 200, 80 / 100))


def test_quad_rotates_around_centre():
    item = SceneItem(source="s", x=0, y=0, rotation=90)
    quad = item.quad((100, 50))
    cx = sum(p[0] for p in quad) / 4
    cy = sum(p[1] for p in quad) / 4
    assert (cx, cy) == pytest.approx((50, 25))
    xs = [p[0] for p in quad]
    assert max(xs) - min(xs) == pytest.approx(50)  # width and height swap at 90 degrees


def test_fully_cropped_item_draws_nothing():
    assert SceneItem(source="s", crop=(50, 0, 50, 0)).quad((100, 100)) == []


def test_new_id_avoids_existing():
    c = default_collection()
    first = c.new_id("scene")
    assert first not in {s.id for s in c.scenes}
