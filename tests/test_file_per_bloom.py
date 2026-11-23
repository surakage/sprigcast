"""A video, image or colour that shows another file or colour in some blooms (Change by bloom)."""

import pytest

from sprigcast.core.scene import PER_BLOOM, PER_BLOOM_ON, Canvas, Scene, SceneCollection, SceneItem, SourceConfig


def _collection(on=True):
    per = {"chat": {"color": "#00ff00ff"}, "soon": {"color": "#0000ffff"}, "gone": {"color": "#ffffffff"}}
    clip = {"path": "main.webm", PER_BLOOM_ON: on, PER_BLOOM: {"soon": {"path": "soon.webm"}}}
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=64, height=36), Canvas(id="vertical", width=36, height=64)],
        sources=[SourceConfig(id="bg", kind="color", name="Background",
                              settings={"color": "#ff0000ff", PER_BLOOM_ON: on, PER_BLOOM: per}),
                 SourceConfig(id="clip", kind="media", name="Clip", settings=clip)],
        scenes=[Scene(id="gaming", name="Gaming", canvas="horizontal", items=[SceneItem(source="bg")]),
                Scene(id="chat", name="Just Chatting", canvas="horizontal", items=[SceneItem(source="bg")]),
                Scene(id="soon", name="Starting Soon", canvas="horizontal", items=[SceneItem(source="bg")]),
                Scene(id="v-chat", name="Just Chatting", canvas="vertical", linked_to="chat")],
        active_scenes={"horizontal": "gaming", "vertical": "v-chat"},
    )


def test_each_bloom_shows_its_own_colour_or_file():
    col = _collection()
    col.canvases[1].enabled = False
    bg, clip = col.source("bg"), col.source("clip")
    assert col.bloom_settings(bg) is bg.settings  # nothing set for Gaming: its own colour
    col.set_active("soon")
    assert col.bloom_settings(bg)["color"] == "#0000ffff"
    assert col.bloom_settings(clip)["path"] == "soon.webm"
    col.set_active("chat")
    assert col.bloom_settings(bg)["color"] == "#00ff00ff"
    assert col.bloom_settings(clip)["path"] == "main.webm"  # no file for this bloom: the main one
    assert bg.settings["color"] == "#ff0000ff"  # the saved setting itself never changes


def test_switched_off_it_works_as_before():
    col = _collection(on=False)
    col.set_active("soon")
    assert col.bloom_settings(col.source("bg")) is col.source("bg").settings


def test_a_linked_twin_counts_as_its_main_bloom():
    col = _collection()  # vertical is on, showing the twin of Just Chatting
    assert col.bloom_settings(col.source("bg"))["color"] == "#00ff00ff"


@pytest.fixture(scope="module")
def compositor():
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL here: {exc}")
    yield comp
    comp.release()


def test_the_engine_follows_the_bloom_on_air(compositor):
    from sprigcast.render.engine import Engine

    col = _collection()
    col.canvases[1].enabled = False
    col.sources = [col.source("bg")]  # the colour only: no video decoder needed
    engine = Engine(col)
    engine._sync_sources(compositor)
    assert engine._sources["bg"].settings["color"] == "#ff0000ff"
    engine._switch_blooms({"horizontal": "soon", "vertical": "v-chat"})
    assert engine._sources["bg"].settings["color"] == "#0000ffff"
    engine._switch_blooms({"horizontal": "gaming", "vertical": "v-chat"})
    assert engine._sources["bg"].settings["color"] == "#ff0000ff"
    engine._update_settings({"bg": {**col.source("bg").settings, "color": "#123456ff"}})
    assert engine._sources["bg"].settings["color"] == "#123456ff"  # its own colour changed in Gaming
    engine._switch_blooms({"horizontal": "chat", "vertical": "v-chat"})
    assert engine._sources["bg"].settings["color"] == "#00ff00ff"


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_card_keeps_only_blooms_with_a_file(app):
    from sprigcast.app.by_bloom import ByBloom

    blooms = [("gaming", "Gaming"), ("chat", "Just Chatting"), ("soon", "Starting Soon")]
    card = ByBloom("media", blooms, {PER_BLOOM_ON: True, PER_BLOOM: {"soon": {"path": "soon.webm"},
                                                                     "gone": {"path": "old.webm"}}})
    assert list(card.rows) == ["soon"]  # a deleted bloom's file is let go
    card._add_row("chat", "")  # added, but no file picked: left out
    assert card.values() == {PER_BLOOM_ON: True, PER_BLOOM: {"soon": {"path": "soon.webm"}}}
    card._remove("soon")
    assert card.values() == {PER_BLOOM_ON: False, PER_BLOOM: {}}  # nothing set: off, as before


def test_video_settings_carry_the_files_per_bloom(app):
    from sprigcast.app.media_dialog import MediaDialog

    settings = {"path": "main.webm", PER_BLOOM_ON: True, PER_BLOOM: {"soon": {"path": "soon.webm"}}}
    dialog = MediaDialog("Clip", settings, blooms=[("soon", "Starting Soon")])
    assert dialog.values()[PER_BLOOM] == {"soon": {"path": "soon.webm"}}
    assert MediaDialog("Clip", settings).by_bloom is None  # no blooms given: the card isn't shown
