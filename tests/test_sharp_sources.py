"""Sharp sources: text and the card plugins draw at the size they're shown, so they stay crisp made big
(and get lighter made small), while their layout box (`size`) never changes."""

import numpy as np
import pytest

from sprigcast import plugin_store
from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.core.source import create_source, known_kinds
from sprigcast.render.engine import detail_step, shown_detail


def _garden():
    return SceneCollection(
        canvases=[Canvas(id="h", width=1920, height=1080)],
        sources=[
            SourceConfig(id="clock", kind="text", name="Clock"),
            SourceConfig(id="sprig", kind="scene", name="Corner", settings={"scene": "p"}),
        ],
        scenes=[
            Scene(id="show", name="Show", canvas="h", items=[SceneItem(source="sprig", scale=0.5)]),
            Scene(id="p", name="Corner", canvas="h", kind="part", items=[SceneItem(source="clock", scale=3.0)]),
        ],
        active_scenes={"h": "show"},
    )


def test_detail_is_the_biggest_it_is_shown_anywhere_including_inside_sprigs():
    col = _garden()
    # In Build the sprig is edited at its own size (3x); in the bloom it's placed at half size (1.5x).
    assert shown_detail(col, {"clock": (200, 50)}) == {"clock": pytest.approx(3.0)}
    col.scene("p").items[0].scale = 0.2
    assert shown_detail(col, {"clock": (200, 50)}) == {"clock": pytest.approx(0.2)}
    col.scene("show").items[0].scale = 4.0  # the sprig made big: what's in it is shown bigger too
    assert shown_detail(col, {"clock": (200, 50)}) == {"clock": pytest.approx(0.8)}


def test_bounds_decide_the_detail_when_on():
    col = _garden()
    item = col.scene("p").items[0]
    item.bounds, item.bounds_size = "fit", (800.0, 100.0)
    assert shown_detail(col, {"clock": (200, 50)})["clock"] == pytest.approx(2.0)  # 100 / 50


def test_a_sprig_that_holds_itself_does_not_loop_forever():
    col = _garden()
    col.sources.append(SourceConfig(id="again", kind="scene", name="Corner", settings={"scene": "p"}))
    col.scene("p").items.append(SceneItem(source="again", scale=2.0))
    assert shown_detail(col, {"clock": (200, 50)})["clock"] == pytest.approx(3.0)


def test_detail_steps_round_up_and_stay_in_range():
    assert detail_step(1.0, (640, 220)) == 1.0
    assert detail_step(1.01, (640, 220)) == pytest.approx(2 ** 0.25, abs=1e-3)  # never drawn smaller than shown
    assert detail_step(1.9, (640, 220)) == 2.0
    assert detail_step(0.5, (640, 220)) == 0.5
    assert detail_step(0.001, (640, 220)) == 0.125  # tiny: still a few pixels to draw with
    assert detail_step(50.0, (640, 220)) == 4.0
    assert detail_step(50.0, (1600, 260)) == pytest.approx(4096 / 1600, abs=1e-3)  # never over 4096 pixels wide
    steps = {detail_step(1.0 + i / 100, (640, 220)) for i in range(101)}
    assert len(steps) <= 5  # dragging a corner from 1x to 2x redraws it a handful of times, not 100


def test_text_draws_more_pixels_but_keeps_its_box():
    source = create_source("text", "t", {"text": "Hello", "size": 40})
    box = source.size
    first = source.video_frame()
    assert first.shape[:2] == (box[1], box[0])
    source.set_detail(2.0)
    big = source.video_frame()
    assert big is not None and source.size == box  # a new picture straight away, the layout untouched
    assert big.shape[1] == pytest.approx(box[0] * 2, abs=4) and big.shape[0] == pytest.approx(box[1] * 2, abs=4)
    assert source.video_frame() is None  # and nothing again until something changes
    source.set_detail(0.5)
    assert source.video_frame().shape[1] == pytest.approx(box[0] / 2, abs=3) and source.size == box


def test_pictures_and_cameras_are_not_sharp():
    assert not create_source("color", "c", {"color": "#ff0000ff", "width": 4, "height": 4}).sharp
    assert create_source("text", "t", {}).sharp


CARDS = {"local_time": (640, 220), "stream_timer": (640, 220), "now_playing": (760, 200),
         "live_captions": (1600, 260)}


@pytest.fixture
def qt():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("kind", sorted(CARDS))
def test_cards_draw_at_their_detail_and_keep_their_box(kind, qt, tmp_path):
    plugin = next(p for p in plugin_store.catalog() if p.id == kind)
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    try:
        import sys

        module = sys.modules[f"sprigcast_plugin_{kind}"]
        source = create_source(kind, "s", {})
        assert source.sharp and source.size == CARDS[kind]
        if kind == "live_captions":
            image = module.paint("Hello there", {}, 2.0)
        elif kind == "now_playing":
            image = module.paint(module.Track(title="Song", artist="Someone", playing=True), {}, 2.0)
        elif kind == "local_time":
            image = module.paint("8:03 PM", "Sat, Nov 14", {}, 2.0)
        else:
            image = module.paint("1:23", "", {}, True, 2.0)
        assert (image.width(), image.height()) == (CARDS[kind][0] * 2, CARDS[kind][1] * 2)
        rows = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
        assert rows.any()  # something drawn
    finally:
        plugin_store.remove(tmp_path / "plugins", kind)
    assert kind not in known_kinds()


def test_clock_card_is_drawn_again_when_its_detail_steps(qt, tmp_path):
    plugin = next(p for p in plugin_store.catalog() if p.id == "local_time")
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    try:
        source = create_source("local_time", "s", {})
        assert source.video_frame().shape[:2] == (220, 640)
        assert source.video_frame() is None  # same minute: nothing drawn
        source.set_detail(2.0)
        assert source.video_frame().shape[:2] == (440, 1280)
        assert source.size == (640, 220)
    finally:
        plugin_store.remove(tmp_path / "plugins", "local_time")


def test_cards_start_readable_in_their_corner():
    col = SceneCollection(canvases=[Canvas(id="h", width=1920, height=1080)],
                          scenes=[Scene(id="b", name="B", canvas="h")], active_scenes={"h": "b"})
    col.sources.append(SourceConfig(id="t", kind="stream_timer", name="Stream timer"))
    part = col.own_sprig(col.scene("b"), "t", (640, 220), "Stream timer", "top_left", 0.14)
    item = part.items[0]
    assert 220 * item.scale == pytest.approx(1080 * 0.14)  # the card's box a seventh of the canvas tall
    assert item.x == pytest.approx(1080 * 0.035) and item.y == pytest.approx(1080 * 0.035)


def test_the_engine_tells_sharp_sources_their_detail_only_when_it_steps():
    from sprigcast.render.engine import Engine

    col = _garden()
    engine = Engine(col)
    told = []
    clock = create_source("text", "clock", {"text": "Hi"})
    real = clock.set_detail
    clock.set_detail = lambda d: (told.append(d), real(d))
    engine._sources = {"clock": clock, "sprig": create_source("color", "sprig", {})}
    engine._sync_detail()
    step = detail_step(3.0, clock.size)
    assert told == [step] and clock.detail == step
    engine._sync_detail()
    col = engine._collection
    col.scene("p").items[0].scale = 2.95  # a small drag: same step, nothing redrawn
    engine._sync_detail()
    assert told == [step]
    col.scene("p").items[0].scale = 1.0
    engine._sync_detail()
    assert told == [step, 1.0]
