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


def test_snapshot_and_text_are_full_copies_made_in_compiled_code():
    """PC test 2026-11-17: model_copy(deep=True) took 300+ ms on the window's thread while live."""
    from sprigcast.core.scene import Canvas, SceneCollection, SourceConfig

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                          sources=[SourceConfig(id="a", kind="image", name="A",
                                                settings={"floor": float("-inf"), "list": [1, {"x": 2}]})])
    copy = col.snapshot()
    assert copy == col and copy.sources[0].settings is not col.sources[0].settings
    assert copy.sources[0].settings["list"][1] is not col.sources[0].settings["list"][1]
    copy.sources[0].settings["list"][1]["x"] = 3
    assert col.sources[0].settings["list"][1]["x"] == 2
    text = col.to_text()
    assert "-Infinity" in text and SceneCollection.model_validate_json(text) == col  # not null


def test_a_later_save_queued_on_a_worker_never_overwrites_a_newer_one(tmp_path):
    from sprigcast.core.scene import Canvas, SceneCollection, next_save, write_garden

    path = tmp_path / "garden.json"
    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)])
    old_ticket = next_save(path)  # queued on a worker with the text as it was then
    col.canvases[0].width = 1280
    col.save(path)  # a newer save, here
    assert not write_garden(path, "old", old_ticket)  # the worker gets to it late: skipped
    assert SceneCollection.load(path).canvases[0].width == 1280
