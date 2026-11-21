"""Sounds live in the Sound area: removing a sprig, a bloom or an OBS audio scene never deletes them."""

from sprigcast.control.native import NativeApi
from sprigcast.core.convert import drop_sound_sprigs
from sprigcast.core.scene import PART_KIND, Canvas, Scene, SceneCollection, SceneItem, SourceConfig
from test_control_api import FakeHost


def _garden() -> SceneCollection:
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        sources=[SourceConfig(id="mic-1", kind="audio_input", name="Mic"),
                 SourceConfig(id="desk-1", kind="audio_output", name="Wave Link Game"),
                 SourceConfig(id="img", kind="image", name="Logo"),
                 SourceConfig(id="p-in", kind=PART_KIND, name="Input", settings={"scene": "input"}),
                 SourceConfig(id="p-art", kind=PART_KIND, name="Art", settings={"scene": "art"})],
        scenes=[Scene(id="input", name="Input", canvas="horizontal", kind="part",
                      items=[SceneItem(source="mic-1"), SceneItem(source="desk-1")]),
                Scene(id="art", name="Art", canvas="horizontal", kind="part", items=[SceneItem(source="img")]),
                Scene(id="b", name="Gameplay", canvas="horizontal",
                      items=[SceneItem(source="p-in"), SceneItem(source="p-art")]),
                Scene(id="c", name="Chatting", canvas="horizontal")],
        active_scenes={"horizontal": "b"},
    )


def test_removing_a_sprig_keeps_its_sounds():
    col = _garden()
    assert col.would_remove(col.shapes(col.scene("input"))) == []
    assert col.remove_sprig(col.scene("input")) == []
    assert {s.id for s in col.sources} >= {"mic-1", "desk-1"}
    assert all(i.source != "p-in" for i in col.scene("b").items)
    assert col.would_remove(col.shapes(col.scene("art"))) == ["Logo"]
    assert col.remove_sprig(col.scene("art")) == ["Logo"]


def test_removing_a_bloom_keeps_sounds_placed_only_there():
    col = _garden()
    col.scene("c").items.append(SceneItem(source="mic-1"))
    col.scene("input").items = []
    col.remove_bloom(col.scene("c"))
    assert col.source("mic-1") is not None


def test_control_api_sprig_remove():
    col = _garden()
    api = NativeApi(FakeHost(col))
    out = api.run("sprig.remove", {"sprig": "Input"})
    assert out == {"removed": "Input", "sources_removed": []}
    assert col.source("mic-1") is not None and all(s.id != "input" for s in col.scenes)
    assert "sprig.remove" in api.run("app.info", {})["features"]


def test_obs_audio_scenes_become_sounds():
    col = _garden()
    notes = []
    drop_sound_sprigs(col, notes)
    assert all(s.id != "input" for s in col.scenes) and col.source("mic-1") is not None
    assert any(s.id == "art" for s in col.scenes)
    assert notes and "Input" in notes[0]
